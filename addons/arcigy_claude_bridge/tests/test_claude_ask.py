from odoo import http
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

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
        result, ask = self._create()
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
            lambda: Ask.arcigy_get_asks([ask.id]),
            lambda: Ask.arcigy_close_asks([ask.id], "x"),
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
        closed = self.Ask.with_user(self.agent).arcigy_close_asks([open_ask.id, answered_ask.id], "Chat skončil")
        self.assertEqual(closed, [open_ask.id])
        states = {a["id"]: a["state"] for a in self.Ask.with_user(self.agent).arcigy_get_asks([open_ask.id, answered_ask.id])}
        self.assertEqual(states, {open_ask.id: "closed", answered_ask.id: "answered"})
        # The original channel message is edited in place, no new message is posted.
        message = open_ask.sudo().channel_message_id
        self.assertNotEqual(message.body, body_before)
        self.assertIn("Chat skončil", message.body)

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
        return self.Ask.with_user(self.agent).arcigy_get_asks([ask.id])[0]

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
