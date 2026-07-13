import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

from odoo import SUPERUSER_ID, http
from odoo.http import Response, request


def json_response(payload, status=200):
    return Response(
        json.dumps(payload, ensure_ascii=False, default=str),
        status=status,
        content_type="application/json; charset=utf-8",
    )


class GeothermApiController(http.Controller):
    def _configured_value(self, env_name, parameter_name):
        return os.getenv(env_name) or request.env["ir.config_parameter"].sudo().get_param(parameter_name) or ""

    def _authorized(self, raw_body):
        api_key = self._configured_value("GEOTHERM_API_KEY", "geotherm_chatbot.api_key")
        secret = self._configured_value("GEOTHERM_WEBHOOK_SECRET", "geotherm_chatbot.webhook_secret")
        authorization = request.httprequest.headers.get("Authorization", "")
        bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if api_key and hmac.compare_digest(bearer, api_key):
            return True
        timestamp = request.httprequest.headers.get("X-Arcigy-Timestamp", "")
        signature = request.httprequest.headers.get("X-Arcigy-Signature", "")
        if not secret or not timestamp or not signature.startswith("sha256="):
            return False
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            if abs((datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds()) > 300:
                return False
        except ValueError:
            return False
        expected = hmac.new(secret.encode(), timestamp.encode() + b"\n" + raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature[7:], expected)

    def _json_body(self, raw_body):
        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must be valid UTF-8 JSON.")
        if not isinstance(body, dict):
            raise ValueError("Request body must be a JSON object.")
        return body

    @http.route("/geotherm/api/v1/health", type="http", auth="none", methods=["GET"], csrf=False, save_session=False)
    def health(self, **_kwargs):
        if not self._authorized(b""):
            return json_response({"ok": False, "error": "unauthorized"}, 401)
        request.update_env(user=SUPERUSER_ID, su=True)
        return json_response({
            "ok": True,
            "service": "geotherm-odoo",
            "products": request.env["geotherm.pricebook.product"].sudo().search_count([("active", "=", True)]),
            "questions": request.env["geotherm.pricebook.question"].sudo().search_count([("active", "=", True)]),
            "leads": request.env["crm.lead"].sudo().search_count([("geotherm_external_lead_id", "!=", False)]),
        })

    @http.route("/geotherm/api/v1/leads", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def create_lead(self, **_kwargs):
        raw_body = request.httprequest.get_data(cache=True)
        if not self._authorized(raw_body):
            return json_response({"ok": False, "error": "unauthorized"}, 401)
        request.update_env(user=SUPERUSER_ID, su=True)
        try:
            payload = self._json_body(raw_body)
            lead, created = request.env["crm.lead"].with_user(SUPERUSER_ID).upsert_from_chatbot(payload)
            conversation_id = lead.geotherm_conversation_id
            if conversation_id:
                session = request.env["geotherm.chatbot.session"].sudo().search([("conversation_id", "=", conversation_id)], limit=1)
                if session:
                    session.write({"lead_captured": True, "lead_count": 1})
            return json_response({"ok": True, "created": created, "odooLeadId": lead.id}, 201 if created else 200)
        except (ValueError, TypeError) as error:
            return json_response({"ok": False, "error": str(error)}, 400)

    @http.route("/geotherm/api/v1/events", type="http", auth="none", methods=["POST"], csrf=False, save_session=False)
    def create_event(self, **_kwargs):
        raw_body = request.httprequest.get_data(cache=True)
        if not self._authorized(raw_body):
            return json_response({"ok": False, "error": "unauthorized"}, 401)
        request.update_env(user=SUPERUSER_ID, su=True)
        try:
            payload = self._json_body(raw_body)
            event, created = request.env["geotherm.chatbot.event"].sudo().create_from_payload(payload)
            return json_response({"ok": True, "created": created, "eventId": event.id}, 201 if created else 200)
        except (ValueError, TypeError) as error:
            return json_response({"ok": False, "error": str(error)}, 400)

    @http.route("/geotherm/api/v1/pricebook", type="http", auth="none", methods=["GET"], csrf=False, save_session=False)
    def pricebook(self, **_kwargs):
        if not self._authorized(b""):
            return json_response({"ok": False, "error": "unauthorized"}, 401)
        request.update_env(user=SUPERUSER_ID, su=True)
        catalog = request.env["geotherm.pricebook.area"].sudo().export_catalog()
        return json_response(catalog)
