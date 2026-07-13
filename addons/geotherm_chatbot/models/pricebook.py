import json
import re
import unicodedata

from odoo import api, fields, models


def stable_key(*parts):
    value = "-".join(str(part or "") for part in parts)
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


class GeothermPricebookCatalog(models.Model):
    _name = "geotherm.pricebook.catalog"
    _description = "Geotherm pricebook metadata"

    name = fields.Char(required=True, default="Geotherm chatbot cenník")
    version = fields.Char(required=True, default=lambda self: fields.Datetime.now().isoformat())
    source_file = fields.Char()
    source_notes = fields.Text()
    imported_at = fields.Datetime(default=fields.Datetime.now, required=True)


class GeothermPricebookArea(models.Model):
    _name = "geotherm.pricebook.area"
    _description = "Geotherm service area"
    _order = "name"

    name = fields.Char(required=True, index=True)
    external_key = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    note = fields.Text()

    _external_key_unique = models.Constraint("UNIQUE(external_key)", "Oblasť s týmto kľúčom už existuje.")

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            values.setdefault("external_key", stable_key(values.get("name")))
        return super().create(values_list)

    @api.model
    def _upsert(self, model_name, external_key, values):
        model = self.env[model_name].sudo().with_context(active_test=False)
        model.flush_model(["external_key"])
        record = model.search([("external_key", "=", external_key)], limit=1)
        if record:
            record.write(values)
        else:
            record = model.create({"external_key": external_key, **values})
        return record

    @api.model
    def import_catalog(self, payload, source_file="Odoo ERP", mode="upsert"):
        if not isinstance(payload, dict):
            raise ValueError("Cenník musí byť JSON objekt.")
        if mode == "replace":
            self.sudo().search([]).write({"active": False})
            self.env["geotherm.pricebook.question"].sudo().search([]).write({"active": False})
            self.env["geotherm.pricebook.product"].sudo().search([]).write({"active": False})
            self.env["geotherm.pricebook.addon"].sudo().search([]).write({"active": False})

        area_by_name = {}
        for item in payload.get("areas", []):
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = str(item.get("externalKey") or stable_key(name))
            area_by_name[name] = self._upsert(
                "geotherm.pricebook.area",
                key,
                {"name": name, "active": bool(item.get("active", True)), "note": item.get("note") or False},
            )

        def area_for(name):
            clean_name = str(name or "").strip()
            area = area_by_name.get(clean_name)
            if not area and clean_name:
                area = self.sudo().with_context(active_test=False).search([("name", "=", clean_name)], limit=1)
            if not area and clean_name:
                area = self._upsert("geotherm.pricebook.area", stable_key(clean_name), {"name": clean_name, "active": True})
            return area

        for item in payload.get("questions", []):
            area = area_for(item.get("area"))
            filter_key = str(item.get("filter") or "").strip()
            question = str(item.get("question") or "").strip()
            if not area or not filter_key or not question:
                continue
            key = str(item.get("externalKey") or stable_key(area.name, filter_key))
            allowed = item.get("allowedAnswers") or []
            self._upsert(
                "geotherm.pricebook.question",
                key,
                {
                    "area_id": area.id,
                    "active": bool(item.get("active", True)),
                    "filter_key": filter_key,
                    "label": item.get("label") or filter_key,
                    "question": question,
                    "answer_type": item.get("answerType") or "text",
                    "allowed_answers": "; ".join(str(value) for value in allowed) if isinstance(allowed, list) else str(allowed),
                    "ask_when": item.get("askWhen") or "vždy",
                    "required_before_price": bool(item.get("requiredBeforePrice")),
                    "sequence": int(item.get("order") or 10),
                    "examples": item.get("examples") or False,
                },
            )

        for item in payload.get("products", []):
            area = area_for(item.get("area"))
            name = str(item.get("name") or "").strip()
            brand_model = str(item.get("brandModel") or "").strip()
            if not area or not name:
                continue
            key = str(item.get("externalKey") or stable_key(area.name, name, brand_model))
            valid_from = item.get("validFrom") or False
            self._upsert(
                "geotherm.pricebook.product",
                key,
                {
                    "area_id": area.id,
                    "active": bool(item.get("active", True)),
                    "series": item.get("series") or stable_key(name, brand_model),
                    "name": name,
                    "brand_model": brand_model,
                    "price_eur_with_vat": float(item.get("priceEurWithVat") or 0),
                    "price_type": item.get("priceType") or "individuálne",
                    "fit": item.get("fit") or False,
                    "area_from_m2": item.get("areaFromM2") or False,
                    "area_to_m2": item.get("areaToM2") or False,
                    "occupants_from": item.get("occupantsFrom") or False,
                    "occupants_to": item.get("occupantsTo") or False,
                    "installation_included": bool(item.get("installationIncluded")),
                    "included_text": item.get("included") or False,
                    "do_not_recommend_when": item.get("doNotRecommendWhen") or False,
                    "chatbot_note": item.get("chatbotNote") or False,
                    "valid_from": valid_from,
                    "custom_filters": item.get("customFilters") or {},
                    "price_confidence": item.get("priceConfidence") or False,
                    "source_url": item.get("sourceUrl") or False,
                },
            )

        for item in payload.get("addons", []):
            area = area_for(item.get("area"))
            name = str(item.get("name") or "").strip()
            if not area or not name:
                continue
            key = str(item.get("externalKey") or stable_key(area.name, name))
            self._upsert(
                "geotherm.pricebook.addon",
                key,
                {
                    "area_id": area.id,
                    "active": bool(item.get("active", True)),
                    "name": name,
                    "price_eur_with_vat": float(item.get("priceEurWithVat") or 0),
                    "price_type": item.get("priceType") or "individuálne",
                    "offer_when": item.get("offerWhen") or False,
                    "chatbot_note": item.get("chatbotNote") or False,
                    "price_confidence": item.get("priceConfidence") or False,
                    "source_url": item.get("sourceUrl") or False,
                },
            )

        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        catalog = self.env["geotherm.pricebook.catalog"].sudo().search([], limit=1)
        values = {
            "version": str(payload.get("version") or fields.Datetime.now().isoformat()),
            "source_file": source_file or source.get("file") or "Odoo ERP",
            "source_notes": "\n".join(source.get("notes") or []),
            "imported_at": fields.Datetime.now(),
        }
        if catalog:
            catalog.write(values)
        else:
            self.env["geotherm.pricebook.catalog"].sudo().create(values)
        return {
            "areas": len(payload.get("areas", [])),
            "questions": len(payload.get("questions", [])),
            "products": len(payload.get("products", [])),
            "addons": len(payload.get("addons", [])),
        }

    @api.model
    def export_catalog(self):
        catalog = self.env["geotherm.pricebook.catalog"].sudo().search([], limit=1)
        areas = self.sudo().with_context(active_test=False).search([], order="name")
        questions = self.env["geotherm.pricebook.question"].sudo().with_context(active_test=False).search([], order="area_id, sequence, id")
        products = self.env["geotherm.pricebook.product"].sudo().with_context(active_test=False).search([], order="area_id, price_eur_with_vat, id")
        addons = self.env["geotherm.pricebook.addon"].sudo().with_context(active_test=False).search([], order="area_id, name")
        imported_at = (catalog.imported_at or fields.Datetime.now()).date().isoformat() if catalog else fields.Date.today().isoformat()
        return {
            "version": catalog.version if catalog else fields.Datetime.now().isoformat(),
            "source": {
                "file": catalog.source_file if catalog else "Odoo ERP",
                "importedAt": imported_at,
                "notes": (catalog.source_notes or "").splitlines() if catalog else ["Cenník je spravovaný v Odoo ERP."],
            },
            "areas": [{"name": item.name, "active": item.active} for item in areas],
            "questions": [item.as_chatbot_dict() for item in questions],
            "products": [item.as_chatbot_dict() for item in products],
            "addons": [item.as_chatbot_dict() for item in addons],
        }


class GeothermPricebookQuestion(models.Model):
    _name = "geotherm.pricebook.question"
    _description = "Geotherm chatbot question"
    _order = "area_id, sequence, id"

    external_key = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    area_id = fields.Many2one("geotherm.pricebook.area", required=True, ondelete="cascade", index=True)
    filter_key = fields.Char(required=True)
    label = fields.Char(required=True)
    question = fields.Char(required=True)
    answer_type = fields.Selection(
        [("text", "Text"), ("číslo", "Číslo"), ("výber", "Výber"), ("áno/nie", "Áno/Nie")],
        default="text",
        required=True,
    )
    allowed_answers = fields.Text()
    ask_when = fields.Char(default="vždy")
    required_before_price = fields.Boolean(default=False)
    sequence = fields.Integer(default=10)
    examples = fields.Char()

    _external_key_unique = models.Constraint("UNIQUE(external_key)", "Otázka s týmto kľúčom už existuje.")

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if not values.get("external_key"):
                area = self.env["geotherm.pricebook.area"].browse(values.get("area_id"))
                values["external_key"] = stable_key(area.name, values.get("filter_key"))
        return super().create(values_list)

    def as_chatbot_dict(self):
        self.ensure_one()
        return {
            "active": self.active,
            "area": self.area_id.name,
            "filter": self.filter_key,
            "label": self.label,
            "question": self.question,
            "answerType": self.answer_type,
            "allowedAnswers": [value.strip() for value in (self.allowed_answers or "").split(";") if value.strip()],
            "askWhen": self.ask_when or "vždy",
            "requiredBeforePrice": self.required_before_price,
            "order": self.sequence,
            "examples": self.examples or "",
        }


class GeothermPricebookProduct(models.Model):
    _name = "geotherm.pricebook.product"
    _description = "Geotherm pricebook product"
    _order = "area_id, price_eur_with_vat, id"

    external_key = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    area_id = fields.Many2one("geotherm.pricebook.area", required=True, ondelete="cascade", index=True)
    series = fields.Char(required=True)
    name = fields.Char(required=True)
    brand_model = fields.Char()
    price_eur_with_vat = fields.Float(string="Cena s DPH")
    price_type = fields.Char(default="balík")
    fit = fields.Text(string="Vhodné pre")
    area_from_m2 = fields.Float()
    area_to_m2 = fields.Float()
    occupants_from = fields.Integer()
    occupants_to = fields.Integer()
    installation_included = fields.Boolean()
    included_text = fields.Text(string="Čo je v cene")
    do_not_recommend_when = fields.Text()
    chatbot_note = fields.Text()
    valid_from = fields.Date()
    custom_filters = fields.Json(default=dict)
    price_confidence = fields.Char()
    source_url = fields.Char()

    _external_key_unique = models.Constraint("UNIQUE(external_key)", "Produkt s týmto kľúčom už existuje.")

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            area = self.env["geotherm.pricebook.area"].browse(values.get("area_id"))
            values.setdefault("series", stable_key(values.get("name"), values.get("brand_model")))
            values.setdefault("external_key", stable_key(area.name, values.get("name"), values.get("brand_model")))
        return super().create(values_list)

    def as_chatbot_dict(self):
        self.ensure_one()
        return {
            "active": self.active,
            "area": self.area_id.name,
            "series": self.series,
            "name": self.name,
            "brandModel": self.brand_model or "",
            "priceEurWithVat": self.price_eur_with_vat,
            "priceType": self.price_type or "individuálne",
            "fit": self.fit or "",
            "areaFromM2": self.area_from_m2 or None,
            "areaToM2": self.area_to_m2 or None,
            "occupantsFrom": self.occupants_from or None,
            "occupantsTo": self.occupants_to or None,
            "installationIncluded": self.installation_included,
            "included": self.included_text or "",
            "doNotRecommendWhen": self.do_not_recommend_when or "",
            "chatbotNote": self.chatbot_note or "",
            "validFrom": self.valid_from.isoformat() if self.valid_from else fields.Date.today().isoformat(),
            "customFilters": self.custom_filters or {},
            "priceConfidence": self.price_confidence or "Odoo ERP",
            "sourceUrl": self.source_url or "",
        }


class GeothermPricebookAddon(models.Model):
    _name = "geotherm.pricebook.addon"
    _description = "Geotherm pricebook addon"
    _order = "area_id, name"

    external_key = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)
    area_id = fields.Many2one("geotherm.pricebook.area", required=True, ondelete="cascade", index=True)
    name = fields.Char(required=True)
    price_eur_with_vat = fields.Float(string="Cena s DPH")
    price_type = fields.Char(default="za ks")
    offer_when = fields.Text()
    chatbot_note = fields.Text()
    price_confidence = fields.Char()
    source_url = fields.Char()

    _external_key_unique = models.Constraint("UNIQUE(external_key)", "Doplnok s týmto kľúčom už existuje.")

    @api.model_create_multi
    def create(self, values_list):
        for values in values_list:
            if not values.get("external_key"):
                area = self.env["geotherm.pricebook.area"].browse(values.get("area_id"))
                values["external_key"] = stable_key(area.name, values.get("name"))
        return super().create(values_list)

    def as_chatbot_dict(self):
        self.ensure_one()
        return {
            "active": self.active,
            "area": self.area_id.name,
            "name": self.name,
            "priceEurWithVat": self.price_eur_with_vat,
            "priceType": self.price_type or "individuálne",
            "offerWhen": self.offer_when or "",
            "chatbotNote": self.chatbot_note or "",
            "priceConfidence": self.price_confidence or "Odoo ERP",
            "sourceUrl": self.source_url or "",
        }


class GeothermPricebookImport(models.Model):
    _name = "geotherm.pricebook.import"
    _description = "Geotherm pricebook import log"
    _order = "create_date desc"

    name = fields.Char(required=True)
    mode = fields.Selection([("upsert", "Doplniť/aktualizovať"), ("replace", "Nahradiť cenník")], required=True)
    state = fields.Selection([("done", "Hotovo"), ("failed", "Chyba")], required=True)
    result_summary = fields.Text()
    attachment_id = fields.Many2one("ir.attachment", ondelete="set null")
