import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


SCOPE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")

OPERATIONAL_ALLOWED_FIELDS = {
    "saas.data.quality.run": {
        "name", "started_at", "finished_at", "status", "events_sent", "events_received",
        "events_processed", "events_rejected", "duplicate_count", "schema_failure_count",
        "missing_field_count", "late_event_count", "unknown_tenant_count",
        "reconciliation_difference", "oldest_unsynced_at", "drilldown_url",
    },
    "saas.backup.run": {
        "name", "started_at", "finished_at", "status", "backup_type", "size_bytes",
        "checksum", "encrypted", "off_host", "drilldown_url",
    },
    "saas.restore.test": {
        "name", "started_at", "finished_at", "status", "actual_rpo_seconds",
        "actual_rto_seconds", "checksum_valid", "application_smoke_passed",
        "tenant_isolation_passed", "evidence_url",
    },
    "saas.load.test": {
        "name", "started_at", "finished_at", "test_type", "status", "concurrent_users",
        "requests_per_second", "p95_seconds", "p99_seconds", "error_rate",
        "recovery_seconds", "evidence_url",
    },
}


def _operation_datetime(value, field_name, required=False):
    if not value:
        if required:
            raise ValidationError(f"{field_name} is required.")
        return False
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _operation_text(value, field_name, maximum=1024):
    normalized = str(value or "").strip()
    if len(normalized) > maximum or any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        raise ValidationError(f"{field_name} is invalid or too long.")
    return normalized or False


class SaasOperationalIngestMixin(models.AbstractModel):
    _name = "saas.operational.ingest.mixin"
    _description = "Shared safe operational ingest"

    external_key = fields.Char(required=True, index=True)
    source_updated_at = fields.Datetime(required=True, default=fields.Datetime.now)

    _external_key_unique = models.Constraint(
        "UNIQUE(external_key)", "Operational external key must be unique."
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records._sync_operational_alerts()
        return records

    def write(self, values):
        result = super().write(values)
        self._sync_operational_alerts()
        return result

    def _sync_operational_alerts(self):
        alert_model = self.env["saas.alert"].sudo()
        administrator = self.env.ref("base.user_admin").sudo()
        base_url = self.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", "http://localhost"
        ).rstrip("/")
        for record in self:
            severity = False
            unhealthy = False
            if record._name in {"saas.backup.run", "saas.restore.test"}:
                unhealthy = record.status == "failed"
                severity = "p1"
                recovered = record.status == "success"
            elif record._name == "saas.load.test":
                unhealthy = record.status in {"not_ready", "ready_with_risk"}
                severity = "p2"
                recovered = record.status == "ready"
            elif record._name == "saas.data.quality.run":
                unhealthy = record.status in {"warning", "invalid"}
                severity = "p1" if record.status == "invalid" else "p2"
                recovered = record.status == "valid"
            else:
                recovered = False
            scope_key = record._name
            open_alerts = alert_model.search(
                [
                    ("environment_id", "=", record.environment_id.id),
                    ("scope_key", "=", scope_key),
                    ("metric_id", "=", False),
                    ("status", "!=", "resolved"),
                ]
            )
            if recovered and open_alerts:
                open_alerts.write({"status": "resolved", "resolved_at": fields.Datetime.now()})
            if not unhealthy:
                continue
            record_url = f"{base_url}/web#id={record.id}&model={record._name}&view_type=form"
            values = {
                "name": f"{record._description}: {record.status} in {record.environment_id.name}",
                "severity": severity,
                "environment_id": record.environment_id.id,
                "scope_key": scope_key,
                "current_value": 0,
                "threshold": 1,
                "owner_id": administrator.id,
                "runbook_url": record_url,
                "drilldown_url": record_url,
                "recovery_condition": f"A newer {record._description} record reports success/ready.",
            }
            if open_alerts:
                open_alerts[:1].write(values)
            else:
                values.update(
                    {
                        "status": "open",
                        "detected_at": record.source_updated_at,
                        "deduplication_key": f"{record.external_key}:operational-alert"[:255],
                    }
                )
                alert_model.create(values)

    @api.constrains("external_key", "environment_id")
    def _check_external_key_environment(self):
        for record in self:
            if len(record.external_key or "") > 255 or not record.external_key.startswith(
                f"{record.environment_id.code}:"
            ):
                raise ValidationError("Operational external key must be environment-prefixed.")

    @api.model
    def ingest_operational_batch(self, payload):
        if not (
            self.env.user.has_group("arcigy_saas_control_center.group_saas_integration_bot")
            or self.env.user.has_group("arcigy_saas_control_center.group_saas_administrator")
        ):
            raise AccessError("Only the SaaS integration bot can ingest operational records.")
        allowed_fields = OPERATIONAL_ALLOWED_FIELDS.get(self._name)
        if not allowed_fields:
            raise ValidationError("This model is not an approved operational ingest target.")
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object.")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 200:
            raise ValidationError("items must contain between 1 and 200 records.")
        environment_code = str(payload.get("environment") or "").strip().lower()
        if environment_code not in {"develop", "main"}:
            raise ValidationError("environment must be develop or main.")
        model = self.sudo()
        environment = model.env["saas.environment"].search(
            [("code", "=", environment_code)], limit=1
        )
        if not environment:
            raise ValidationError("Configured SaaS environment was not found.")
        source_updated_at = _operation_datetime(
            payload.get("source_updated_at"), "source_updated_at", required=True
        )
        created = updated = 0
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValidationError("Every operational item must be an object.")
            unknown = set(raw_item) - allowed_fields - {"external_key", "release_version"}
            if unknown:
                raise ValidationError(f"Unsupported operational fields: {', '.join(sorted(unknown))}.")
            external_key = _operation_text(raw_item.get("external_key"), "external_key", 255)
            if not external_key or not external_key.startswith(f"{environment_code}:"):
                raise ValidationError("external_key must be environment-prefixed.")
            values = {
                "environment_id": environment.id,
                "external_key": external_key,
                "source_updated_at": source_updated_at,
            }
            release_version = _operation_text(raw_item.get("release_version"), "release_version", 128)
            if release_version and "release_id" in model._fields:
                release = model.env["saas.release"].search(
                    [
                        ("environment_id", "=", environment.id),
                        ("version", "=", release_version),
                    ],
                    limit=1,
                )
                if not release:
                    raise ValidationError("Unknown release_version for this environment.")
                values["release_id"] = release.id
            for field_name in allowed_fields:
                if field_name not in raw_item:
                    continue
                field = model._fields[field_name]
                value = raw_item[field_name]
                if field.type == "datetime":
                    values[field_name] = _operation_datetime(
                        value, field_name, required=field.required
                    )
                elif field.type in {"integer", "float", "monetary"}:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValidationError(f"{field_name} must be numeric.")
                    numeric = float(value)
                    if not math.isfinite(numeric):
                        raise ValidationError(f"{field_name} must be finite.")
                    if field.type == "integer" and not numeric.is_integer():
                        raise ValidationError(f"{field_name} must be an integer.")
                    values[field_name] = int(numeric) if field.type == "integer" else numeric
                elif field.type == "boolean":
                    if not isinstance(value, bool):
                        raise ValidationError(f"{field_name} must be boolean.")
                    values[field_name] = value
                elif field.type in {"char", "text", "selection"}:
                    values[field_name] = _operation_text(value, field_name)
                else:
                    raise ValidationError(f"{field_name} cannot be ingested directly.")
            existing = model.search([("external_key", "=", external_key)], limit=1)
            if existing:
                if existing.environment_id != environment:
                    raise ValidationError("external_key belongs to another environment.")
                existing.write(values)
                updated += 1
            else:
                model.create(values)
                created += 1
        return {
            "ok": True,
            "model": model._name,
            "environment": environment_code,
            "created": created,
            "updated": updated,
            "processed": len(raw_items),
        }


def _validate_http_urls(records, field_names):
    for record in records:
        for field_name in field_names:
            value = (record[field_name] or "").strip()
            if not value:
                continue
            parsed = urlparse(value)
            if (
                len(value) > 1024
                or parsed.scheme not in {"https", "http"}
                or not parsed.netloc
                or parsed.username
                or parsed.password
            ):
                raise ValidationError(f"{field_name} must be an http(s) URL without credentials.")


class SaasAlert(models.Model):
    _name = "saas.alert"
    _description = "SaaS alert"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "detected_at desc, id desc"

    name = fields.Char(required=True, tracking=True)
    severity = fields.Selection(
        [("p0", "P0"), ("p1", "P1"), ("p2", "P2"), ("p3", "P3")],
        required=True,
        index=True,
        tracking=True,
    )
    status = fields.Selection(
        [("open", "Open"), ("acknowledged", "Acknowledged"), ("resolved", "Resolved")],
        required=True,
        default="open",
        index=True,
        tracking=True,
    )
    metric_id = fields.Many2one("saas.metric.definition", ondelete="restrict", index=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    service_id = fields.Many2one("saas.service", ondelete="restrict", index=True)
    tenant_id = fields.Many2one("saas.tenant", ondelete="restrict", index=True)
    release_id = fields.Many2one("saas.release", ondelete="restrict", index=True)
    scope_key = fields.Char(required=True, default="global", index=True)
    current_value = fields.Float()
    threshold = fields.Float()
    detected_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    acknowledged_at = fields.Datetime()
    resolved_at = fields.Datetime()
    owner_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)
    runbook_url = fields.Char(required=True)
    drilldown_url = fields.Char(required=True)
    recovery_condition = fields.Char(required=True)
    deduplication_key = fields.Char(required=True, index=True)
    incident_id = fields.Many2one("saas.incident", ondelete="restrict", readonly=True)

    _deduplication_key_unique = models.Constraint(
        "UNIQUE(deduplication_key)", "Alert deduplication key must be unique."
    )

    @api.constrains("runbook_url", "drilldown_url")
    def _check_urls(self):
        _validate_http_urls(self, ("runbook_url", "drilldown_url"))

    @api.constrains("scope_key")
    def _check_scope_key(self):
        for record in self:
            if not SCOPE_KEY_PATTERN.fullmatch(record.scope_key or ""):
                raise ValidationError("Alert scope key contains unsupported characters.")

    @api.model_create_multi
    def create(self, vals_list):
        alerts = super().create(vals_list)
        for alert in alerts.filtered(lambda item: item.severity in {"p0", "p1"}):
            incident = self.env["saas.incident"].create(
                {
                    "name": alert.name,
                    "severity": alert.severity,
                    "status": "open",
                    "detected_at": alert.detected_at,
                    "environment_id": alert.environment_id.id,
                    "service_ids": [(6, 0, alert.service_id.ids)],
                    "tenant_ids": [(6, 0, alert.tenant_id.ids)],
                    "release_id": alert.release_id.id or False,
                    "triggering_metric_id": alert.metric_id.id or False,
                    "alert_id": alert.id,
                    "runbook_url": alert.runbook_url,
                    "external_dashboard_url": alert.drilldown_url,
                    "owner_id": alert.owner_id.id,
                }
            )
            super(SaasAlert, alert).write({"incident_id": incident.id})
            incident.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=alert.owner_id.id,
                summary=f"{alert.severity.upper()}: {alert.name}",
                note=f"Runbook: {alert.runbook_url}",
            )
        return alerts

    def write(self, values):
        values = dict(values)
        if values.get("status") == "acknowledged" and not values.get("acknowledged_at"):
            values["acknowledged_at"] = fields.Datetime.now()
        if values.get("status") == "resolved" and not values.get("resolved_at"):
            values["resolved_at"] = fields.Datetime.now()
        result = super().write(values)
        for alert in self.filtered("incident_id"):
            if values.get("status") == "acknowledged" and alert.incident_id.status == "open":
                alert.incident_id.write(
                    {"status": "acknowledged", "acknowledged_at": alert.acknowledged_at}
                )
            elif values.get("status") == "resolved" and alert.incident_id.status != "resolved":
                alert.incident_id.write(
                    {"status": "mitigated", "mitigated_at": alert.resolved_at}
                )
        return result


class SaasIncident(models.Model):
    _name = "saas.incident"
    _description = "SaaS incident"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "detected_at desc, id desc"

    name = fields.Char(required=True, tracking=True)
    severity = fields.Selection(
        [("p0", "P0"), ("p1", "P1"), ("p2", "P2"), ("p3", "P3")],
        required=True,
        index=True,
        tracking=True,
    )
    status = fields.Selection(
        [
            ("open", "Open"),
            ("acknowledged", "Acknowledged"),
            ("mitigated", "Mitigated"),
            ("resolved", "Resolved"),
        ],
        required=True,
        default="open",
        index=True,
        tracking=True,
    )
    detected_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    acknowledged_at = fields.Datetime()
    mitigated_at = fields.Datetime()
    resolved_at = fields.Datetime()
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    service_ids = fields.Many2many("saas.service", string="Affected services")
    tenant_ids = fields.Many2many("saas.tenant", string="Affected tenants")
    affected_users = fields.Integer()
    affected_mrr = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency", required=True, default=lambda self: self.env.company.currency_id
    )
    release_id = fields.Many2one("saas.release", ondelete="restrict", index=True)
    root_cause = fields.Text()
    triggering_metric_id = fields.Many2one("saas.metric.definition", ondelete="restrict")
    alert_id = fields.Many2one("saas.alert", ondelete="restrict")
    runbook_url = fields.Char(required=True)
    external_dashboard_url = fields.Char()
    postmortem_url = fields.Char()
    owner_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)
    postmortem_action_ids = fields.One2many(
        "saas.postmortem.action", "incident_id", string="Postmortem actions"
    )

    @api.constrains("runbook_url", "external_dashboard_url", "postmortem_url")
    def _check_urls(self):
        _validate_http_urls(
            self, ("runbook_url", "external_dashboard_url", "postmortem_url")
        )

    @api.constrains("status", "root_cause", "postmortem_action_ids")
    def _check_critical_closure(self):
        for record in self:
            if record.status == "resolved" and record.severity in {"p0", "p1"}:
                if not (record.root_cause or "").strip():
                    raise ValidationError("P0/P1 incidents require a root cause before resolution.")
                if not record.postmortem_action_ids:
                    raise ValidationError("P0/P1 incidents require at least one postmortem action.")


class SaasPostmortemAction(models.Model):
    _name = "saas.postmortem.action"
    _description = "SaaS postmortem action"
    _order = "deadline, id"

    name = fields.Char(required=True)
    incident_id = fields.Many2one(
        "saas.incident", required=True, ondelete="cascade", index=True
    )
    owner_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)
    deadline = fields.Date(required=True)
    status = fields.Selection(
        [("open", "Open"), ("in_progress", "In progress"), ("done", "Done"), ("cancelled", "Cancelled")],
        required=True,
        default="open",
        index=True,
    )
    verification = fields.Text()


class SaasBackupRun(models.Model):
    _name = "saas.backup.run"
    _inherit = "saas.operational.ingest.mixin"
    _description = "SaaS backup run"
    _order = "started_at desc, id desc"

    name = fields.Char(required=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    started_at = fields.Datetime(required=True, index=True)
    finished_at = fields.Datetime()
    status = fields.Selection(
        [("running", "Running"), ("success", "Success"), ("failed", "Failed")],
        required=True,
        index=True,
    )
    backup_type = fields.Selection(
        [("full", "Full"), ("incremental", "Incremental"), ("pitr", "PITR")],
        required=True,
    )
    size_bytes = fields.Integer()
    checksum = fields.Char()
    encrypted = fields.Boolean()
    off_host = fields.Boolean()
    drilldown_url = fields.Char()

    @api.constrains("drilldown_url")
    def _check_drilldown_url(self):
        _validate_http_urls(self, ("drilldown_url",))


class SaasRestoreTest(models.Model):
    _name = "saas.restore.test"
    _inherit = "saas.operational.ingest.mixin"
    _description = "SaaS restore test"
    _order = "started_at desc, id desc"

    name = fields.Char(required=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    started_at = fields.Datetime(required=True, index=True)
    finished_at = fields.Datetime()
    status = fields.Selection(
        [("running", "Running"), ("success", "Success"), ("failed", "Failed")],
        required=True,
        index=True,
    )
    actual_rpo_seconds = fields.Integer()
    actual_rto_seconds = fields.Integer()
    checksum_valid = fields.Boolean()
    application_smoke_passed = fields.Boolean()
    tenant_isolation_passed = fields.Boolean()
    evidence_url = fields.Char()
    owner_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)

    @api.constrains("evidence_url")
    def _check_evidence_url(self):
        _validate_http_urls(self, ("evidence_url",))


class SaasLoadTest(models.Model):
    _name = "saas.load.test"
    _inherit = "saas.operational.ingest.mixin"
    _description = "SaaS load test"
    _order = "started_at desc, id desc"

    name = fields.Char(required=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    release_id = fields.Many2one("saas.release", ondelete="restrict")
    started_at = fields.Datetime(required=True, index=True)
    finished_at = fields.Datetime()
    test_type = fields.Selection(
        [
            ("baseline", "Baseline"),
            ("ramp", "Ramp"),
            ("hold", "Hold"),
            ("spike", "Spike"),
            ("stress", "Stress"),
            ("soak", "Soak"),
            ("failure", "Failure recovery"),
        ],
        required=True,
    )
    status = fields.Selection(
        [
            ("ready", "Ready"),
            ("ready_with_risk", "Ready with risk"),
            ("not_ready", "Not ready"),
            ("test_stale", "Test stale"),
        ],
        required=True,
    )
    concurrent_users = fields.Integer()
    requests_per_second = fields.Float()
    p95_seconds = fields.Float()
    p99_seconds = fields.Float()
    error_rate = fields.Float()
    recovery_seconds = fields.Float()
    evidence_url = fields.Char()

    @api.constrains("evidence_url")
    def _check_evidence_url(self):
        _validate_http_urls(self, ("evidence_url",))


class SaasSyncRun(models.Model):
    _name = "saas.sync.run"
    _description = "SaaS Odoo synchronization run"
    _order = "started_at desc, id desc"

    name = fields.Char(required=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    started_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    finished_at = fields.Datetime()
    status = fields.Selection(
        [("running", "Running"), ("success", "Success"), ("partial", "Partial"), ("failed", "Failed")],
        required=True,
        default="running",
        index=True,
    )
    records_read = fields.Integer()
    records_created = fields.Integer()
    records_updated = fields.Integer()
    records_skipped = fields.Integer()
    records_rejected = fields.Integer()
    retry_count = fields.Integer()
    oldest_unsynced_at = fields.Datetime()
    error_code = fields.Char()
    drilldown_url = fields.Char()
