import json
from datetime import datetime, timezone

from odoo import api, fields, models


def event_datetime(value):
    if not value:
        return fields.Datetime.now()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return fields.Datetime.now()


class GeothermChatbotSession(models.Model):
    _name = "geotherm.chatbot.session"
    _description = "Geotherm chatbot conversation analytics"
    _order = "last_seen_at desc"

    conversation_id = fields.Char(required=True, index=True)
    anonymous_id = fields.Char(index=True)
    site_id = fields.Char(index=True)
    service_type = fields.Char(index=True, default="unknown")
    service_intent = fields.Char(index=True)
    intent = fields.Char(index=True)
    answer_mode = fields.Char(index=True)
    turn_count = fields.Integer(default=0)
    conversation_count = fields.Integer(default=1)
    lead_captured = fields.Boolean(default=False, index=True)
    lead_count = fields.Integer(default=0)
    first_seen_at = fields.Datetime(required=True)
    last_seen_at = fields.Datetime(required=True)

    _conversation_unique = models.Constraint("UNIQUE(conversation_id)", "Konverzácia už existuje.")

    @api.model
    def register_turn(self, payload):
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
        lead = payload.get("lead") if isinstance(payload.get("lead"), dict) else {}
        conversation_id = str(conversation.get("id") or "").strip()
        if not conversation_id:
            raise ValueError("conversation.id is required")
        event_time = event_datetime(payload.get("createdAt"))
        record = self.sudo().search([("conversation_id", "=", conversation_id)], limit=1)
        captured = bool(lead.get("captured"))
        values = {
            "anonymous_id": session.get("anonymousId") or False,
            "site_id": payload.get("siteId") or False,
            "service_type": classification.get("serviceType") or "unknown",
            "service_intent": classification.get("serviceIntent") or False,
            "intent": classification.get("intent") or False,
            "answer_mode": classification.get("answerMode") or False,
            "last_seen_at": event_time,
            "lead_captured": captured or (record.lead_captured if record else False),
            "lead_count": 1 if captured or (record.lead_captured if record else False) else 0,
        }
        if record:
            values["turn_count"] = record.turn_count + 1
            record.write(values)
        else:
            record = self.sudo().create({
                **values,
                "conversation_id": conversation_id,
                "turn_count": 1,
                "first_seen_at": event_time,
            })
        return record


class GeothermChatbotEvent(models.Model):
    _name = "geotherm.chatbot.event"
    _description = "Geotherm chatbot analytics event"
    _order = "event_at desc"

    external_event_id = fields.Char(required=True, index=True)
    event_type = fields.Char(required=True, index=True)
    event_at = fields.Datetime(required=True, index=True)
    site_id = fields.Char(index=True)
    conversation_id = fields.Char(index=True)
    anonymous_id = fields.Char(index=True)
    service_type = fields.Char(index=True)
    service_intent = fields.Char(index=True)
    answer_mode = fields.Char(index=True)
    intent = fields.Char(index=True)
    lead_captured = fields.Boolean(index=True)
    payload_json = fields.Text(required=True)

    _external_event_unique = models.Constraint("UNIQUE(external_event_id)", "Udalosť už existuje.")

    @api.model
    def create_from_payload(self, payload):
        classification = payload.get("classification") if isinstance(payload.get("classification"), dict) else {}
        conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
        session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
        lead = payload.get("lead") if isinstance(payload.get("lead"), dict) else {}
        external_id = str(payload.get("id") or "").strip()
        if not external_id:
            raise ValueError("event id is required")
        existing = self.sudo().search([("external_event_id", "=", external_id)], limit=1)
        if existing:
            return existing, False
        event_at = event_datetime(payload.get("createdAt"))
        event = self.sudo().create({
            "external_event_id": external_id,
            "event_type": payload.get("event") or "chat.turn",
            "event_at": event_at,
            "site_id": payload.get("siteId") or False,
            "conversation_id": conversation.get("id") or False,
            "anonymous_id": session.get("anonymousId") or False,
            "service_type": classification.get("serviceType") or "unknown",
            "service_intent": classification.get("serviceIntent") or False,
            "answer_mode": classification.get("answerMode") or False,
            "intent": classification.get("intent") or False,
            "lead_captured": bool(lead.get("captured")),
            "payload_json": json.dumps(payload, ensure_ascii=False),
        })
        self.env["geotherm.chatbot.session"].sudo().register_turn(payload)
        return event, True
