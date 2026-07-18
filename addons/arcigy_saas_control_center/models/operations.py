import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


SCOPE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")

OPERATIONAL_ALLOWED_FIELDS = {
    "saas.data.quality.run": {
        "name", "started_at", "finished_at", "status", "events_sent", "events_received",
        "events_processed", "events_rejected", "event_stream_complete",
        "retry_adjustment_count", "duplicate_count", "schema_failure_count",
        "missing_field_count", "late_event_count", "unknown_tenant_count",
        "clock_skew_seconds", "processing_lag_p95_seconds", "dead_letter_count",
        "metric_quality_contract_complete", "eligible_metric_count",
        "fresh_metric_count", "complete_metric_count", "unique_metric_count",
        "valid_metric_count", "consistent_metric_count", "outlier_count",
        "unexpected_zero_count", "unexpected_volume_spike_count",
        "numerator_denominator_violation_count", "negative_value_violation_count",
        "missing_dimension_count",
        "reconciliation_difference", "oldest_unsynced_at", "drilldown_url",
    },
    "saas.backup.run": {
        "name", "started_at", "finished_at", "status", "backup_type", "size_bytes",
        "checksum", "encrypted", "off_host", "backup_contract_complete",
        "failure_count_24h", "snapshot_count", "pitr_enabled", "pitr_window_seconds",
        "wal_archive_status", "secondary_copy_status", "storage_cost_monthly_eur",
        "drilldown_url",
    },
    "saas.restore.test": {
        "name", "started_at", "finished_at", "status", "actual_rpo_seconds",
        "actual_rto_seconds", "rpo_measured", "rto_measured", "checksum_valid",
        "application_smoke_passed", "tenant_isolation_passed", "restore_contract_complete",
        "missing_record_count", "owner_team", "next_test_at", "evidence_url",
    },
    "saas.dr.drill": {
        "name", "started_at", "finished_at", "status", "dr_contract_complete",
        "failover_duration_seconds", "failback_duration_seconds",
        "dns_propagation_duration_seconds", "unavailable_dependency_count",
        "runbook_accuracy_rate", "open_remediation_action_count", "owner_team",
        "next_drill_at", "evidence_url",
    },
    "saas.load.test": {
        "name", "started_at", "finished_at", "test_type", "status", "concurrent_users",
        "requests_per_second", "p95_seconds", "p99_seconds", "error_rate",
        "recovery_seconds", "representative", "architecture_version", "evidence_url",
    },
}

SYNC_RUN_ALLOWED_FIELDS = {
    "name", "started_at", "finished_at", "status", "sync_contract_complete",
    "records_read", "records_created", "records_updated", "records_skipped",
    "records_rejected", "duplicate_upsert_count", "api_error_count",
    "authentication_error_count", "permission_error_count", "rate_limit_error_count",
    "retry_count", "backlog_count", "oldest_unsynced_at", "error_code", "drilldown_url",
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

    _complete_contract_marker = False
    _complete_contract_fields = set()
    _complete_contract_label = "operational"

    external_key = fields.Char(required=True, index=True)
    source_updated_at = fields.Datetime(required=True, default=fields.Datetime.now)

    _external_key_unique = models.Constraint(
        "UNIQUE(external_key)", "Operational external key must be unique."
    )

    @api.model_create_multi
    def create(self, vals_list):
        marker = self._complete_contract_marker
        if marker:
            for values in vals_list:
                if values.get(marker) is True:
                    missing = self._complete_contract_fields - set(values)
                    if missing:
                        raise ValidationError(
                            f"Complete {self._complete_contract_label} evidence requires every "
                            f"contract field: {', '.join(sorted(missing))}."
                        )
        records = super().create(vals_list)
        records._sync_operational_alerts()
        return records

    def write(self, values):
        marker = self._complete_contract_marker
        if marker and values.get(marker) is True:
            for record in self.filtered(lambda item: not item[marker]):
                missing = self._complete_contract_fields - set(values)
                if missing:
                    raise ValidationError(
                        f"Completing {self._complete_contract_label} evidence requires every "
                        f"contract field: {', '.join(sorted(missing))}."
                    )
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
            elif record._name == "saas.dr.drill":
                unhealthy = record.status in {"partial", "failed"}
                severity = "p1" if record.status == "failed" else "p2"
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
            if self._name == "saas.data.quality.run" and raw_item.get(
                "event_stream_complete"
            ) is True:
                complete_fields = model._complete_event_fields
                missing = complete_fields - set(raw_item)
                if missing:
                    raise ValidationError(
                        "Complete event-stream evidence requires all event fields: "
                        f"{', '.join(sorted(missing))}."
                    )
            if self._name == "saas.data.quality.run" and raw_item.get(
                "metric_quality_contract_complete"
            ) is True:
                complete_fields = model._complete_metric_quality_fields
                missing = complete_fields - set(raw_item)
                if missing:
                    raise ValidationError(
                        "Complete metric-quality evidence requires all quality fields: "
                        f"{', '.join(sorted(missing))}."
                    )
            marker = model._complete_contract_marker
            if marker and raw_item.get(marker) is True:
                missing = model._complete_contract_fields - set(raw_item)
                if missing:
                    raise ValidationError(
                        f"Complete {model._complete_contract_label} evidence requires every "
                        f"contract field: {', '.join(sorted(missing))}."
                    )
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

    def action_acknowledge(self):
        self.filtered(lambda alert: alert.status == "open").write(
            {"status": "acknowledged"}
        )
        return True

    def action_resolve(self):
        self.filtered(lambda alert: alert.status != "resolved").write(
            {"status": "resolved"}
        )
        return True


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


class SaasImplementationPlanItem(models.Model):
    _name = "saas.implementation.plan.item"
    _description = "SaaS implementation plan item"
    _order = "priority, sequence, id"

    name = fields.Char(required=True)
    priority = fields.Selection(
        [("p0", "P0"), ("p1", "P1"), ("p2", "P2")],
        required=True,
        default="p1",
        index=True,
    )
    status = fields.Selection(
        [
            ("planned", "Planned"),
            ("in_progress", "In progress"),
            ("blocked", "Blocked"),
            ("done", "Done"),
        ],
        required=True,
        default="planned",
        index=True,
    )
    scope = fields.Selection(
        [
            ("arcigy", "Arcigy"),
            ("odoo", "Odoo"),
            ("cross_system", "Cross-system"),
            ("policy", "Policy / approval"),
        ],
        required=True,
        default="cross_system",
    )
    owner_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)
    sequence = fields.Integer(default=100)
    next_action = fields.Text()
    acceptance_criteria = fields.Text()
    blocker = fields.Text()
    source_document = fields.Char(
        required=True,
        default="docs/SAAS_IMPLEMENTATION_PLAN_REMAINING.md",
        readonly=True,
    )


class SaasBackupRun(models.Model):
    _name = "saas.backup.run"
    _inherit = "saas.operational.ingest.mixin"
    _description = "SaaS backup run"
    _order = "started_at desc, id desc"

    _complete_contract_marker = "backup_contract_complete"
    _complete_contract_label = "backup"
    _complete_contract_fields = {
        "name",
        "started_at",
        "finished_at",
        "status",
        "backup_type",
        "size_bytes",
        "checksum",
        "encrypted",
        "off_host",
        "failure_count_24h",
        "snapshot_count",
        "pitr_enabled",
        "pitr_window_seconds",
        "wal_archive_status",
        "secondary_copy_status",
        "storage_cost_monthly_eur",
    }

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
    backup_contract_complete = fields.Boolean(
        help="True only when the full backup, PITR, secondary-copy and cost contract is measured."
    )
    failure_count_24h = fields.Integer()
    snapshot_count = fields.Integer()
    pitr_enabled = fields.Boolean()
    pitr_window_seconds = fields.Integer()
    wal_archive_status = fields.Selection(
        [
            ("healthy", "Healthy"),
            ("unhealthy", "Unhealthy"),
            ("not_applicable", "Not applicable"),
        ]
    )
    secondary_copy_status = fields.Selection(
        [("healthy", "Healthy"), ("unhealthy", "Unhealthy")]
    )
    storage_cost_monthly_eur = fields.Float()
    drilldown_url = fields.Char()

    @api.constrains(
        "started_at",
        "finished_at",
        "status",
        "size_bytes",
        "checksum",
        "encrypted",
        "off_host",
        "backup_contract_complete",
        "failure_count_24h",
        "snapshot_count",
        "pitr_enabled",
        "pitr_window_seconds",
        "wal_archive_status",
        "secondary_copy_status",
        "storage_cost_monthly_eur",
    )
    def _check_backup_evidence_contract(self):
        for record in self:
            if record.finished_at and record.finished_at <= record.started_at:
                raise ValidationError("Backup finish must be after its start.")
            if any(
                value < 0
                for value in (
                    record.size_bytes,
                    record.failure_count_24h,
                    record.snapshot_count,
                    record.pitr_window_seconds,
                    record.storage_cost_monthly_eur,
                )
            ):
                raise ValidationError("Backup counts, size, PITR window and cost cannot be negative.")
            if not math.isfinite(record.storage_cost_monthly_eur):
                raise ValidationError("Backup storage cost must be finite.")
            if not record.backup_contract_complete:
                continue
            if (
                not record.finished_at
                or record.finished_at <= record.started_at
                or record.status == "running"
            ):
                raise ValidationError(
                    "Complete backup evidence requires a completed run with valid timestamps."
                )
            if record.pitr_enabled:
                if record.pitr_window_seconds <= 0 or record.wal_archive_status == "not_applicable":
                    raise ValidationError(
                        "Enabled PITR requires a positive window and applicable WAL/archive health."
                    )
            elif record.pitr_window_seconds or record.wal_archive_status != "not_applicable":
                raise ValidationError(
                    "Disabled PITR requires a zero window and not-applicable WAL/archive status."
                )
            if record.status == "success" and (
                record.size_bytes <= 0
                or not (record.checksum or "").strip()
                or not record.encrypted
                or not record.off_host
                or record.secondary_copy_status != "healthy"
            ):
                raise ValidationError(
                    "A successful complete backup requires size, checksum, encryption, off-host "
                    "storage and a healthy secondary copy."
                )

    @api.constrains("drilldown_url")
    def _check_drilldown_url(self):
        _validate_http_urls(self, ("drilldown_url",))


class SaasRestoreTest(models.Model):
    _name = "saas.restore.test"
    _inherit = "saas.operational.ingest.mixin"
    _description = "SaaS restore test"
    _order = "started_at desc, id desc"

    _complete_contract_marker = "restore_contract_complete"
    _complete_contract_label = "restore"
    _complete_contract_fields = {
        "name",
        "started_at",
        "finished_at",
        "status",
        "actual_rpo_seconds",
        "actual_rto_seconds",
        "rpo_measured",
        "rto_measured",
        "checksum_valid",
        "application_smoke_passed",
        "tenant_isolation_passed",
        "missing_record_count",
        "owner_team",
        "next_test_at",
    }

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
    rpo_measured = fields.Boolean()
    rto_measured = fields.Boolean()
    checksum_valid = fields.Boolean()
    application_smoke_passed = fields.Boolean()
    tenant_isolation_passed = fields.Boolean()
    restore_contract_complete = fields.Boolean(
        help="True only when the full restore result, ownership and next-test contract is measured."
    )
    missing_record_count = fields.Integer()
    owner_team = fields.Char(size=128)
    next_test_at = fields.Datetime()
    evidence_url = fields.Char()
    owner_id = fields.Many2one("res.users", required=True, default=lambda self: self.env.user)

    @api.constrains("evidence_url")
    def _check_evidence_url(self):
        _validate_http_urls(self, ("evidence_url",))

    @api.constrains(
        "started_at",
        "finished_at",
        "status",
        "actual_rpo_seconds",
        "actual_rto_seconds",
        "rpo_measured",
        "rto_measured",
        "checksum_valid",
        "application_smoke_passed",
        "tenant_isolation_passed",
        "restore_contract_complete",
        "missing_record_count",
        "owner_team",
        "next_test_at",
    )
    def _check_restore_evidence(self):
        for record in self:
            if record.finished_at and record.finished_at <= record.started_at:
                raise ValidationError("Restore finish must be after its start.")
            if record.actual_rpo_seconds < 0 or record.actual_rto_seconds < 0:
                raise ValidationError("Restore RPO and RTO cannot be negative.")
            if record.missing_record_count < 0:
                raise ValidationError("Restore missing-record count cannot be negative.")
            if not record.rpo_measured and record.actual_rpo_seconds:
                raise ValidationError("RPO requires explicit measured evidence.")
            if not record.rto_measured and record.actual_rto_seconds:
                raise ValidationError("RTO requires explicit measured evidence.")
            if record.status in {"success", "failed"} and not record.finished_at:
                raise ValidationError("A completed restore test requires finished_at.")
            if record.status == "success" and not all(
                (
                    record.checksum_valid,
                    record.application_smoke_passed,
                    record.tenant_isolation_passed,
                    record.rpo_measured,
                    record.rto_measured,
                )
            ):
                raise ValidationError(
                    "A successful restore requires checksum, smoke, tenant isolation, RPO and RTO evidence."
                )
            if not record.restore_contract_complete:
                continue
            if (
                not record.finished_at
                or record.finished_at <= record.started_at
                or record.status == "running"
            ):
                raise ValidationError(
                    "Complete restore evidence requires a completed test with valid timestamps."
                )
            if not (record.owner_team or "").strip():
                raise ValidationError("Complete restore evidence requires an owner team.")
            if not record.next_test_at or record.next_test_at <= record.finished_at:
                raise ValidationError(
                    "Complete restore evidence requires a next test after the completed test."
                )
            if record.status == "success" and record.missing_record_count:
                raise ValidationError("A successful complete restore cannot have missing records.")


class SaasDrDrill(models.Model):
    _name = "saas.dr.drill"
    _inherit = "saas.operational.ingest.mixin"
    _description = "SaaS disaster recovery drill"
    _order = "started_at desc, id desc"

    _complete_contract_marker = "dr_contract_complete"
    _complete_contract_label = "disaster-recovery drill"
    _complete_contract_fields = {
        "name",
        "started_at",
        "finished_at",
        "status",
        "failover_duration_seconds",
        "failback_duration_seconds",
        "dns_propagation_duration_seconds",
        "unavailable_dependency_count",
        "runbook_accuracy_rate",
        "open_remediation_action_count",
        "owner_team",
        "next_drill_at",
    }

    name = fields.Char(required=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    started_at = fields.Datetime(required=True, index=True)
    finished_at = fields.Datetime()
    status = fields.Selection(
        [
            ("running", "Running"),
            ("success", "Success"),
            ("partial", "Partial"),
            ("failed", "Failed"),
        ],
        required=True,
        index=True,
    )
    dr_contract_complete = fields.Boolean(
        help="True only when failover, failback, DNS, dependencies, runbook and remediation are measured."
    )
    failover_duration_seconds = fields.Float()
    failback_duration_seconds = fields.Float()
    dns_propagation_duration_seconds = fields.Float()
    unavailable_dependency_count = fields.Integer()
    runbook_accuracy_rate = fields.Float()
    open_remediation_action_count = fields.Integer()
    owner_team = fields.Char(size=128)
    next_drill_at = fields.Datetime()
    evidence_url = fields.Char()

    @api.constrains("evidence_url")
    def _check_evidence_url(self):
        _validate_http_urls(self, ("evidence_url",))

    @api.constrains(
        "started_at",
        "finished_at",
        "status",
        "dr_contract_complete",
        "failover_duration_seconds",
        "failback_duration_seconds",
        "dns_propagation_duration_seconds",
        "unavailable_dependency_count",
        "runbook_accuracy_rate",
        "open_remediation_action_count",
        "owner_team",
        "next_drill_at",
    )
    def _check_dr_evidence_contract(self):
        for record in self:
            if record.finished_at and record.finished_at <= record.started_at:
                raise ValidationError("DR drill finish must be after its start.")
            if any(
                value < 0
                for value in (
                    record.failover_duration_seconds,
                    record.failback_duration_seconds,
                    record.dns_propagation_duration_seconds,
                    record.unavailable_dependency_count,
                    record.runbook_accuracy_rate,
                    record.open_remediation_action_count,
                )
            ):
                raise ValidationError("DR drill measurements cannot be negative.")
            if not all(
                math.isfinite(value)
                for value in (
                    record.failover_duration_seconds,
                    record.failback_duration_seconds,
                    record.dns_propagation_duration_seconds,
                    record.runbook_accuracy_rate,
                )
            ):
                raise ValidationError("DR drill duration and accuracy measurements must be finite.")
            if record.runbook_accuracy_rate > 100:
                raise ValidationError("DR drill runbook accuracy cannot exceed 100 percent.")
            if not record.dr_contract_complete:
                continue
            if (
                not record.finished_at
                or record.finished_at <= record.started_at
                or record.status == "running"
            ):
                raise ValidationError(
                    "Complete DR drill evidence requires a completed drill with valid timestamps."
                )
            if not (record.owner_team or "").strip():
                raise ValidationError("Complete DR drill evidence requires an owner team.")
            if not record.next_drill_at or record.next_drill_at <= record.finished_at:
                raise ValidationError(
                    "Complete DR drill evidence requires a next drill after the completed drill."
                )


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
    representative = fields.Boolean()
    architecture_version = fields.Char()
    evidence_url = fields.Char()

    @api.constrains("evidence_url")
    def _check_evidence_url(self):
        _validate_http_urls(self, ("evidence_url",))

    @api.constrains(
        "started_at",
        "finished_at",
        "concurrent_users",
        "requests_per_second",
        "p95_seconds",
        "p99_seconds",
        "error_rate",
        "recovery_seconds",
        "representative",
        "architecture_version",
    )
    def _check_load_evidence(self):
        for record in self:
            if record.finished_at and record.finished_at <= record.started_at:
                raise ValidationError("Load-test finish must be after its start.")
            if any(
                value < 0
                for value in (
                    record.concurrent_users,
                    record.requests_per_second,
                    record.p95_seconds,
                    record.p99_seconds,
                    record.error_rate,
                    record.recovery_seconds,
                )
            ):
                raise ValidationError("Load-test measurements cannot be negative.")
            if record.error_rate > 100:
                raise ValidationError("Load-test error rate cannot exceed 100 percent.")
            if record.p99_seconds and record.p95_seconds and record.p99_seconds < record.p95_seconds:
                raise ValidationError("Load-test p99 cannot be lower than p95.")
            architecture_version = (record.architecture_version or "").strip()
            if len(architecture_version) > 128:
                raise ValidationError("Architecture version is too long.")
            if record.representative and (
                not record.finished_at
                or not architecture_version
                or record.concurrent_users <= 0
            ):
                raise ValidationError(
                    "A representative load test requires finish time, architecture version and positive concurrency."
                )


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
    duplicate_upsert_count = fields.Integer()
    api_error_count = fields.Integer()
    authentication_error_count = fields.Integer()
    permission_error_count = fields.Integer()
    rate_limit_error_count = fields.Integer()
    retry_count = fields.Integer()
    backlog_count = fields.Integer()
    oldest_unsynced_at = fields.Datetime()
    error_code = fields.Char()
    drilldown_url = fields.Char()
    sync_contract_complete = fields.Boolean(
        help="True only when the full sync attempt and backlog contract is measured."
    )
    external_key = fields.Char(index=True)
    source_updated_at = fields.Datetime(
        required=True, default=fields.Datetime.now, index=True
    )

    _external_key_unique = models.Constraint(
        "UNIQUE(external_key)", "Sync evidence external key must be unique."
    )

    _complete_sync_count_fields = {
        "records_read",
        "records_created",
        "records_updated",
        "records_skipped",
        "records_rejected",
        "duplicate_upsert_count",
        "api_error_count",
        "authentication_error_count",
        "permission_error_count",
        "rate_limit_error_count",
        "retry_count",
        "backlog_count",
    }

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("sync_contract_complete") is True:
                missing = self._complete_sync_count_fields - set(values)
                if missing:
                    raise ValidationError(
                        "Complete sync evidence requires every sync count: "
                        f"{', '.join(sorted(missing))}."
                    )
                if not values.get("external_key"):
                    raise ValidationError("Complete sync evidence requires external_key.")
        return super().create(vals_list)

    def write(self, values):
        if values.get("sync_contract_complete") is True:
            for record in self.filtered(lambda item: not item.sync_contract_complete):
                missing = self._complete_sync_count_fields - set(values)
                if missing:
                    raise ValidationError(
                        "Completing sync evidence requires every sync count: "
                        f"{', '.join(sorted(missing))}."
                    )
                if not (values.get("external_key") or record.external_key):
                    raise ValidationError("Complete sync evidence requires external_key.")
        return super().write(values)

    @api.constrains(
        "environment_id",
        "external_key",
        "started_at",
        "finished_at",
        "status",
        "sync_contract_complete",
        "records_read",
        "records_created",
        "records_updated",
        "records_skipped",
        "records_rejected",
        "duplicate_upsert_count",
        "api_error_count",
        "authentication_error_count",
        "permission_error_count",
        "rate_limit_error_count",
        "retry_count",
        "backlog_count",
        "oldest_unsynced_at",
        "error_code",
    )
    def _check_sync_evidence_contract(self):
        for record in self:
            counts = [record[field_name] for field_name in self._complete_sync_count_fields]
            if any(value < 0 for value in counts):
                raise ValidationError("Sync evidence counts cannot be negative.")
            if record.external_key and (
                len(record.external_key) > 255
                or not record.external_key.startswith(f"{record.environment_id.code}:")
                or not re.fullmatch(r"[A-Za-z0-9._:-]+", record.external_key)
            ):
                raise ValidationError(
                    "Sync evidence external_key must be safe and environment-prefixed."
                )
            if record.error_code and not re.fullmatch(
                r"[A-Z0-9_.:-]{1,64}", record.error_code
            ):
                raise ValidationError("Sync error_code must be a bounded symbolic code.")
            if not record.sync_contract_complete:
                continue
            if (
                not record.finished_at
                or record.finished_at <= record.started_at
                or record.status == "running"
            ):
                raise ValidationError(
                    "Complete sync evidence requires a completed attempt with valid timestamps."
                )
            categorized = (
                record.records_created
                + record.records_updated
                + record.records_skipped
                + record.records_rejected
            )
            if categorized != record.records_read:
                raise ValidationError(
                    "Created, updated, skipped and rejected records must equal records read."
                )
            if record.duplicate_upsert_count > record.records_read:
                raise ValidationError("Duplicate upserts cannot exceed records read.")
            classified_api_errors = (
                record.authentication_error_count
                + record.permission_error_count
                + record.rate_limit_error_count
            )
            if classified_api_errors > record.api_error_count:
                raise ValidationError(
                    "Classified API errors cannot exceed total API errors."
                )
            if record.backlog_count > 0 and not record.oldest_unsynced_at:
                raise ValidationError("A positive sync backlog requires oldest_unsynced_at.")
            if record.backlog_count == 0 and record.oldest_unsynced_at:
                raise ValidationError("An empty sync backlog cannot have oldest_unsynced_at.")
            if record.oldest_unsynced_at and record.oldest_unsynced_at > record.finished_at:
                raise ValidationError("oldest_unsynced_at cannot be newer than the sync finish.")
            if record.status == "success" and (
                record.records_rejected
                or record.api_error_count
                or record.authentication_error_count
                or record.permission_error_count
                or record.rate_limit_error_count
            ):
                raise ValidationError("A successful complete sync cannot contain errors.")

    @api.constrains("drilldown_url")
    def _check_sync_drilldown_url(self):
        _validate_http_urls(self, ("drilldown_url",))

    @api.model
    def ingest_sync_run_batch(self, payload):
        if not (
            self.env.user.has_group("arcigy_saas_control_center.group_saas_integration_bot")
            or self.env.user.has_group("arcigy_saas_control_center.group_saas_administrator")
        ):
            raise AccessError("Only the SaaS integration bot can ingest sync evidence.")
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object.")
        unknown_payload_fields = set(payload) - {"environment", "source_updated_at", "items"}
        if unknown_payload_fields:
            raise ValidationError(
                "Unsupported sync payload fields: "
                f"{', '.join(sorted(unknown_payload_fields))}."
            )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 200:
            raise ValidationError("items must contain between 1 and 200 sync records.")
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
        if source_updated_at > fields.Datetime.now() + timedelta(minutes=5):
            raise ValidationError("source_updated_at is too far in the future.")
        created = updated = stale_skipped = 0
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValidationError("Every sync item must be an object.")
            unknown = set(raw_item) - SYNC_RUN_ALLOWED_FIELDS - {"external_key"}
            if unknown:
                raise ValidationError(
                    f"Unsupported sync fields: {', '.join(sorted(unknown))}."
                )
            if raw_item.get("sync_contract_complete") is not True:
                raise ValidationError("External sync evidence must declare a complete contract.")
            required_fields = self._complete_sync_count_fields | {
                "name",
                "started_at",
                "finished_at",
                "status",
                "sync_contract_complete",
            }
            missing = required_fields - set(raw_item)
            if missing:
                raise ValidationError(
                    "Complete external sync evidence requires every contract field: "
                    f"{', '.join(sorted(missing))}."
                )
            external_key = _operation_text(
                raw_item.get("external_key"), "external_key", 255
            )
            if (
                not external_key
                or not external_key.startswith(f"{environment_code}:")
                or not re.fullmatch(r"[A-Za-z0-9._:-]+", external_key)
            ):
                raise ValidationError(
                    "external_key must be safe and environment-prefixed."
                )
            values = {
                "environment_id": environment.id,
                "external_key": external_key,
                "source_updated_at": source_updated_at,
            }
            for field_name in SYNC_RUN_ALLOWED_FIELDS:
                if field_name not in raw_item:
                    continue
                field = model._fields[field_name]
                value = raw_item[field_name]
                if field.type == "datetime":
                    values[field_name] = _operation_datetime(
                        value, field_name, required=field.required
                    )
                elif field.type == "integer":
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise ValidationError(f"{field_name} must be numeric.")
                    numeric = float(value)
                    if not math.isfinite(numeric) or not numeric.is_integer():
                        raise ValidationError(f"{field_name} must be a finite integer.")
                    values[field_name] = int(numeric)
                elif field.type == "boolean":
                    if not isinstance(value, bool):
                        raise ValidationError(f"{field_name} must be boolean.")
                    values[field_name] = value
                elif field.type in {"char", "selection"}:
                    values[field_name] = _operation_text(value, field_name)
                else:
                    raise ValidationError(f"{field_name} cannot be ingested directly.")
            for field_name in ("started_at", "finished_at", "oldest_unsynced_at"):
                if values.get(field_name) and values[field_name] > source_updated_at + timedelta(
                    minutes=5
                ):
                    raise ValidationError(f"{field_name} is too far in the future.")
            existing = model.search([("external_key", "=", external_key)], limit=1)
            if existing:
                if existing.environment_id != environment:
                    raise ValidationError("external_key belongs to another environment.")
                if existing.source_updated_at > source_updated_at:
                    stale_skipped += 1
                    continue
                existing.write(values)
                updated += 1
            else:
                model.create(values)
                created += 1
        return {
            "ok": True,
            "environment": environment_code,
            "created": created,
            "updated": updated,
            "stale_skipped": stale_skipped,
            "processed": len(raw_items),
        }
