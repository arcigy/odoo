import hmac

from werkzeug.exceptions import Forbidden, NotFound

from odoo import fields, http
from odoo.http import request

OTHER = "__other__"


class ClaudeAskController(http.Controller):
    def _check_owner(self):
        if not request.env.user.has_group("base.group_system"):
            raise Forbidden()

    def _get_ask(self, ask_id, token):
        self._check_owner()
        ask = request.env["arcigy.claude.ask"].sudo().browse(ask_id).exists()
        if not ask or not token or not ask.access_token or not hmac.compare_digest(
            str(ask.access_token), str(token)
        ):
            raise NotFound()
        return ask

    def _local_time(self, ask, value=None):
        stamp = fields.Datetime.context_timestamp(ask.with_user(request.env.user), value or ask.create_date)
        return stamp.strftime("%d. %m. %Y %H:%M")

    def _render(self, ask, *, selected=None, others=None, error=None, just_sent=False, status=200):
        questions = []
        for index, question in enumerate(ask.question_ids):
            questions.append({
                "index": index,
                "record": question,
                "selected": (selected or {}).get(index, []),
                "other": (others or {}).get(index, ""),
                "answers": question._answer_list(),
            })
        auto_submit = len(ask.question_ids) == 1 and not ask.question_ids.multi_select
        values = {
            "ask": ask,
            "questions": questions,
            "error": error,
            "just_sent": just_sent,
            "auto_submit": auto_submit,
            "time_text": self._local_time(ask),
            "time_answered": self._local_time(ask, ask.answered_at) if ask.answered_at else "",
            "kind_text": dict(ask._fields["kind"].selection).get(ask.kind, ""),
            "form_action": f"/claude/otazka/{ask.id}?t={ask.access_token}",
        }
        response = request.render("arcigy_claude_bridge.ask_page", values)
        response.status_code = status
        return response

    @http.route("/claude/otazka/<int:ask_id>", type="http", auth="user", methods=["GET", "POST"], sitemap=False)
    def ask_page(self, ask_id, t=None, ok=None, **kwargs):
        ask = self._get_ask(ask_id, t)
        if request.httprequest.method == "POST":
            return self._submit(ask)
        return self._render(ask, just_sent=bool(ok) and ask.state == "answered")

    def _submit(self, ask):
        if ask.state != "open":
            return self._render(ask)
        form = request.httprequest.form
        selected, others, answers, missing = {}, {}, [], []
        for index, question in enumerate(ask.question_ids):
            values = form.getlist(f"q{index}")
            if not question.multi_select:
                values = values[:1]
            other_text = (form.get(f"q{index}_other") or "").strip()
            selected[index] = values
            others[index] = other_text
            options = question.option_ids
            labels = []
            for value in values:
                if value.isdigit() and int(value) < len(options):
                    labels.append(options[int(value)].label)
            # Free text counts when "Iné" is chosen, or when nothing else is (page without JS).
            use_other = other_text and (OTHER in values or not labels)
            if not labels and not use_other:
                missing.append(question.header or str(index + 1))
            answers.append((labels, other_text if use_other else ""))
        if missing:
            return self._render(
                ask,
                selected=selected,
                others=others,
                error="Vyber odpoveď pri každej otázke (" + ", ".join(missing) + ").",
                status=400,
            )
        ask._save_answer(answers)
        return request.redirect(f"/claude/otazka/{ask.id}?t={ask.access_token}&ok=1", code=303)

    @http.route("/claude/otazky", type="http", auth="user", methods=["GET"], sitemap=False)
    def ask_list(self, **kwargs):
        self._check_owner()
        Ask = request.env["arcigy.claude.ask"].sudo()
        open_asks = Ask.search([("state", "=", "open")], order="id desc")
        done_asks = Ask.search([("state", "!=", "open")], order="write_date desc, id desc", limit=10)

        def rows(asks):
            return [{
                "ask": ask,
                "url": f"/claude/otazka/{ask.id}?t={ask.access_token}",
                "time_text": self._local_time(ask),
            } for ask in asks]

        return request.render("arcigy_claude_bridge.ask_list_page", {
            "open_rows": rows(open_asks),
            "done_rows": rows(done_asks),
        })
