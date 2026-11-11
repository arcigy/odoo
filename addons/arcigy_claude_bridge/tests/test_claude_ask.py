import base64
import json
from datetime import datetime, timedelta

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, get_db_name, new_test_user, tagged

# 1x1 transparent PNG
PNG_1PX = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae426082"
)).decode()
AGENT_GROUPS = "base.group_user,arcigy_claude_bridge.group_claude_agent"


def _payload(questions=None, **extra):
    payload = {
        "session_id": "sess-1",
        "chat_name": "Kuchyňa – Gola",
        "kind": "question",
        "context": "Spravil som dve verzie.\nKtorú chceš?",
        "questions": questions or [{
            "header": "Verzia",
            "question": "Ktorú verziu nasadiť?",
            "multi_select": False,
            "options": [
                {"label": "Verzia A", "description": "Rýchlejšia"},
                {"label": "Verzia B", "description": "Bezpečnejšia", "preview": "diff --git a b"},
            ],
        }],
    }
    payload.update(extra)
    return payload


class ClaudeAskCommon:
    @classmethod
    def _setup_users(cls):
        cls.agent = new_test_user(cls.env, "claude_agent_test", groups=AGENT_GROUPS)
        cls.plain = new_test_user(cls.env, "plain_user_test", groups="base.group_user")
        cls.owner = cls.env.ref("base.user_admin")
        cls.Ask = cls.env["arcigy.claude.ask"]

    def _create(self, **kwargs):
        result = self.Ask.with_user(self.agent).arcigy_create_ask(_payload(**kwargs))
        return result, self.Ask.browse(result["id"])


@tagged("post_install", "-at_install")
class TestClaudeAskRpc(ClaudeAskCommon, TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_users()

    def test_agent_creates_ask_and_owner_is_notified(self):
        # Inbox notifications need "handle in Odoo" and an e-mail on the partner (as in production).
        self.owner.write({"notification_type": "inbox", "email": "owner@arcigy.test"})
        channel = self.Ask._get_channel()
        channel.channel_member_ids.filtered(lambda m: m.partner_id == self.agent.partner_id).unlink()
        result, ask = self._create()
        self.assertIn(self.agent.partner_id, channel.channel_member_ids.partner_id)
        self.assertTrue(result["url"].endswith(ask.sudo()._relative_url()))
        message = ask.sudo().channel_message_id
        self.assertEqual(message.model, "discuss.channel")
        self.assertEqual(message.author_id, self.agent.partner_id)
        self.assertIn(self.owner.partner_id, message.partner_ids)
        self.assertTrue(
            self.env["mail.notification"].search_count(
                [("mail_message_id", "=", message.id), ("res_partner_id", "=", self.owner.partner_id.id)]
            )
        )

    def test_plain_user_cannot_call_rpc(self):
        _result, ask = self._create()
        Ask = self.Ask.with_user(self.plain)
        calls = [
            lambda: Ask.arcigy_create_ask(_payload()),
            lambda: Ask.browse(ask.id).arcigy_get_asks(),
            lambda: Ask.browse(ask.id).arcigy_close_asks("x"),
            lambda: Ask.arcigy_react([ask.sudo().channel_message_id.id], "✅"),
        ]
        for call in calls:
            with self.assertRaises(AccessError):
                call()

    def test_close_asks_closes_only_open_ones(self):
        _r1, open_ask = self._create()
        _r2, answered_ask = self._create()
        answered_ask.with_user(self.owner)._save_answer([(["Verzia A"], "")])
        body_before = open_ask.sudo().channel_message_id.body
        closed = self.Ask.with_user(self.agent).browse([open_ask.id, answered_ask.id]).arcigy_close_asks("Chat skončil")
        self.assertEqual(closed, [open_ask.id])
        states = {a["id"]: a["state"] for a in self.Ask.with_user(self.agent).browse([open_ask.id, answered_ask.id]).arcigy_get_asks()}
        self.assertEqual(states, {open_ask.id: "closed", answered_ask.id: "answered"})
        # The original channel message is edited in place, no new message is posted.
        message = open_ask.sudo().channel_message_id
        self.assertNotEqual(message.body, body_before)
        self.assertIn("Chat skončil", message.body)

    def test_non_image_is_refused(self):
        bad = {"name": "skript.html", "mimetype": "text/html", "data": base64.b64encode(b"<b>x</b>").decode()}
        with self.assertRaises(UserError):
            self.Ask.with_user(self.agent).arcigy_create_ask(_payload(images=[bad]))
        # A wrong claim about the bytes is refused too.
        fake = {"name": "foto.png", "mimetype": "image/png", "data": base64.b64encode(b"<svg/>").decode()}
        with self.assertRaises(UserError):
            self.Ask.with_user(self.agent).arcigy_create_ask(_payload(images=[fake]))

    def test_react_only_in_channels_where_caller_is_member(self):
        _result, ask = self._create()
        own_message = ask.sudo().channel_message_id
        other_channel = self.env["discuss.channel"].create({"name": "cudzí", "channel_type": "channel"})
        foreign_message = other_channel.message_post(body="ahoj", message_type="comment")
        reacted = self.Ask.with_user(self.agent).arcigy_react([own_message.id, foreign_message.id], "✅")
        self.assertEqual(reacted, [own_message.id])
        reactions = self.env["mail.message.reaction"].search([("partner_id", "=", self.agent.partner_id.id)])
        self.assertEqual(reactions.message_id, own_message)


@tagged("post_install", "-at_install")
class TestClaudeAskPage(ClaudeAskCommon, HttpCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_users()

    def _post(self, ask, data):
        data = dict(data, csrf_token=http.Request.csrf_token(self))
        return self.url_open(ask.sudo()._relative_url(), data=data, allow_redirects=False)

    def _answers(self, ask):
        return self.Ask.with_user(self.agent).browse(ask.id).arcigy_get_asks()[0]

    def test_daemon_flow_over_json2(self):
        key = self.agent.with_user(self.agent).env["res.users.apikeys"]._generate(
            scope="rpc", name="daemon", expiration_date=datetime.now() + timedelta(days=1)
        )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "X-Odoo-Database": get_db_name(),
        }

        def call(method, **params):
            response = self.url_open(
                f"/json/2/arcigy.claude.ask/{method}", data=json.dumps(params), headers=headers
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        created = call("arcigy_create_ask", payload=_payload())
        self.assertIn(f"/claude/otazka/{created['id']}?t=", created["url"])
        [item] = call("arcigy_get_asks", ids=[created["id"]])
        self.assertEqual(item["state"], "open")
        self.assertEqual(item["questions"][0]["labels"], [])
        self.assertEqual(call("arcigy_close_asks", ids=[created["id"]], reason="Chat skončil"), [created["id"]])
        [item] = call("arcigy_get_asks", ids=[created["id"]])
        self.assertEqual((item["state"], item["close_reason"]), ("closed", "Chat skončil"))

    def test_image_is_stored_posted_and_shown(self):
        _result, ask = self._create(images=[{"name": "../screen shot.png", "mimetype": "image/png", "data": PNG_1PX}])
        image = ask.sudo().image_ids
        self.assertEqual(len(image), 1)
        self.assertEqual((image.res_model, image.res_id, image.mimetype), (ask._name, ask.id, "image/png"))
        self.assertNotIn("/", image.name)
        message = ask.sudo().channel_message_id
        self.assertEqual(len(message.attachment_ids), 1)
        self.assertEqual(message.attachment_ids.raw, image.raw)
        self.assertEqual(message.attachment_ids.res_model, "discuss.channel")
        self.authenticate("admin", "admin")
        page = self.url_open(ask.sudo()._relative_url())
        self.assertIn(f'src="/web/image/{image.id}"', page.text)
        self.assertEqual(self.url_open(f"/web/image/{image.id}").status_code, 200)

    def test_plain_user_gets_403(self):
        _result, ask = self._create()
        self.authenticate("plain_user_test", "plain_user_test")
        self.assertEqual(self.url_open(ask.sudo()._relative_url(), allow_redirects=False).status_code, 403)

    def test_wrong_token_gets_404(self):
        _result, ask = self._create()
        self.authenticate("admin", "admin")
        self.assertEqual(self.url_open(f"/claude/otazka/{ask.id}?t=wrong").status_code, 404)
        self.assertEqual(self.url_open(f"/claude/otazka/{ask.id}").status_code, 404)

    def test_admin_answers_single_select(self):
        _result, ask = self._create()
        self.authenticate("admin", "admin")
        page = self.url_open(ask.sudo()._relative_url())
        self.assertEqual(page.status_code, 200)
        response = self._post(ask, {"q0": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("ok=1", response.headers["Location"])
        answer = self._answers(ask)
        self.assertEqual(answer["state"], "answered")
        self.assertEqual(answer["questions"][0]["labels"], ["Verzia B"])
        self.assertIsNone(answer["questions"][0]["other"])
        self.assertTrue(answer["answered_at"])
        self.assertEqual(ask.sudo().answered_by, self.owner)

    def test_admin_answers_multi_select_with_other(self):
        _result, ask = self._create(questions=[
            {"header": "Kroky", "question": "Čo spraviť?", "multi_select": True,
             "options": [{"label": "Testy"}, {"label": "Build"}, {"label": "Deploy"}]},
            {"question": "Kedy?", "options": [{"label": "Hneď"}, {"label": "Zajtra"}]},
        ])
        self.authenticate("admin", "admin")
        response = self._post(ask, {"q0": ["0", "2", "__other__"], "q0_other": "Aj PR", "q1": "__other__", "q1_other": "V piatok"})
        self.assertEqual(response.status_code, 303)
        questions = self._answers(ask)["questions"]
        self.assertEqual(questions[0]["labels"], ["Testy", "Deploy"])
        self.assertEqual(questions[0]["other"], "Aj PR")
        self.assertEqual(questions[1]["labels"], [])
        self.assertEqual(questions[1]["other"], "V piatok")

    def test_incomplete_answer_is_refused(self):
        _result, ask = self._create(questions=[
            {"question": "Prvá?", "options": [{"label": "Áno"}]},
            {"question": "Druhá?", "options": [{"label": "Nie"}]},
        ])
        self.authenticate("admin", "admin")
        response = self._post(ask, {"q0": "0", "q1": "__other__", "q1_other": "  "})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._answers(ask)["state"], "open")
