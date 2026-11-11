import base64
import binascii
import json
import os
import re
import secrets

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.mimetypes import guess_mimetype

CHANNEL_NAME = "otázky-z-chatov"
CHANNEL_DESCRIPTION = "Otázky z Claude chatov – odpovedz klepnutím na Odpovedať"
CHANNEL_PARAM = "arcigy_claude_bridge.channel_id"
AGENT_LOGINS_PARAM = "arcigy_claude_bridge.agent_logins"
DEFAULT_AGENT_LOGINS = "agent-claude-chaty@arcigy.local,agent-git-a-vetvy@arcigy.local"
REACTION_EMOJIS = ("✅", "👀", "⚠️")

IMAGE_MIMETYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 8 * 1024 * 1024

LIMITS = {
    "context": 4000,
    "session_id": 200,
    "chat_name": 200,
    "header": 100,
    "question": 2000,
    "label": 200,
    "description": 1000,
    "preview": 4000,
}
KINDS = [("question", "Otázka"), ("next_step", "Ďalší krok"), ("waiting", "Čaká na povolenie")]


def _check_text(value, field, required=False, path=""):
    """Validate an optional/required string from the RPC payload and return it stripped of nothing."""
    where = f"{path}{field}"
    if value is None or value is False or value == "":
        if required:
            raise UserError(f"Chýba povinné pole „{where}“.")
        return ""
    if not isinstance(value, str):
        raise UserError(f"Pole „{where}“ musí byť text.")
    if required and not value.strip():
        raise UserError(f"Chýba povinné pole „{where}“.")
    if len(value) > LIMITS[field]:
        raise UserError(f"Pole „{where}“ je dlhšie ako {LIMITS[field]} znakov.")
    return value


def _clean_image_name(name, index):
    """Plain file name: no path, no control or reserved characters, at most 200 chars."""
    name = os.path.basename(str(name).replace("\\", "/"))
    name = re.sub(r'[\x00-\x1f\x7f<>:"/\\|?*]+', "_", name).strip(" .")
    return name[:200] or f"obrazok-{index + 1}"


def _parse_images(images):
    """Validate the optional ``images`` payload; return a list of (name, mimetype, raw bytes)."""
    if images in (None, False):
        return []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise UserError(f"Pole „images“ musí byť zoznam najviac {MAX_IMAGES} obrázkov.")
    parsed = []
    for index, image in enumerate(images):
        where = f"images[{index}]"
        if not isinstance(image, dict):
            raise UserError(f"Obrázok {index + 1} musí byť objekt.")
        name, mimetype, data = image.get("name"), image.get("mimetype"), image.get("data")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise UserError(f"Pole „{where}.name“ musí byť text do 200 znakov.")
        if mimetype not in IMAGE_MIMETYPES:
            raise UserError(f"Obrázok „{name}“ má nepovolený typ. Povolené: {', '.join(IMAGE_MIMETYPES)}.")
        if not isinstance(data, str) or not data or len(data) > (MAX_IMAGE_BYTES * 4) // 3 + 8:
            raise UserError(f"Obrázok „{name}“ chýba alebo je väčší ako 8 MB.")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise UserError(f"Obrázok „{name}“ nie je platný base64.") from None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise UserError(f"Obrázok „{name}“ chýba alebo je väčší ako 8 MB.")
        # The bytes must really be one of the allowed image types, whatever the caller claims.
        if guess_mimetype(raw) not in IMAGE_MIMETYPES:
            raise UserError(f"Obrázok „{name}“ nie je PNG, JPEG, GIF ani WebP.")
        parsed.append((_clean_image_name(name, index), guess_mimetype(raw), raw))
    return parsed


class ArcigyClaudeAsk(models.Model):
    _name = "arcigy.claude.ask"
    _description = "Otázka z Claude chatu"
    _order = "id desc"

    name = fields.Char(required=True)
    session_id = fields.Char(index=True, required=True)
    chat_name = fields.Char()
    kind = fields.Selection(KINDS, default="question", required=True)
    context_text = fields.Text()
    state = fields.Selection(
        [("open", "Čaká"), ("answered", "Zodpovedané"), ("closed", "Uzavreté")],
        default="open",
        required=True,
        index=True,
    )
    close_reason = fields.Char()
    access_token = fields.Char(
        default=lambda self: secrets.token_urlsafe(32),
        copy=False,
        groups="base.group_system",
    )
    answered_at = fields.Datetime()
    answered_by = fields.Many2one("res.users")
    question_ids = fields.One2many("arcigy.claude.ask.question", "ask_id")
    image_ids = fields.Many2many(
        "ir.attachment", "arcigy_claude_ask_image_rel", "ask_id", "attachment_id", copy=False
    )
    channel_message_id = fields.Many2one("mail.message", ondelete="set null")
    url = fields.Char(compute="_compute_url", compute_sudo=True)

    @api.depends("access_token")
    def _compute_url(self):
        base_url = self.env["ir.config_parameter"].sudo().get_param("web.base.url", "").rstrip("/")
        for ask in self:
            ask.url = f"{base_url}{ask._relative_url()}" if ask.id else False

    def _relative_url(self):
        self.ensure_one()
        return f"/claude/otazka/{self.id}?t={self.sudo().access_token}"

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    @api.model
    def _check_rpc_access(self):
        user = self.env.user
        if not (user.has_group("arcigy_claude_bridge.group_claude_agent") or user.has_group("base.group_system")):
            raise AccessError("Na otázky z Claude chatov nemáš prístup.")

    # ------------------------------------------------------------------
    # Channel
    # ------------------------------------------------------------------

    @api.model
    def _owner(self):
        return self.env.ref("base.user_admin").sudo()

    @api.model
    def _agent_users(self):
        return self.env.ref("arcigy_claude_bridge.group_claude_agent").sudo().user_ids

    @api.model
    def _get_channel(self):
        """Return the questions channel, creating it (and its members) when missing."""
        Channel = self.env["discuss.channel"].sudo()
        params = self.env["ir.config_parameter"].sudo()
        channel = Channel.browse(int(params.get_param(CHANNEL_PARAM) or 0)).exists()
        if not channel:
            channel = Channel.search([("name", "=", CHANNEL_NAME), ("channel_type", "=", "channel")], limit=1)
        if not channel:
            channel = Channel.create({"name": CHANNEL_NAME, "channel_type": "channel"})
        group = self.env.ref("arcigy_claude_bridge.group_claude_agent")
        owner = self._owner()
        # The channel is restricted to the agent group; the owner joins it so he can be mentioned.
        if owner not in group.sudo().user_ids:
            group.sudo().write({"user_ids": [(4, owner.id)]})
        values = {}
        if channel.description != CHANNEL_DESCRIPTION:
            values["description"] = CHANNEL_DESCRIPTION
        if channel.group_public_id != group:
            values["group_public_id"] = group.id
        if values:
            channel.write(values)
        channel._add_members(users=owner | self._agent_users(), post_joined_message=False)
        owner_member = channel.channel_member_ids.filtered(lambda m: m.partner_id == owner.partner_id)
        if owner_member.custom_notifications != "all":
            owner_member.custom_notifications = "all"
        if params.get_param(CHANNEL_PARAM) != str(channel.id):
            params.set_param(CHANNEL_PARAM, str(channel.id))
        return channel

    def _question_body(self):
        self.ensure_one()
        owner = self._owner().partner_id
        mention = Markup(
            '<a href="/odoo/res.partner/%s" class="o_mail_redirect" data-oe-id="%s" '
            'data-oe-model="res.partner" target="_blank">@%s</a>'
        ) % (owner.id, owner.id, owner.name)
        parts = [Markup("<p>❓ <b>%s</b> sa pýta: %s</p>") % (self.chat_name or self.session_id, mention)]
        for question in self.question_ids:
            items = Markup("").join(
                Markup("<li>%s%s</li>")
                % (option.label, Markup(" – %s") % option.description if option.description else "")
                for option in question.option_ids
            )
            parts.append(Markup("<p><b>%s</b></p><ol>%s</ol>") % (question.question, items))
        parts.append(Markup('<p><a href="%s">Odpovedať →</a></p>') % self.url)
        return Markup("").join(parts)

    def _final_line(self):
        self.ensure_one()
        if self.state == "answered":
            return "✅ Odpovedal si: " + self._answer_summary()
        if self.state == "closed":
            return "☑️ Uzavreté: " + (self.close_reason or "bez dôvodu")
        return ""

    def _answer_summary(self):
        self.ensure_one()
        chunks = []
        for question in self.question_ids:
            answer = ", ".join(question._answer_list())
            chunks.append(f"{question.header}: {answer}" if len(self.question_ids) > 1 and question.header else answer)
        return "; ".join(chunks)

    def _post_to_channel(self):
        for ask in self.sudo():
            channel = self._get_channel()
            # Agent users created after install are not members yet.
            channel._add_members(partners=self.env.user.partner_id, post_joined_message=False)
            message = channel.message_post(
                body=ask._question_body(),
                author_id=self.env.user.partner_id.id,
                message_type="comment",
                subtype_xmlid="mail.mt_comment",
                partner_ids=[self._owner().partner_id.id],
                # Copies owned by the channel, so every channel member can open them in Discuss.
                attachments=[(image.name, image.raw) for image in ask.image_ids],
            )
            ask.channel_message_id = message

    def _update_channel_message(self):
        for ask in self.sudo():
            message = ask.channel_message_id
            if not message or message.model != "discuss.channel":
                continue
            channel = self.env["discuss.channel"].sudo().browse(message.res_id).exists()
            if not channel:
                continue
            body = ask._question_body() + Markup("<p>%s</p>") % ask._final_line()
            channel._message_update_content(message, body=body)

    # ------------------------------------------------------------------
    # RPC API (Mac daemon)
    # ------------------------------------------------------------------

    @api.model
    def arcigy_create_ask(self, payload):
        self._check_rpc_access()
        if not isinstance(payload, dict):
            raise UserError("Vstup musí byť objekt.")
        session_id = _check_text(payload.get("session_id"), "session_id", required=True)
        chat_name = _check_text(payload.get("chat_name"), "chat_name")
        context = _check_text(payload.get("context"), "context")
        kind = payload.get("kind") or "question"
        if kind not in dict(KINDS):
            raise UserError(f"Neznámy druh „{kind}“. Povolené: {', '.join(dict(KINDS))}.")
        questions = payload.get("questions")
        if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
            raise UserError("Pole „questions“ musí mať 1 až 4 otázky.")
        question_commands = []
        for qi, question in enumerate(questions):
            path = f"questions[{qi}]."
            if not isinstance(question, dict):
                raise UserError(f"Otázka {qi + 1} musí byť objekt.")
            multi = question.get("multi_select", False)
            if not isinstance(multi, bool):
                raise UserError(f"Pole „{path}multi_select“ musí byť true alebo false.")
            options = question.get("options")
            if not isinstance(options, list) or not 1 <= len(options) <= 6:
                raise UserError(f"Otázka {qi + 1} musí mať 1 až 6 možností.")
            option_commands = []
            for oi, option in enumerate(options):
                opath = f"{path}options[{oi}]."
                if not isinstance(option, dict):
                    raise UserError(f"Možnosť {oi + 1} otázky {qi + 1} musí byť objekt.")
                option_commands.append((0, 0, {
                    "sequence": oi,
                    "label": _check_text(option.get("label"), "label", required=True, path=opath),
                    "description": _check_text(option.get("description"), "description", path=opath),
                    "preview": _check_text(option.get("preview"), "preview", path=opath),
                }))
            question_commands.append((0, 0, {
                "sequence": qi,
                "header": _check_text(question.get("header"), "header", path=path),
                "question": _check_text(question.get("question"), "question", required=True, path=path),
                "multi_select": multi,
                "option_ids": option_commands,
            }))
        images = _parse_images(payload.get("images"))
        first = questions[0]
        ask = self.sudo().create({
            "name": (first.get("header") or first.get("question"))[:120],
            "session_id": session_id,
            "chat_name": chat_name,
            "kind": kind,
            "context_text": context,
            "question_ids": question_commands,
        })
        if images:
            attachments = self.env["ir.attachment"].sudo().create([
                {"name": name, "mimetype": mimetype, "raw": raw, "res_model": self._name, "res_id": ask.id}
                for name, mimetype, raw in images
            ])
            ask.image_ids = attachments
        ask._post_to_channel()
        return {"id": ask.id, "url": ask.url}

    def arcigy_get_asks(self):
        """Record method: JSON-2 browses ``self`` from the ``ids`` key."""
        self._check_rpc_access()
        result = []
        for ask in self.sudo().exists():
            result.append({
                "id": ask.id,
                "state": ask.state,
                "close_reason": ask.close_reason or None,
                "answered_at": ask.answered_at.isoformat() + "Z" if ask.answered_at else None,
                "questions": [
                    {
                        "header": question.header or "",
                        "question": question.question,
                        "labels": question._labels(),
                        "other": question.answer_other or None,
                    }
                    for question in ask.question_ids
                ],
            })
        return result

    def arcigy_close_asks(self, reason=""):
        """Record method: closes the asks in ``self`` that are still open, returns their ids."""
        self._check_rpc_access()
        if reason and not isinstance(reason, str):
            raise UserError("Dôvod musí byť text.")
        asks = self.sudo().exists().filtered(lambda a: a.state == "open")
        asks.write({"state": "closed", "close_reason": (reason or "")[:500]})
        asks._update_channel_message()
        return asks.ids

    @api.model
    def arcigy_react(self, message_ids, emoji):
        self._check_rpc_access()
        if emoji not in REACTION_EMOJIS:
            raise UserError(f"Povolené reakcie: {' '.join(REACTION_EMOJIS)}.")
        partner = self.env.user.partner_id
        messages = self.env["mail.message"].sudo().browse(self._clean_ids(message_ids)).exists()
        reacted = []
        for message in messages:
            if message.model != "discuss.channel":
                continue
            is_member = self.env["discuss.channel.member"].sudo().search_count(
                [("channel_id", "=", message.res_id), ("partner_id", "=", partner.id)]
            )
            if not is_member:
                continue
            message._message_reaction(emoji, "add", partner, self.env["mail.guest"])
            reacted.append(message.id)
        return reacted

    @api.model
    def _clean_ids(self, ids):
        if isinstance(ids, int):
            ids = [ids]
        if not isinstance(ids, (list, tuple)) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise UserError("Zoznam id musí obsahovať celé čísla.")
        return list(ids)

    # ------------------------------------------------------------------
    # Answering (page controller)
    # ------------------------------------------------------------------

    def _save_answer(self, answers):
        """answers: list (per question) of (labels, other_text). Caller checked access and completeness."""
        self.ensure_one()
        ask = self.sudo()
        for question, (labels, other) in zip(ask.question_ids, answers):
            question.write({
                "answer_labels": json.dumps(labels, ensure_ascii=False),
                "answer_other": other or False,
            })
        ask.write({
            "state": "answered",
            "answered_at": fields.Datetime.now(),
            "answered_by": self.env.user.id,
        })
        ask._update_channel_message()


class ArcigyClaudeAskQuestion(models.Model):
    _name = "arcigy.claude.ask.question"
    _description = "Otázka z Claude chatu – otázka"
    _order = "sequence, id"

    ask_id = fields.Many2one("arcigy.claude.ask", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer()
    header = fields.Char()
    question = fields.Text(required=True)
    multi_select = fields.Boolean()
    option_ids = fields.One2many("arcigy.claude.ask.option", "question_id")
    answer_labels = fields.Text()
    answer_other = fields.Text()

    def _labels(self):
        self.ensure_one()
        try:
            labels = json.loads(self.answer_labels or "[]")
        except ValueError:
            return []
        return labels if isinstance(labels, list) else []

    def _answer_list(self):
        self.ensure_one()
        answers = list(self._labels())
        if self.answer_other:
            answers.append(self.answer_other)
        return answers


class ArcigyClaudeAskOption(models.Model):
    _name = "arcigy.claude.ask.option"
    _description = "Otázka z Claude chatu – možnosť"
    _order = "sequence, id"

    question_id = fields.Many2one("arcigy.claude.ask.question", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer()
    label = fields.Char(required=True)
    description = fields.Text()
    preview = fields.Text()
