import json

from odoo import api, fields, models


SERVICE_LABELS = {
    "heat_pump": "tepelné čerpadlo",
    "air_conditioning": "klimatizáciu",
    "heat_recovery": "rekuperáciu",
    "floor_heating": "podlahové kúrenie",
    "ceiling_cooling": "stropné chladenie",
    "solar_photovoltaic": "fotovoltiku",
    "boilers": "kotol",
    "radiators": "radiátory",
    "water": "vodu a rozvody",
    "sanitary": "sanitu",
    "central_vacuum": "centrálny vysávač",
    "screeds": "potery",
    "service": "servis",
}


class CrmLead(models.Model):
    _inherit = "crm.lead"

    geotherm_external_lead_id = fields.Char(index=True, copy=False)
    geotherm_conversation_id = fields.Char(index=True, copy=False)
    geotherm_site_id = fields.Char(copy=False)
    geotherm_service_type = fields.Char(copy=False)
    geotherm_service_intent = fields.Char(copy=False)
    geotherm_project_type = fields.Char(copy=False)
    geotherm_area_m2 = fields.Float(copy=False)
    geotherm_heating_distribution = fields.Char(copy=False)
    geotherm_current_heat_source = fields.Char(copy=False)
    geotherm_wants_cooling = fields.Boolean(copy=False)
    geotherm_wants_hot_water = fields.Boolean(copy=False)
    geotherm_lead_score = fields.Integer(copy=False)
    geotherm_lead_temperature = fields.Selection(
        [("cold", "Cold"), ("warm", "Warm"), ("hot", "Hot")], copy=False
    )
    geotherm_chatbot_summary = fields.Text(copy=False)
    geotherm_transcript_json = fields.Text(copy=False)
    geotherm_payload_json = fields.Text(copy=False)
    geotherm_source_url = fields.Char(copy=False)

    @api.model
    def upsert_from_chatbot(self, payload):
        lead_data = payload.get("lead") if isinstance(payload.get("lead"), dict) else {}
        external_id = str(lead_data.get("id") or "").strip()
        if not external_id:
            raise ValueError("lead.id is required")
        contact = lead_data.get("contact") if isinstance(lead_data.get("contact"), dict) else {}
        project = lead_data.get("project") if isinstance(lead_data.get("project"), dict) else {}
        context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
        transcript = lead_data.get("transcript") if isinstance(lead_data.get("transcript"), list) else []
        service_type = str(project.get("serviceType") or "unknown")
        service_label = SERVICE_LABELS.get(service_type, service_type.replace("_", " "))
        area = project.get("areaM2")
        title_parts = ["Chatbot", service_label]
        if area:
            title_parts.append(f"{area:g} m²" if isinstance(area, (int, float)) else f"{area} m²")
        summary = str(lead_data.get("summary") or "").strip()
        description = "\n".join(
            part
            for part in [
                summary,
                f"Ďalší krok: {lead_data.get('nextAction')}" if lead_data.get("nextAction") else None,
                f"Zdroj: {context.get('currentUrl')}" if context.get("currentUrl") else None,
                "\nKonverzácia:\n" + "\n".join(
                    f"{str(item.get('role') or '').upper()}: {str(item.get('content') or '')}"
                    for item in transcript
                    if isinstance(item, dict)
                ) if transcript else None,
            ]
            if part
        )
        values = {
            "name": " - ".join(title_parts),
            "type": "lead",
            "contact_name": contact.get("name") or False,
            "email_from": contact.get("email") or False,
            "phone": contact.get("phone") or False,
            "description": description,
            "geotherm_external_lead_id": external_id,
            "geotherm_conversation_id": lead_data.get("conversationId") or False,
            "geotherm_site_id": payload.get("siteId") or False,
            "geotherm_service_type": service_type,
            "geotherm_service_intent": project.get("serviceIntent") or False,
            "geotherm_project_type": project.get("projectType") or False,
            "geotherm_area_m2": float(area or 0),
            "geotherm_heating_distribution": project.get("heatingDistribution") or False,
            "geotherm_current_heat_source": project.get("currentHeatSource") or False,
            "geotherm_wants_cooling": bool(project.get("wantsCooling")),
            "geotherm_wants_hot_water": bool(project.get("wantsHotWater")),
            "geotherm_lead_score": int(lead_data.get("score") or 0),
            "geotherm_lead_temperature": lead_data.get("temperature") if lead_data.get("temperature") in {"cold", "warm", "hot"} else False,
            "geotherm_chatbot_summary": summary,
            "geotherm_transcript_json": json.dumps(transcript, ensure_ascii=False),
            "geotherm_payload_json": json.dumps(payload, ensure_ascii=False),
            "geotherm_source_url": context.get("currentUrl") or False,
        }
        lead = self.sudo().search([("geotherm_external_lead_id", "=", external_id)], limit=1)
        is_new = not bool(lead)
        if lead:
            lead.write(values)
        else:
            lead = self.sudo().create(values)
        if is_new:
            lead.message_post(body=summary or "Nový lead prijatý z Geotherm chatbota.", subject="Nový lead z webu")
            user_id = self._geotherm_salesperson_id()
            if user_id:
                lead.write({"user_id": user_id})
                lead.activity_schedule(
                    "mail.mail_activity_data_call",
                    user_id=user_id,
                    summary="Zavolať novému leadu z webu",
                    note=summary or "Kontakt bol získaný cez Geotherm chatbot.",
                )
        return lead, is_new

    @api.model
    def _geotherm_salesperson_id(self):
        configured = self.env["ir.config_parameter"].sudo().get_param("geotherm_chatbot.salesperson_id")
        if configured and str(configured).isdigit():
            user = self.env["res.users"].sudo().browse(int(configured)).exists()
            if user:
                return user.id
        return self.env.ref("base.user_admin", raise_if_not_found=False).id

