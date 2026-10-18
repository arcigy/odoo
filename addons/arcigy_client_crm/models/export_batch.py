import hashlib
import json
import re
from datetime import datetime, timezone

from odoo import api, fields, models
from odoo.exceptions import AccessError

from .client_metrics import DAILY_COSTS, DAILY_COUNTERS, DAILY_SNAPSHOTS, MONTHLY_FIELDS, USER_MONTHLY_COUNTERS

SCHEMA_VERSION = 1
BATCH_ID = re.compile(r"^[a-z0-9_-]{1,20}:\d{4}-\d{2}-\d{2}\.\.\d{4}-\d{2}-\d{2}:[0-9a-f]{16}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
APP_ROLES = {"administrator", "team_member", "observer"}
USER_STATES = {"active_30d", "inactive", "deactivated"}
MAX_CLIENTS = 500
MAX_DAYS = 400


class BatchRejected(Exception):
    """The batch is structurally invalid; it is recorded as rejected and never applied."""


class ArcigyClientExportBatch(models.Model):
    _name = "arcigy.client.export.batch"
    _description = "Arcigy daily client metrics batch"
    _order = "received_at desc, id desc"

    name = fields.Char(string="Dávka", required=True, readonly=True, index=True)
    environment = fields.Char(string="Prostredie", readonly=True)
    schema_version = fields.Integer(string="Verzia formátu", readonly=True)
    date_from = fields.Date(string="Od", readonly=True)
    date_to = fields.Date(string="Do", readonly=True)
    generated_at = fields.Datetime(string="Vytvorená v aplikácii", readonly=True)
    received_at = fields.Datetime(string="Prijatá", readonly=True, default=fields.Datetime.now)
    payload_sha256 = fields.Char(string="SHA-256", readonly=True)
    state = fields.Selection(
        [("applied", "Zapísaná"), ("applied_with_unmatched", "Zapísaná, niektorí klienti nespárovaní"), ("rejected", "Odmietnutá")],
        string="Stav", readonly=True, index=True,
    )
    unmatched_keys = fields.Char(string="Nespárované kľúče klientov", readonly=True)
    rows_written = fields.Integer(string="Zapísané riadky", readonly=True)
    error = fields.Text(string="Chyba", readonly=True)

    _name_unique = models.Constraint("UNIQUE(name)", "Dávka s týmto ID už bola prijatá.")

    @api.model
    def arcigy_ingest(self, payload):
        """JSON-2 entry point for the app's daily export; only the ingest group may call it."""
        if not (self.env.user.has_group("arcigy_client_crm.group_crm_ingest") or self.env.is_system()):
            raise AccessError("Only the Arcigy CRM ingest user may send client metrics.")
        return self.sudo()._arcigy_ingest(payload)

    def _arcigy_ingest(self, payload):
        batch_id = payload.get("batch_id") if isinstance(payload, dict) else None
        existing = self.search([("name", "=", batch_id)], limit=1) if isinstance(batch_id, str) else self.browse()
        if existing:
            return {"ok": existing.state != "rejected", "state": existing.state, "replayed": True}
        sha = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            self._arcigy_validate(payload)
        except BatchRejected as error:
            self.create({
                "name": batch_id if isinstance(batch_id, str) and batch_id else f"rejected:{sha[:16]}",
                "state": "rejected", "error": str(error), "payload_sha256": sha,
                "environment": str(payload.get("environment", ""))[:40] if isinstance(payload, dict) else False,
            })
            return {"ok": False, "state": "rejected", "error": str(error)}

        generated_at = _parse_datetime(payload["generated_at"])
        partners = self.env["res.partner"].search([("arcigy_client_key", "in", [c["client_key"] for c in payload["clients"]])])
        by_key = {partner.arcigy_client_key: partner for partner in partners}
        unmatched = sorted({c["client_key"] for c in payload["clients"]} - set(by_key))
        rows = 0
        for client in payload["clients"]:
            partner = by_key.get(client["client_key"])
            if not partner:
                continue  # Unmatched clients are reported, never stored: link the company first.
            contacts = self._arcigy_sync_users(partner, client["users"])
            rows += self._arcigy_upsert_daily(partner, client["daily"], generated_at)
            months = self._arcigy_upsert_monthly(partner, client["monthly"], generated_at)
            rows += len(months)
            rows += self._arcigy_upsert_user_monthly(contacts, client["user_monthly"], generated_at)
            months._refresh_from_daily()
            partner._arcigy_refresh_metric_summary()
        batch = self.create({
            "name": payload["batch_id"],
            "environment": payload["environment"],
            "schema_version": payload["schema_version"],
            "date_from": payload["date_from"],
            "date_to": payload["date_to"],
            "generated_at": generated_at,
            "payload_sha256": sha,
            "state": "applied_with_unmatched" if unmatched else "applied",
            "unmatched_keys": ", ".join(unmatched) or False,
            "rows_written": rows,
        })
        return {"ok": True, "state": batch.state, "unmatched": unmatched, "rows": rows}

    def _arcigy_validate(self, payload):
        def require(condition, message):
            if not condition:
                raise BatchRejected(message)

        require(isinstance(payload, dict), "payload must be an object")
        require(payload.get("schema_version") == SCHEMA_VERSION, f"schema_version {payload.get('schema_version')} is not supported")
        accepted = self.env["ir.config_parameter"].sudo().get_param("arcigy_client_crm.accepted_environment", "prod")
        require(payload.get("environment") == accepted, f"environment {payload.get('environment')!r} is not accepted here ({accepted!r})")
        require(isinstance(payload.get("batch_id"), str) and BATCH_ID.match(payload["batch_id"]), "invalid batch_id")
        for key in ("date_from", "date_to"):
            require(isinstance(payload.get(key), str) and DATE.match(payload[key]), f"invalid {key}")
        require(payload["date_from"] <= payload["date_to"], "date_from is after date_to")
        require(isinstance(payload.get("generated_at"), str), "missing generated_at")
        try:
            _parse_datetime(payload["generated_at"])
        except ValueError:
            raise BatchRejected("invalid generated_at") from None
        clients = payload.get("clients")
        require(isinstance(clients, list) and len(clients) <= MAX_CLIENTS, "clients must be a list")
        totals = {"projects_created": 0, "offers_sent": 0, "ai_cost_usd": 0.0, "ai_calls": 0}
        for client in clients:
            require(isinstance(client, dict) and isinstance(client.get("client_key"), str), "client without client_key")
            for section in ("users", "daily", "monthly", "user_monthly"):
                require(isinstance(client.get(section), list), f"{client['client_key']}: {section} must be a list")
            require(len(client["daily"]) <= MAX_DAYS, f"{client['client_key']}: too many days")
            for user in client["users"]:
                require(isinstance(user, dict) and isinstance(user.get("user_key"), str)
                        and user["user_key"].startswith(client["client_key"] + ":"), "user_key must belong to its client")
                require(user.get("app_role") in APP_ROLES and user.get("state") in USER_STATES, f"{user.get('user_key')}: invalid role or state")
            for row in client["daily"]:
                require(isinstance(row, dict) and isinstance(row.get("date"), str) and DATE.match(row["date"]), "daily row without date")
                for name in DAILY_COUNTERS + DAILY_SNAPSHOTS:
                    require(isinstance(row.get(name), int), f"{row['date']}: {name} must be an integer")
                for name in DAILY_COSTS:
                    require(isinstance(row.get(name), (int, float)) and row[name] >= 0, f"{row['date']}: {name} must be a non-negative number")
                totals["projects_created"] += row["projects_created"]
                totals["offers_sent"] += row["offers_sent"]
                totals["ai_cost_usd"] += sum(row[name] for name in DAILY_COSTS)
                totals["ai_calls"] += row["ai_calls"]
            for row in client["monthly"]:
                require(isinstance(row, dict) and isinstance(row.get("month"), str) and DATE.match(row["month"]), "monthly row without month")
            for row in client["user_monthly"]:
                require(isinstance(row, dict) and isinstance(row.get("user_key"), str) and DATE.match(str(row.get("month", ""))),
                        "user_monthly row without user_key or month")
        control = payload.get("control_totals")
        require(isinstance(control, dict), "missing control_totals")
        for key in ("projects_created", "offers_sent", "ai_calls"):
            require(control.get(key) == totals[key], f"control total {key} does not match the rows ({control.get(key)} != {totals[key]})")
        require(abs(float(control.get("ai_cost_usd", -1)) - totals["ai_cost_usd"]) < 0.000005, "control total ai_cost_usd does not match the rows")

    def _arcigy_sync_users(self, company, users):
        """App users become contacts of the client company; users that left the app are deactivated."""
        Partner = self.env["res.partner"].with_context(tracking_disable=True)
        existing = Partner.search([("parent_id", "=", company.id), ("arcigy_user_key", "!=", False)])
        by_key = {contact.arcigy_user_key: contact for contact in existing}
        seen = set()
        for user in users:
            values = {
                "name": user.get("name") or user["user_key"],
                "function": user.get("position") or False,
                "arcigy_app_role": user["app_role"],
                "arcigy_app_user_state": user["state"],
            }
            contact = by_key.get(user["user_key"])
            if contact:
                changed = {key: value for key, value in values.items() if contact[key] != value}
                if changed:
                    contact.write(changed)
            else:
                contact = Partner.create({**values, "parent_id": company.id, "type": "contact", "arcigy_user_key": user["user_key"]})
                by_key[user["user_key"]] = contact
            seen.add(user["user_key"])
        gone = existing.filtered(lambda contact: contact.arcigy_user_key not in seen and contact.arcigy_app_user_state != "deactivated")
        if gone:
            gone.write({"arcigy_app_user_state": "deactivated"})
        return by_key

    def _arcigy_upsert(self, model, domain_keys, rows, values_for, generated_at):
        """Writes rows unless a newer batch already wrote them; returns the written records."""
        Model = self.env[model]
        written = Model.browse()
        for row in rows:
            domain = [(key, "=", value) for key, value in domain_keys(row)]
            record = Model.search(domain, limit=1)
            values = {**values_for(row), "batch_generated_at": generated_at}
            if record:
                if record.batch_generated_at and record.batch_generated_at > generated_at:
                    continue
                record.write(values)
            else:
                record = Model.create({**dict(domain_keys(row)), **values})
            written |= record
        return written

    def _arcigy_upsert_daily(self, partner, rows, generated_at):
        names = DAILY_COUNTERS + DAILY_SNAPSHOTS + DAILY_COSTS
        return len(self._arcigy_upsert(
            "arcigy.client.metric.daily",
            lambda row: [("partner_id", partner.id), ("date", row["date"])],
            rows, lambda row: {name: row[name] for name in names}, generated_at))

    def _arcigy_upsert_monthly(self, partner, rows, generated_at):
        return self._arcigy_upsert(
            "arcigy.client.metric.monthly",
            lambda row: [("partner_id", partner.id), ("month", row["month"])],
            rows, lambda row: {name: row.get(name, 0) for name in MONTHLY_FIELDS}, generated_at)

    def _arcigy_upsert_user_monthly(self, contacts, rows, generated_at):
        known = [row for row in rows if row["user_key"] in contacts]
        return len(self._arcigy_upsert(
            "arcigy.client.user.monthly",
            lambda row: [("contact_id", contacts[row["user_key"]].id), ("month", row["month"])],
            known, lambda row: {name: row.get(name, 0) for name in USER_MONTHLY_COUNTERS}, generated_at))


def _parse_datetime(value):
    """ISO 8601 from the app (UTC, with Z) to the naive UTC datetime Odoo stores."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed
