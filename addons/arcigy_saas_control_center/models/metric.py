import hashlib
import math
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


STATUS_SELECTION = [
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("critical", "Critical"),
    ("unknown", "Unknown"),
]
SCOPE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")
METRIC_BATCH_FIELDS = {"environment", "source_updated_at", "release_version", "commit_sha", "metrics"}
METRIC_ITEM_FIELDS = {
    "code", "value", "numerator", "denominator", "sample_count", "status",
    "measured_at", "freshness_seconds", "scope_key", "service_code",
    "tenant_external_id", "plan_code", "region_code", "feature_code",
    "integration_code", "country_code", "currency_code", "tenant_size_band", "model_code",
    "endpoint_group", "job_type", "acquisition_source", "browser",
    "operating_system", "device", "incident_severity", "drilldown_url",
    "external_key", "period_start", "period_end", "granularity",
}


def _utc_datetime(value, field_name):
    if not value:
        return fields.Datetime.now()
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValidationError(f"{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _required_external_utc_datetime(value, field_name):
    if not value:
        raise ValidationError(f"{field_name} is required.")
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as error:
            raise ValidationError(f"{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        raise ValidationError(f"{field_name} must explicitly use UTC.")
    if parsed.utcoffset() != timedelta(0):
        raise ValidationError(f"{field_name} must explicitly use UTC.")
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _finite_number(value, field_name, required=False):
    if value is None and not required:
        return False
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field_name} must be a finite number.")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValidationError(f"{field_name} must be a finite number.")
    return converted


def _safe_url(value, field_name):
    normalized = str(value or "").strip()
    if not normalized:
        return False
    if len(normalized) > 1024:
        raise ValidationError(f"{field_name} is too long.")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password:
        raise ValidationError(f"{field_name} must be an http(s) URL without credentials.")
    return normalized


def _bounded_text(value, field_name, maximum=120):
    normalized = str(value or "").strip()
    if not normalized:
        return False
    if len(normalized) > maximum or any(ord(char) < 32 for char in normalized):
        raise ValidationError(f"{field_name} is invalid or too long.")
    return normalized


class SaasMetricDefinition(models.Model):
    _name = "saas.metric.definition"
    _description = "SaaS metric definition"
    _order = "sequence, code"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    dashboard_ids = fields.Many2many(
        "saas.dashboard",
        "saas_metric_dashboard_rel",
        "metric_id",
        "dashboard_id",
        string="Dashboards",
        required=True,
    )
    description = fields.Text(required=True, translate=True)
    formula = fields.Char(required=True)
    metric_type = fields.Selection(
        [
            ("counter", "Counter"),
            ("gauge", "Gauge"),
            ("histogram", "Histogram"),
            ("ratio", "Ratio"),
            ("money", "Money"),
            ("duration", "Duration"),
            ("status", "Status"),
        ],
        required=True,
        index=True,
    )
    unit = fields.Char(required=True)
    numerator_name = fields.Char(required=True, default="Not applicable")
    denominator_name = fields.Char(required=True, default="Not applicable")
    source = fields.Char(required=True)
    dimensions = fields.Char(
        required=True,
        default="environment,scope",
        help="Comma-separated relation or bounded dimensions available for filtering.",
    )
    granularity = fields.Selection(
        [
            ("current", "Current"),
            ("5m", "5 minutes"),
            ("hour", "Hour"),
            ("day", "Day"),
            ("month", "Month"),
            ("event", "Event"),
        ],
        required=True,
        default="5m",
    )
    audience = fields.Selection(
        [
            ("general", "General"),
            ("executive", "Executive"),
            ("finance", "Finance"),
            ("customer_success", "Customer Success"),
            ("support", "Support"),
            ("engineering", "Engineering"),
            ("security", "Security"),
        ],
        required=True,
        default="general",
        index=True,
    )
    owner = fields.Char(string="Owner team", required=True, default="Engineering")
    owner_id = fields.Many2one(
        "res.users", required=True, default=lambda self: self.env.user, ondelete="restrict"
    )
    target_value = fields.Float()
    warning_value = fields.Float()
    critical_value = fields.Float()
    warning_alert_severity = fields.Selection(
        [("p1", "P1"), ("p2", "P2"), ("p3", "P3")],
        required=True,
        default="p2",
    )
    critical_alert_severity = fields.Selection(
        [("p0", "P0"), ("p1", "P1"), ("p2", "P2")],
        required=True,
        default="p1",
    )
    direction = fields.Selection(
        [("higher", "Higher is better"), ("lower", "Lower is better"), ("neutral", "Neutral")],
        required=True,
        default="neutral",
    )
    freshness_seconds = fields.Integer(required=True, default=300)
    retention_days = fields.Integer(required=True, default=90)
    formula_version = fields.Char(required=True, default="1")
    runbook = fields.Text(
        required=True,
        default=(
            "Confirm data freshness and the affected environment. Open the drilldown, "
            "compare Develop with Main, identify the owning service or tenant scope, "
            "and record the recovery evidence in the related incident."
        ),
    )
    drilldown_url = fields.Char()
    runbook_url = fields.Char()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Metric code must be unique.")

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("metric_type") == "ratio":
                formula = values.get("formula") or ""
                if "/" in formula:
                    numerator, denominator = formula.split("/", 1)
                    values.setdefault("numerator_name", numerator.strip()[:255])
                    values.setdefault("denominator_name", denominator.strip()[:255])
                else:
                    values.setdefault("numerator_name", "Formula numerator")
                    values.setdefault("denominator_name", "Formula denominator")
            else:
                values.setdefault("numerator_name", "Not applicable")
                values.setdefault("denominator_name", "Not applicable")
        return super().create(vals_list)

    @api.constrains("freshness_seconds", "retention_days")
    def _check_positive_windows(self):
        for record in self:
            if record.freshness_seconds <= 0 or record.retention_days <= 0:
                raise ValidationError("Freshness and retention must be positive.")


class SaasMetricCurrent(models.Model):
    _name = "saas.metric.current"
    _description = "Current SaaS metric value"
    _order = "metric_id, environment_id, scope_key"

    metric_id = fields.Many2one(
        "saas.metric.definition", required=True, ondelete="restrict", index=True
    )
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    service_id = fields.Many2one("saas.service", ondelete="restrict", index=True)
    tenant_id = fields.Many2one("saas.tenant", ondelete="restrict", index=True)
    plan_id = fields.Many2one("saas.plan", ondelete="restrict", index=True)
    region_id = fields.Many2one("saas.region", ondelete="restrict", index=True)
    release_id = fields.Many2one("saas.release", ondelete="restrict", index=True)
    feature_id = fields.Many2one("saas.feature", ondelete="restrict", index=True)
    integration_id = fields.Many2one("saas.integration", ondelete="restrict", index=True)
    country_id = fields.Many2one("res.country", ondelete="restrict", index=True)
    currency_id = fields.Many2one("res.currency", ondelete="restrict", index=True)
    tenant_size_band = fields.Selection(
        [
            ("micro", "Micro"),
            ("small", "Small"),
            ("medium", "Medium"),
            ("large", "Large"),
            ("enterprise", "Enterprise"),
        ],
        index=True,
    )
    endpoint_group = fields.Char(index=True)
    job_type = fields.Char(index=True)
    acquisition_source = fields.Char(index=True)
    browser = fields.Char(index=True)
    operating_system = fields.Char(index=True)
    device = fields.Char(index=True)
    model_code = fields.Char(index=True)
    incident_severity = fields.Selection(
        [("p0", "P0"), ("p1", "P1"), ("p2", "P2"), ("p3", "P3")],
        index=True,
    )
    scope_key = fields.Char(required=True, default="global", index=True)
    status = fields.Selection(STATUS_SELECTION, required=True, default="unknown", index=True)
    current_value = fields.Float(required=True)
    numerator = fields.Float()
    denominator = fields.Float()
    sample_count = fields.Integer()
    measured_at = fields.Datetime(required=True, index=True)
    fresh_until = fields.Datetime(required=True, index=True)
    source_updated_at = fields.Datetime()
    drilldown_url = fields.Char()
    target_value = fields.Float(related="metric_id.target_value")
    warning_value = fields.Float(related="metric_id.warning_value")
    critical_value = fields.Float(related="metric_id.critical_value")
    freshness_status = fields.Selection(
        [("fresh", "Fresh"), ("delayed", "Delayed"), ("stale", "Stale")],
        compute="_compute_freshness_status",
    )

    _metric_environment_scope_unique = models.Constraint(
        "UNIQUE(metric_id, environment_id, scope_key)",
        "A metric can have only one current value per environment and scope.",
    )

    @api.depends("measured_at", "fresh_until")
    def _compute_freshness_status(self):
        now = fields.Datetime.now()
        for record in self:
            cadence = max(
                (record.fresh_until - record.measured_at).total_seconds()
                if record.measured_at and record.fresh_until
                else record.metric_id.freshness_seconds,
                1,
            )
            age = (now - record.measured_at).total_seconds() if record.measured_at else cadence * 4
            if age <= cadence * 1.5:
                record.freshness_status = "fresh"
            elif age <= cadence * 3:
                record.freshness_status = "delayed"
            else:
                record.freshness_status = "stale"

    def _sync_status_alert(self):
        self.ensure_one()
        alert_model = self.env["saas.alert"].sudo()
        open_alerts = alert_model.search(
            [
                ("metric_id", "=", self.metric_id.id),
                ("environment_id", "=", self.environment_id.id),
                ("scope_key", "=", self.scope_key),
                ("status", "!=", "resolved"),
            ]
        )
        if self.status == "healthy":
            if open_alerts:
                open_alerts.write({"status": "resolved", "resolved_at": fields.Datetime.now()})
            return 0, len(open_alerts)
        if self.status not in {"warning", "critical"}:
            return 0, 0
        severity = (
            self.metric_id.critical_alert_severity
            if self.status == "critical"
            else self.metric_id.warning_alert_severity
        )
        threshold = (
            self.metric_id.critical_value
            if self.status == "critical"
            else self.metric_id.warning_value
        )
        base_url = self.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", "http://localhost"
        ).rstrip("/")
        runbook_url = self.metric_id.runbook_url or (
            f"{base_url}/web#id={self.metric_id.id}&model=saas.metric.definition&view_type=form"
        )
        drilldown_url = self.drilldown_url or self.metric_id.drilldown_url or (
            f"{base_url}/web#id={self.id}&model=saas.metric.current&view_type=form"
        )
        alert_values = {
            "name": f"{self.metric_id.name}: {self.status} in {self.environment_id.name}",
            "severity": severity,
            "metric_id": self.metric_id.id,
            "environment_id": self.environment_id.id,
            "scope_key": self.scope_key,
            "service_id": self.service_id.id or False,
            "tenant_id": self.tenant_id.id or False,
            "release_id": self.release_id.id or False,
            "current_value": self.current_value,
            "threshold": threshold,
            "owner_id": self.metric_id.owner_id.id,
            "runbook_url": runbook_url,
            "drilldown_url": drilldown_url,
            "recovery_condition": "The same metric, environment and scope returns to healthy.",
        }
        if open_alerts:
            open_alerts[:1].write(alert_values)
            return 0, 0
        measured_key = fields.Datetime.to_string(self.measured_at).replace(" ", "T")
        alert_values.update(
            {
                "status": "open",
                "detected_at": self.measured_at,
                "deduplication_key": (
                    f"{self.environment_id.code}:{self.metric_id.code}:"
                    f"{self.scope_key}:{measured_key}"
                )[:255],
            }
        )
        alert_model.create(alert_values)
        return 1, 0

    @api.model
    def _internal_metric_status(self, definition, value):
        if definition.direction == "neutral":
            return "unknown"
        if definition.direction == "lower":
            if value >= definition.critical_value:
                return "critical"
            if value >= definition.warning_value:
                return "warning"
            return "healthy"
        if value <= definition.critical_value:
            return "critical"
        if value <= definition.warning_value:
            return "warning"
        return "healthy"

    @api.model
    def _cron_refresh_internal_operational_metrics(self):
        """Refresh only metrics whose authoritative source is this Odoo database."""
        self = self.sudo()
        now = fields.Datetime.now()
        hour_start = now.replace(minute=0, second=0, microsecond=0)
        hour_end = hour_start + timedelta(hours=1)
        base_url = self.env["ir.config_parameter"].get_param(
            "web.base.url", "http://localhost"
        ).rstrip("/")
        metric_specs = {
            "open_p0_p1_incidents": {
                "model": "saas.incident",
                "drilldown": f"{base_url}/web#model=saas.incident&view_type=list",
            },
            "backup_age_seconds": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_duration_seconds": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_size_bytes": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_failure_count_24h": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_snapshot_count": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_pitr_enabled_status": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_pitr_window_seconds": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_wal_archive_health_status": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_secondary_copy_status": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_encryption_status": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "backup_storage_cost_monthly_eur": {
                "model": "saas.backup.run",
                "drilldown": f"{base_url}/web#model=saas.backup.run&view_type=list",
            },
            "restore_test_age_seconds": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "restore_test_success_rate": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "actual_rpo_seconds": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "actual_rto_seconds": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "restore_duration_seconds": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "restore_checksum_status": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "restore_missing_record_count": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "restore_application_smoke_status": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "restore_tenant_isolation_status": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "restore_next_test_overdue_seconds": {
                "model": "saas.restore.test",
                "drilldown": f"{base_url}/web#model=saas.restore.test&view_type=list",
            },
            "dr_drill_age_seconds": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_drill_success_status": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_failover_duration_seconds": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_failback_duration_seconds": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_dns_propagation_duration_seconds": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_unavailable_dependency_count": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_runbook_accuracy_rate": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_open_remediation_action_count": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "dr_next_drill_overdue_seconds": {
                "model": "saas.dr.drill",
                "drilldown": f"{base_url}/web#model=saas.dr.drill&view_type=list",
            },
            "odoo_sync_freshness_seconds": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_attempt_age_seconds": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_duration_seconds": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_records_read_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_records_created_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_records_updated_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_records_skipped_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_records_rejected_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_duplicate_upsert_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_api_error_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_authentication_error_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_permission_error_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_rate_limit_error_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_retry_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_backlog_count": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_oldest_unsynced_age_seconds": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "odoo_sync_error_rate": {
                "model": "saas.sync.run",
                "drilldown": f"{base_url}/web#model=saas.sync.run&view_type=list",
            },
            "events_sent_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "events_received_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "events_processed_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "events_rejected_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "event_loss_estimate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "event_duplicate_rate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "schema_validation_failure_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "missing_required_field_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "late_event_rate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "unknown_tenant_mapping_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "event_clock_skew_seconds": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "event_processing_lag_p95_seconds": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "dead_letter_event_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "metric_freshness_rate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "metric_completeness_rate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "metric_uniqueness_rate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "metric_validity_rate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "metric_consistency_rate": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "data_quality_reconciliation_difference": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "data_quality_outlier_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "unexpected_zero_value_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "unexpected_volume_spike_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "numerator_denominator_violation_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "negative_value_violation_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "missing_dimension_count": {
                "model": "saas.data.quality.run",
                "drilldown": f"{base_url}/web#model=saas.data.quality.run&view_type=list",
            },
            "tested_concurrent_users": {
                "model": "saas.load.test",
                "drilldown": f"{base_url}/web#model=saas.load.test&view_type=list",
            },
            "load_test_age_days": {
                "model": "saas.load.test",
                "drilldown": f"{base_url}/web#model=saas.load.test&view_type=list",
            },
            "capacity_readiness_status": {
                "model": "saas.load.test",
                "drilldown": f"{base_url}/web#model=saas.load.test&view_type=list",
            },
        }
        definitions = {
            definition.code: definition
            for definition in self.env["saas.metric.definition"].search(
                [("code", "in", list(metric_specs)), ("active", "=", True)]
            )
        }
        environments = self.env["saas.environment"].search(
            [("code", "in", ["develop", "main"]), ("active", "=", True)]
        )
        refreshed = 0
        omitted = []

        def age_seconds(record):
            return max((now - record.finished_at).total_seconds(), 0)

        for environment in environments:
            details = {}
            values = {
                "open_p0_p1_incidents": self.env["saas.incident"].search_count(
                    [
                        "|",
                        ("triggering_metric_id", "=", False),
                        ("triggering_metric_id.code", "!=", "open_p0_p1_incidents"),
                        ("environment_id", "=", environment.id),
                        ("severity", "in", ["p0", "p1"]),
                        ("status", "!=", "resolved"),
                    ]
                )
            }
            backup = self.env["saas.backup.run"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("status", "=", "success"),
                    ("encrypted", "=", True),
                    ("off_host", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if backup:
                values["backup_age_seconds"] = age_seconds(backup)
            backup_evidence = self.env["saas.backup.run"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("backup_contract_complete", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if backup_evidence:
                backup_values = {
                    "backup_duration_seconds": (
                        backup_evidence.finished_at - backup_evidence.started_at
                    ).total_seconds(),
                    "backup_size_bytes": backup_evidence.size_bytes,
                    "backup_failure_count_24h": backup_evidence.failure_count_24h,
                    "backup_snapshot_count": backup_evidence.snapshot_count,
                    "backup_pitr_enabled_status": 1 if backup_evidence.pitr_enabled else 0,
                    "backup_pitr_window_seconds": backup_evidence.pitr_window_seconds,
                    "backup_secondary_copy_status": (
                        1 if backup_evidence.secondary_copy_status == "healthy" else 0
                    ),
                    "backup_encryption_status": 1 if backup_evidence.encrypted else 0,
                    "backup_storage_cost_monthly_eur": (
                        backup_evidence.storage_cost_monthly_eur
                    ),
                }
                if backup_evidence.wal_archive_status != "not_applicable":
                    backup_values["backup_wal_archive_health_status"] = (
                        1 if backup_evidence.wal_archive_status == "healthy" else 0
                    )
                for code, value in backup_values.items():
                    values[code] = value
                    details[code] = {
                        "sample_count": 1,
                        "source_record": backup_evidence,
                    }
            latest_restore = self.env["saas.restore.test"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if latest_restore:
                restore_success = bool(
                    latest_restore.status == "success"
                    and latest_restore.checksum_valid
                    and latest_restore.application_smoke_passed
                    and latest_restore.tenant_isolation_passed
                )
                values["restore_test_success_rate"] = 100 if restore_success else 0
                details["restore_test_success_rate"] = {
                    "numerator": 1 if restore_success else 0,
                    "denominator": 1,
                    "sample_count": 1,
                    "source_record": latest_restore,
                }
            restore = self.env["saas.restore.test"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("status", "=", "success"),
                    ("checksum_valid", "=", True),
                    ("application_smoke_passed", "=", True),
                    ("tenant_isolation_passed", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if restore:
                values["restore_test_age_seconds"] = age_seconds(restore)
                if restore.rpo_measured:
                    values["actual_rpo_seconds"] = restore.actual_rpo_seconds
                    details["actual_rpo_seconds"] = {"source_record": restore}
                if restore.rto_measured:
                    values["actual_rto_seconds"] = restore.actual_rto_seconds
                    details["actual_rto_seconds"] = {"source_record": restore}
            restore_evidence = self.env["saas.restore.test"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("restore_contract_complete", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if restore_evidence:
                restore_values = {
                    "restore_duration_seconds": (
                        restore_evidence.finished_at - restore_evidence.started_at
                    ).total_seconds(),
                    "restore_checksum_status": 1 if restore_evidence.checksum_valid else 0,
                    "restore_missing_record_count": restore_evidence.missing_record_count,
                    "restore_application_smoke_status": (
                        1 if restore_evidence.application_smoke_passed else 0
                    ),
                    "restore_tenant_isolation_status": (
                        1 if restore_evidence.tenant_isolation_passed else 0
                    ),
                    "restore_next_test_overdue_seconds": max(
                        (now - restore_evidence.next_test_at).total_seconds(), 0
                    ),
                }
                for code, value in restore_values.items():
                    values[code] = value
                    details[code] = {
                        "sample_count": 1,
                        "source_record": restore_evidence,
                    }
            dr_drill = self.env["saas.dr.drill"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("dr_contract_complete", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if dr_drill:
                dr_values = {
                    "dr_drill_age_seconds": age_seconds(dr_drill),
                    "dr_drill_success_status": 1 if dr_drill.status == "success" else 0,
                    "dr_failover_duration_seconds": dr_drill.failover_duration_seconds,
                    "dr_failback_duration_seconds": dr_drill.failback_duration_seconds,
                    "dr_dns_propagation_duration_seconds": (
                        dr_drill.dns_propagation_duration_seconds
                    ),
                    "dr_unavailable_dependency_count": (
                        dr_drill.unavailable_dependency_count
                    ),
                    "dr_runbook_accuracy_rate": dr_drill.runbook_accuracy_rate,
                    "dr_open_remediation_action_count": (
                        dr_drill.open_remediation_action_count
                    ),
                    "dr_next_drill_overdue_seconds": max(
                        (now - dr_drill.next_drill_at).total_seconds(), 0
                    ),
                }
                for code, value in dr_values.items():
                    values[code] = value
                    details[code] = {
                        "sample_count": 1,
                        "source_record": dr_drill,
                    }
            sync_run = self.env["saas.sync.run"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("status", "=", "success"),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if sync_run:
                values["odoo_sync_freshness_seconds"] = age_seconds(sync_run)

            sync_attempt = self.env["saas.sync.run"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("sync_contract_complete", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if sync_attempt:
                values["odoo_sync_attempt_age_seconds"] = max(
                    (now - sync_attempt.started_at).total_seconds(), 0
                )
                details["odoo_sync_attempt_age_seconds"] = {"sample_count": 1}
                values["odoo_sync_duration_seconds"] = (
                    sync_attempt.finished_at - sync_attempt.started_at
                ).total_seconds()
                details["odoo_sync_duration_seconds"] = {
                    "sample_count": 1,
                    "source_record": sync_attempt,
                }
                sync_counts = {
                    "odoo_sync_records_read_count": sync_attempt.records_read,
                    "odoo_sync_records_created_count": sync_attempt.records_created,
                    "odoo_sync_records_updated_count": sync_attempt.records_updated,
                    "odoo_sync_records_skipped_count": sync_attempt.records_skipped,
                    "odoo_sync_records_rejected_count": sync_attempt.records_rejected,
                    "odoo_sync_duplicate_upsert_count": sync_attempt.duplicate_upsert_count,
                    "odoo_sync_api_error_count": sync_attempt.api_error_count,
                    "odoo_sync_authentication_error_count": (
                        sync_attempt.authentication_error_count
                    ),
                    "odoo_sync_permission_error_count": sync_attempt.permission_error_count,
                    "odoo_sync_rate_limit_error_count": sync_attempt.rate_limit_error_count,
                    "odoo_sync_retry_count": sync_attempt.retry_count,
                    "odoo_sync_backlog_count": sync_attempt.backlog_count,
                }
                attempted_records = sync_attempt.records_read + sync_attempt.api_error_count
                for code, value in sync_counts.items():
                    values[code] = value
                    details[code] = {
                        "sample_count": attempted_records,
                        "source_record": sync_attempt,
                    }
                if attempted_records > 0:
                    failed_records = (
                        sync_attempt.records_rejected + sync_attempt.api_error_count
                    )
                    values["odoo_sync_error_rate"] = (
                        failed_records / attempted_records * 100
                    )
                    details["odoo_sync_error_rate"] = {
                        "numerator": failed_records,
                        "denominator": attempted_records,
                        "sample_count": attempted_records,
                        "source_record": sync_attempt,
                    }
                if sync_attempt.backlog_count > 0:
                    values["odoo_sync_oldest_unsynced_age_seconds"] = max(
                        (now - sync_attempt.oldest_unsynced_at).total_seconds(), 0
                    )
                    details["odoo_sync_oldest_unsynced_age_seconds"] = {
                        "sample_count": sync_attempt.backlog_count
                    }

            data_quality = self.env["saas.data.quality.run"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("event_stream_complete", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if data_quality:
                received = data_quality.events_received
                direct_values = {
                    "events_sent_count": (data_quality.events_sent, data_quality.events_sent),
                    "events_received_count": (received, received),
                    "events_processed_count": (data_quality.events_processed, received),
                    "events_rejected_count": (data_quality.events_rejected, received),
                    "schema_validation_failure_count": (
                        data_quality.schema_failure_count,
                        received,
                    ),
                    "missing_required_field_count": (
                        data_quality.missing_field_count,
                        received,
                    ),
                    "unknown_tenant_mapping_count": (
                        data_quality.unknown_tenant_count,
                        received,
                    ),
                    "event_clock_skew_seconds": (
                        data_quality.clock_skew_seconds,
                        received,
                    ),
                    "event_processing_lag_p95_seconds": (
                        data_quality.processing_lag_p95_seconds,
                        received,
                    ),
                    "dead_letter_event_count": (
                        data_quality.dead_letter_count,
                        received,
                    ),
                }
                for code, (value, sample_count) in direct_values.items():
                    values[code] = value
                    details[code] = {
                        "sample_count": sample_count,
                        "source_record": data_quality,
                    }
                values["event_loss_estimate"] = max(
                    data_quality.events_sent
                    - received
                    - data_quality.retry_adjustment_count,
                    0,
                )
                details["event_loss_estimate"] = {
                    "sample_count": data_quality.events_sent,
                    "source_record": data_quality,
                }
                if received > 0:
                    ratio_values = {
                        "event_duplicate_rate": data_quality.duplicate_count,
                        "late_event_rate": data_quality.late_event_count,
                    }
                    for code, numerator in ratio_values.items():
                        values[code] = numerator / received * 100
                        details[code] = {
                            "numerator": numerator,
                            "denominator": received,
                            "sample_count": received,
                            "source_record": data_quality,
                        }

            metric_quality = self.env["saas.data.quality.run"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("metric_quality_contract_complete", "=", True),
                    ("finished_at", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if metric_quality:
                eligible = metric_quality.eligible_metric_count
                direct_quality_values = {
                    "data_quality_reconciliation_difference": abs(
                        metric_quality.reconciliation_difference
                    ),
                    "data_quality_outlier_count": metric_quality.outlier_count,
                    "unexpected_zero_value_count": metric_quality.unexpected_zero_count,
                    "unexpected_volume_spike_count": (
                        metric_quality.unexpected_volume_spike_count
                    ),
                    "numerator_denominator_violation_count": (
                        metric_quality.numerator_denominator_violation_count
                    ),
                    "negative_value_violation_count": (
                        metric_quality.negative_value_violation_count
                    ),
                    "missing_dimension_count": metric_quality.missing_dimension_count,
                }
                for code, value in direct_quality_values.items():
                    values[code] = value
                    details[code] = {
                        "sample_count": eligible,
                        "source_record": metric_quality,
                    }
                if eligible > 0:
                    ratio_quality_values = {
                        "metric_freshness_rate": metric_quality.fresh_metric_count,
                        "metric_completeness_rate": metric_quality.complete_metric_count,
                        "metric_uniqueness_rate": metric_quality.unique_metric_count,
                        "metric_validity_rate": metric_quality.valid_metric_count,
                        "metric_consistency_rate": metric_quality.consistent_metric_count,
                    }
                    for code, numerator in ratio_quality_values.items():
                        values[code] = numerator / eligible * 100
                        details[code] = {
                            "numerator": numerator,
                            "denominator": eligible,
                            "sample_count": eligible,
                            "source_record": metric_quality,
                        }

            latest_load = self.env["saas.load.test"].search(
                [
                    ("environment_id", "=", environment.id),
                    ("representative", "=", True),
                    ("finished_at", "!=", False),
                    ("architecture_version", "!=", False),
                ],
                order="finished_at desc, id desc",
                limit=1,
            )
            if latest_load:
                values["load_test_age_days"] = age_seconds(latest_load) / 86400
                readiness_values = {
                    "ready": 1,
                    "ready_with_risk": 0.66,
                    "test_stale": 0.33,
                    "not_ready": 0,
                }
                values["capacity_readiness_status"] = readiness_values[latest_load.status]
                details["capacity_readiness_status"] = {"source_record": latest_load}
                safe_load = self.env["saas.load.test"].search(
                    [
                        ("environment_id", "=", environment.id),
                        ("representative", "=", True),
                        ("architecture_version", "=", latest_load.architecture_version),
                        ("status", "in", ["ready", "ready_with_risk"]),
                        ("finished_at", "!=", False),
                        ("concurrent_users", ">", 0),
                    ],
                    order="concurrent_users desc, finished_at desc, id desc",
                    limit=1,
                )
                if safe_load:
                    values["tested_concurrent_users"] = safe_load.concurrent_users
                    details["tested_concurrent_users"] = {"source_record": safe_load}

            for code, spec in metric_specs.items():
                definition = definitions.get(code)
                if not definition or code not in values:
                    omitted.append(f"{environment.code}:{code}")
                    continue
                value = float(values[code])
                detail = details.get(code, {})
                source_record = detail.get("source_record")
                measured_at = source_record.finished_at if source_record else now
                source_updated_at = (
                    source_record.source_updated_at if source_record else now
                )
                status = self._internal_metric_status(definition, value)
                current_values = {
                    "metric_id": definition.id,
                    "environment_id": environment.id,
                    "scope_key": "global",
                    "status": status,
                    "current_value": value,
                    "sample_count": detail.get(
                        "sample_count",
                        1 if code != "open_p0_p1_incidents" else int(value),
                    ),
                    "measured_at": measured_at,
                    "fresh_until": measured_at + timedelta(seconds=definition.freshness_seconds),
                    "source_updated_at": source_updated_at,
                    "drilldown_url": spec["drilldown"],
                }
                if "numerator" in detail:
                    current_values["numerator"] = detail["numerator"]
                if "denominator" in detail:
                    current_values["denominator"] = detail["denominator"]
                current = self.search(
                    [
                        ("metric_id", "=", definition.id),
                        ("environment_id", "=", environment.id),
                        ("scope_key", "=", "global"),
                    ],
                    limit=1,
                )
                if current:
                    current.write(current_values)
                else:
                    current = self.create(current_values)
                current._sync_status_alert()

                if source_record:
                    source_token = hashlib.sha256(
                        source_record.external_key.encode("utf-8")
                    ).hexdigest()[:24]
                    period_start = source_record.started_at
                    period_end = source_record.finished_at
                    history_granularity = "event"
                    external_key = (
                        f"{environment.code}:odoo-internal:{code}:{source_token}"
                    )
                    history_source_updated_at = source_record.source_updated_at
                else:
                    period_start = hour_start
                    period_end = hour_end
                    history_granularity = "hour"
                    external_key = (
                        f"{environment.code}:odoo-internal:{code}:"
                        f"{hour_start.strftime('%Y%m%d%H')}"
                    )
                    history_source_updated_at = now
                history_values = {
                    "metric_id": definition.id,
                    "environment_id": environment.id,
                    "scope_key": "global",
                    "period_start": period_start,
                    "period_end": period_end,
                    "granularity": history_granularity,
                    "value": value,
                    "sample_count": current_values["sample_count"],
                    "status": status,
                    "data_quality_status": "valid",
                    "source_updated_at": history_source_updated_at,
                    "external_key": external_key,
                    "drilldown_url": spec["drilldown"],
                }
                if "numerator" in detail:
                    history_values["numerator"] = detail["numerator"]
                if "denominator" in detail:
                    history_values["denominator"] = detail["denominator"]
                history = self.env["saas.metric.timeseries"].search(
                    [("external_key", "=", external_key)], limit=1
                )
                if history:
                    if history.metric_id != definition or history.environment_id != environment:
                        raise ValidationError(
                            "Internal metric history key belongs to another metric or environment."
                        )
                    history.write(history_values)
                else:
                    self.env["saas.metric.timeseries"].create(history_values)
                refreshed += 1
        return {"refreshed": refreshed, "omitted": omitted}

    @api.constrains("scope_key", "sample_count", "numerator", "denominator")
    def _check_value_contract(self):
        for record in self:
            if not SCOPE_KEY_PATTERN.fullmatch(record.scope_key or ""):
                raise ValidationError("Scope key contains unsupported characters.")
            if record.sample_count < 0:
                raise ValidationError("Sample count cannot be negative.")
            if record.denominator and record.numerator > record.denominator:
                raise ValidationError("Numerator cannot be greater than denominator.")

    @api.model
    def _normalize_dashboard_filters(self, filters):
        if filters in (None, False):
            return {}
        if not isinstance(filters, dict) or len(filters) > 24:
            raise ValidationError("filters must be a bounded object.")
        normalized = {}
        relation_fields = {
            "service_id",
            "region_id",
            "release_id",
            "tenant_id",
            "plan_id",
            "feature_id",
            "integration_id",
            "country_id",
            "currency_id",
        }
        text_fields = {
            "endpoint_group",
            "job_type",
            "acquisition_source",
            "browser",
            "operating_system",
            "device",
            "model_code",
        }
        for field_name in relation_fields:
            raw_value = filters.get(field_name)
            if raw_value in (None, False, ""):
                continue
            if isinstance(raw_value, bool):
                raise ValidationError(f"{field_name} must be a positive record ID.")
            try:
                record_id = int(raw_value)
            except (TypeError, ValueError) as error:
                raise ValidationError(f"{field_name} must be a positive record ID.") from error
            if record_id <= 0:
                raise ValidationError(f"{field_name} must be a positive record ID.")
            normalized[field_name] = record_id
        for field_name in text_fields:
            value = _bounded_text(filters.get(field_name), field_name)
            if value:
                normalized[field_name] = value
        selection_values = {
            "tenant_size_band": {"micro", "small", "medium", "large", "enterprise"},
            "incident_severity": {"p0", "p1", "p2", "p3"},
            "status": {"healthy", "warning", "critical", "unknown"},
            "period": {"current", "24h", "7d", "30d", "90d"},
        }
        for field_name, allowed in selection_values.items():
            value = str(filters.get(field_name) or "").strip().lower()
            if value:
                if value not in allowed:
                    raise ValidationError(f"Invalid {field_name} filter.")
                normalized[field_name] = value
        compare = filters.get("compare_previous")
        if compare not in (None, False, True, 0, 1):
            raise ValidationError("compare_previous must be boolean.")
        normalized["compare_previous"] = bool(compare)
        normalized.setdefault("period", "current")
        return normalized

    @api.model
    def dashboard_filter_options(self):
        def options(model_name, label_field="name", limit=500):
            try:
                records = self.env[model_name].search([], order=f"{label_field}, id", limit=limit)
                return [{"id": record.id, "name": record[label_field]} for record in records]
            except AccessError:
                return []

        try:
            releases = self.env["saas.release"].search(
                [], order="released_at desc, id desc", limit=200
            )
            release_options = [
                {
                    "id": release.id,
                    "name": f"{release.environment_id.name}: {release.version}",
                }
                for release in releases
            ]
        except AccessError:
            release_options = []

        return {
            "dashboards": [
                {"code": dashboard.code, "name": dashboard.name, "priority": dashboard.priority.upper()}
                for dashboard in self.env["saas.dashboard"].search(
                    [("active", "=", True)], order="priority, sequence, id"
                )
            ],
            "services": options("saas.service"),
            "regions": options("saas.region"),
            "releases": release_options,
            "tenants": options("saas.tenant"),
            "plans": options("saas.plan"),
            "features": options("saas.feature"),
            "integrations": options("saas.integration"),
            "countries": options("res.country"),
            "currencies": options("res.currency"),
        }

    @api.model
    def _dashboard_freshness_summary(self, definitions, current_values):
        expected_count = len(definitions)
        expected_refresh_seconds = min(definitions.mapped("freshness_seconds"), default=0)
        status_rank = {"fresh": 0, "delayed": 1, "stale": 2}
        summary = {}
        for environment_code in ("develop", "main"):
            points = current_values.filtered(
                lambda value: value.environment_id.code == environment_code
            )
            observed_statuses = points.mapped("freshness_status")
            worst_observed = max(
                observed_statuses,
                key=lambda status: status_rank.get(status, 2),
                default="missing",
            )
            complete = bool(expected_count) and len(points) == expected_count
            measured_at = min(points.mapped("measured_at"), default=False)
            newest_measured_at = max(points.mapped("measured_at"), default=False)
            summary[environment_code] = {
                "status": worst_observed if complete else "missing",
                "worstObservedStatus": worst_observed,
                "measuredAt": fields.Datetime.to_string(measured_at) if measured_at else None,
                "newestMeasuredAt": (
                    fields.Datetime.to_string(newest_measured_at)
                    if newest_measured_at
                    else None
                ),
                "expectedRefreshSeconds": expected_refresh_seconds or None,
                "presentMetricCount": len(points),
                "expectedMetricCount": expected_count,
                "completenessPercent": (
                    round(len(points) / expected_count * 100, 2) if expected_count else 0
                ),
            }
        return summary

    @api.model
    def dashboard_payload(self, dashboard_code=None, scope_key="global", filters=None):
        if not SCOPE_KEY_PATTERN.fullmatch(scope_key or ""):
            raise ValidationError("Invalid scope key.")
        normalized_filters = self._normalize_dashboard_filters(filters)
        scoped_filter_fields = {
            "service_id", "region_id", "release_id", "tenant_id", "plan_id",
            "feature_id", "integration_id", "country_id", "currency_id",
            "tenant_size_band", "endpoint_group", "job_type", "acquisition_source",
            "browser", "operating_system", "device", "incident_severity", "model_code",
        }
        effective_scope_key = None if scoped_filter_fields.intersection(normalized_filters) else scope_key
        base_url = self.env["ir.config_parameter"].sudo().get_param(
            "web.base.url", "http://localhost"
        ).rstrip("/")
        dashboards = self.env["saas.dashboard"].search(
            [("active", "=", True)] + ([('code', '=', dashboard_code)] if dashboard_code else []),
            order="priority, sequence, id",
        )
        definitions = self.env["saas.metric.definition"].search(
            [("active", "=", True), ("dashboard_ids", "in", dashboards.ids)],
            order="sequence, code",
        )
        current_domain = [("metric_id", "in", definitions.ids)]
        if effective_scope_key:
            current_domain.append(("scope_key", "=", effective_scope_key))
        for field_name in (
            "service_id",
            "region_id",
            "release_id",
            "tenant_id",
            "plan_id",
            "feature_id",
            "integration_id",
            "country_id",
            "currency_id",
            "tenant_size_band",
            "endpoint_group",
            "job_type",
            "acquisition_source",
            "browser",
            "operating_system",
            "device",
            "model_code",
            "incident_severity",
            "status",
        ):
            if field_name in normalized_filters:
                current_domain.append((field_name, "=", normalized_filters[field_name]))
        current_values = self.search(current_domain)
        by_metric_environment = {
            (value.metric_id.id, value.environment_id.code): value for value in current_values
        }
        trend_by_metric_environment = {}
        period_key = normalized_filters.get("period", "current")
        if period_key != "current" and definitions:
            now = fields.Datetime.now()
            period_delta = {
                "24h": timedelta(hours=24),
                "7d": timedelta(days=7),
                "30d": timedelta(days=30),
                "90d": timedelta(days=90),
            }[period_key]
            period_start = now - period_delta
            history_start = period_start - period_delta if normalized_filters.get(
                "compare_previous"
            ) else period_start
            history_domain = [
                ("metric_id", "in", definitions.ids),
                ("period_start", ">=", history_start),
                ("period_start", "<=", now),
            ]
            if effective_scope_key:
                history_domain.append(("scope_key", "=", effective_scope_key))
            for field_name in (
                "service_id",
                "region_id",
                "release_id",
                "tenant_id",
                "plan_id",
                "feature_id",
                "integration_id",
                "country_id",
                "currency_id",
                "tenant_size_band",
                "endpoint_group",
                "job_type",
                "acquisition_source",
                "browser",
                "operating_system",
                "device",
                "model_code",
                "incident_severity",
                "status",
            ):
                if field_name in normalized_filters:
                    history_domain.append((field_name, "=", normalized_filters[field_name]))
            history = self.env["saas.metric.timeseries"].search(
                history_domain, order="period_start desc, id desc", limit=5000
            )
            grouped_history = {}
            for point in history:
                grouped_history.setdefault(
                    (point.metric_id.id, point.environment_id.code), []
                ).append(point)
            for key, points in grouped_history.items():
                current_period_points = [point for point in points if point.period_start >= period_start]
                previous_period_points = [point for point in points if point.period_start < period_start]
                chronological = list(reversed(current_period_points[:120]))
                if len(chronological) > 24:
                    step = max(len(chronological) // 24, 1)
                    chronological = chronological[::step][-24:]
                previous = previous_period_points[0] if previous_period_points else False
                trend_by_metric_environment[key] = {
                    "period": period_key,
                    "points": [
                        {
                            "value": point.value,
                            "at": fields.Datetime.to_string(point.period_start),
                        }
                        for point in chronological
                    ],
                    "previousValue": previous.value if previous else None,
                }
        try:
            open_alerts = self.env["saas.alert"].search(
                [
                    ("metric_id", "in", definitions.ids),
                    ("status", "!=", "resolved"),
                ]
            )
        except AccessError:
            open_alerts = self.env["saas.alert"]
        alert_summary = {}
        severity_rank = {"p0": 4, "p1": 3, "p2": 2, "p3": 1}
        for alert in open_alerts:
            key = (alert.metric_id.id, alert.environment_id.code)
            summary = alert_summary.setdefault(key, {"count": 0, "severity": False})
            summary["count"] += 1
            if severity_rank.get(alert.severity, 0) > severity_rank.get(summary["severity"], 0):
                summary["severity"] = alert.severity

        def serialized(value):
            if not value:
                return None
            trend = trend_by_metric_environment.get(
                (value.metric_id.id, value.environment_id.code),
                {"period": period_key, "points": [], "previousValue": None},
            )
            previous_value = trend["previousValue"]
            comparison = None
            if previous_value is not None:
                delta = value.current_value - previous_value
                comparison = {
                    "previousValue": previous_value,
                    "delta": delta,
                    "percent": (delta / abs(previous_value) * 100) if previous_value else None,
                    "period": period_key,
                }
            return {
                "value": value.current_value,
                "numerator": value.numerator,
                "denominator": value.denominator,
                "sampleCount": value.sample_count,
                "status": value.status,
                "freshness": value.freshness_status,
                "measuredAt": fields.Datetime.to_string(value.measured_at),
                "release": value.release_id.version or None,
                "drilldownUrl": value.drilldown_url or value.metric_id.drilldown_url or None,
                "alerts": alert_summary.get(
                    (value.metric_id.id, value.environment_id.code),
                    {"count": 0, "severity": False},
                ),
                "trend": trend["points"],
                "comparison": comparison,
            }

        sections = []
        for dashboard in dashboards:
            rows = []
            for definition in definitions.filtered(lambda item: dashboard in item.dashboard_ids):
                rows.append(
                    {
                        "code": definition.code,
                        "name": definition.name,
                        "description": definition.description,
                        "unit": definition.unit,
                        "direction": definition.direction,
                        "target": definition.target_value,
                        "warning": definition.warning_value,
                        "critical": definition.critical_value,
                        "owner": definition.owner,
                        "runbook": definition.runbook,
                        "runbookUrl": definition.runbook_url
                        or f"{base_url}/web#id={definition.id}&model=saas.metric.definition&view_type=form",
                        "definitionDrilldownUrl": definition.drilldown_url
                        or (
                            f"{base_url}/web#model=saas.metric.timeseries&view_type=list"
                            f"&domain=[('metric_id','=',{definition.id})]"
                        ),
                        "develop": serialized(by_metric_environment.get((definition.id, "develop"))),
                        "main": serialized(by_metric_environment.get((definition.id, "main"))),
                    }
                )
            sections.append(
                {
                    "code": dashboard.code,
                    "name": dashboard.name,
                    "purpose": dashboard.purpose,
                    "owner": dashboard.owner,
                    "priority": dashboard.priority.upper(),
                    "rows": rows,
                }
            )
        return {
            "generatedAt": fields.Datetime.to_string(fields.Datetime.now()),
            "scopeKey": effective_scope_key or "filtered",
            "environments": ["develop", "main"],
            "appliedFilters": normalized_filters,
            "freshnessSummary": self._dashboard_freshness_summary(
                definitions, current_values
            ),
            "sections": sections,
        }

    @api.model
    def ingest_metric_batch(self, payload):
        if not (
            self.env.user.has_group("arcigy_saas_control_center.group_saas_integration_bot")
            or self.env.user.has_group("arcigy_saas_control_center.group_saas_administrator")
        ):
            raise AccessError("Only the SaaS integration bot can ingest metrics.")
        self = self.sudo()
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object.")
        unknown_payload_fields = set(payload) - METRIC_BATCH_FIELDS
        if unknown_payload_fields:
            raise ValidationError(
                f"Unsupported metric payload fields: {', '.join(sorted(unknown_payload_fields))}."
            )
        raw_metrics = payload.get("metrics")
        if not isinstance(raw_metrics, list) or not raw_metrics or len(raw_metrics) > 500:
            raise ValidationError("metrics must contain between 1 and 500 items.")
        for item in raw_metrics:
            if not isinstance(item, dict):
                raise ValidationError("Every metric item must be an object.")
            unknown_metric_fields = set(item) - METRIC_ITEM_FIELDS
            if unknown_metric_fields:
                raise ValidationError(
                    f"Unsupported metric item fields: {', '.join(sorted(unknown_metric_fields))}."
                )
        environment_code = str(payload.get("environment") or "").strip().lower()
        if environment_code not in {"develop", "main"}:
            raise ValidationError("environment must be develop or main.")
        environment = self.env["saas.environment"].search([("code", "=", environment_code)], limit=1)
        if not environment:
            raise ValidationError("Configured SaaS environment was not found.")
        source_updated_at = _required_external_utc_datetime(
            payload.get("source_updated_at"), "source_updated_at"
        )
        if source_updated_at > fields.Datetime.now() + timedelta(minutes=5):
            raise ValidationError("source_updated_at is too far in the future.")
        sync_run = self.env["saas.sync.run"].create(
            {
                "name": f"Arcigy metric sync {environment_code} {fields.Datetime.to_string(source_updated_at)}",
                "environment_id": environment.id,
                "started_at": source_updated_at,
                "status": "running",
                "records_read": len(raw_metrics),
            }
        )
        release = False
        release_version = str(payload.get("release_version") or "").strip()
        if release_version:
            release = self.env["saas.release"].search(
                [("environment_id", "=", environment.id), ("version", "=", release_version)],
                limit=1,
            )
            if not release:
                release = self.env["saas.release"].create(
                    {
                        "environment_id": environment.id,
                        "version": release_version[:128],
                        "commit_sha": str(payload.get("commit_sha") or "")[:64] or False,
                        "released_at": source_updated_at,
                    }
                )
        created = updated = stale_skipped = history_created = history_updated = 0
        alerts_opened = alerts_resolved = 0

        def dimension_record(model_name, code, field_name="code", include_inactive=False):
            normalized = _bounded_text(code, field_name)
            if not normalized:
                return self.env[model_name]
            model = self.env[model_name]
            if include_inactive:
                model = model.with_context(active_test=False)
            record = model.search([(field_name, "=", normalized)], limit=1)
            if not record:
                raise ValidationError(f"Unknown {model_name} {field_name}: {normalized}.")
            return record

        for item in raw_metrics:
            metric_code = str(item.get("code") or "").strip()
            definition = self.env["saas.metric.definition"].search(
                [("code", "=", metric_code), ("active", "=", True)], limit=1
            )
            if not definition:
                raise ValidationError(f"Unknown metric code: {metric_code or '<empty>'}.")
            scope_key = str(item.get("scope_key") or "global").strip()
            if not SCOPE_KEY_PATTERN.fullmatch(scope_key):
                raise ValidationError("Invalid scope_key.")
            measured_at = (
                _required_external_utc_datetime(item.get("measured_at"), "measured_at")
                if item.get("measured_at")
                else source_updated_at
            )
            if measured_at > source_updated_at + timedelta(minutes=5):
                raise ValidationError("measured_at is newer than the source watermark.")
            raw_freshness_seconds = item.get("freshness_seconds", definition.freshness_seconds)
            if isinstance(raw_freshness_seconds, bool) or not isinstance(raw_freshness_seconds, int):
                raise ValidationError("freshness_seconds must be an integer.")
            freshness_seconds = raw_freshness_seconds
            if freshness_seconds < 1 or freshness_seconds > 604800:
                raise ValidationError("freshness_seconds must be between 1 and 604800.")
            numerator = _finite_number(item.get("numerator"), "numerator")
            denominator = _finite_number(item.get("denominator"), "denominator")
            if denominator is not False and numerator is not False and numerator > denominator:
                raise ValidationError("numerator cannot be greater than denominator.")
            sample_count = item.get("sample_count", 0)
            if isinstance(sample_count, bool) or not isinstance(sample_count, int):
                raise ValidationError("sample_count must be an integer.")
            if sample_count < 0:
                raise ValidationError("sample_count cannot be negative.")
            status = str(item.get("status") or "unknown").lower()
            if status not in {item[0] for item in STATUS_SELECTION}:
                raise ValidationError("Invalid metric status.")
            tenant = dimension_record("saas.tenant", item.get("tenant_external_id"), "external_id")
            plan = dimension_record("saas.plan", item.get("plan_code"))
            if tenant and plan and tenant.plan_id and tenant.plan_id != plan:
                raise ValidationError("plan_code does not match the configured tenant plan.")
            if tenant and not plan:
                plan = tenant.plan_id
            country = dimension_record("res.country", item.get("country_code"), "code")
            if tenant and country and tenant.country_id and tenant.country_id != country:
                raise ValidationError("country_code does not match the configured tenant country.")
            if tenant and not country:
                country = tenant.country_id
            size_band = str(item.get("tenant_size_band") or "").strip().lower() or (
                tenant.size_band if tenant else False
            )
            if size_band and size_band not in {"micro", "small", "medium", "large", "enterprise"}:
                raise ValidationError("Invalid tenant_size_band.")
            incident_severity = str(item.get("incident_severity") or "").strip().lower()
            if incident_severity and incident_severity not in {"p0", "p1", "p2", "p3"}:
                raise ValidationError("Invalid incident_severity.")
            values = {
                "metric_id": definition.id,
                "environment_id": environment.id,
                "service_id": dimension_record("saas.service", item.get("service_code")).id or False,
                "tenant_id": tenant.id or False,
                "plan_id": plan.id or False,
                "region_id": dimension_record("saas.region", item.get("region_code")).id or False,
                "release_id": release.id if release else False,
                "feature_id": dimension_record("saas.feature", item.get("feature_code")).id or False,
                "integration_id": dimension_record(
                    "saas.integration", item.get("integration_code")
                ).id
                or False,
                "country_id": country.id or False,
                "currency_id": dimension_record(
                    "res.currency", item.get("currency_code"), "name", include_inactive=True
                ).id
                or False,
                "tenant_size_band": size_band or False,
                "endpoint_group": _bounded_text(item.get("endpoint_group"), "endpoint_group"),
                "job_type": _bounded_text(item.get("job_type"), "job_type"),
                "acquisition_source": _bounded_text(
                    item.get("acquisition_source"), "acquisition_source"
                ),
                "browser": _bounded_text(item.get("browser"), "browser"),
                "operating_system": _bounded_text(
                    item.get("operating_system"), "operating_system"
                ),
                "device": _bounded_text(item.get("device"), "device"),
                "model_code": _bounded_text(item.get("model_code"), "model_code"),
                "incident_severity": incident_severity or False,
                "scope_key": scope_key,
                "status": status,
                "current_value": _finite_number(item.get("value"), "value", required=True),
                "numerator": numerator,
                "denominator": denominator,
                "sample_count": sample_count,
                "measured_at": measured_at,
                "fresh_until": measured_at + timedelta(seconds=freshness_seconds),
                "source_updated_at": source_updated_at,
                "drilldown_url": _safe_url(item.get("drilldown_url"), "drilldown_url"),
            }
            current = self.search(
                [
                    ("metric_id", "=", definition.id),
                    ("environment_id", "=", environment.id),
                    ("scope_key", "=", scope_key),
                ],
                limit=1,
            )
            if current and current.measured_at > measured_at:
                # Delayed delivery may still populate history, but it must never roll the
                # current environment column back to an older observation.
                stale_skipped += 1
            elif current:
                current.write(values)
                updated += 1
            else:
                current = self.create(values)
                created += 1
            if current and current.measured_at == measured_at:
                opened, resolved = current._sync_status_alert()
                alerts_opened += opened
                alerts_resolved += resolved
            historical_fields = {"external_key", "period_start", "period_end", "granularity"}
            supplied_historical_fields = {
                field_name
                for field_name in historical_fields
                if item.get(field_name) not in (None, "")
            }
            if supplied_historical_fields and supplied_historical_fields != historical_fields:
                missing_historical_fields = historical_fields - supplied_historical_fields
                raise ValidationError(
                    "Historical metric fields must be supplied together; missing: "
                    f"{', '.join(sorted(missing_historical_fields))}."
                )
            external_key = str(item.get("external_key") or "").strip()
            if external_key:
                if len(external_key) > 255 or not external_key.startswith(f"{environment_code}:"):
                    raise ValidationError("external_key must be environment-prefixed and at most 255 characters.")
                period_start = _required_external_utc_datetime(
                    item.get("period_start"), "period_start"
                )
                period_end = _required_external_utc_datetime(item.get("period_end"), "period_end")
                if period_end <= period_start:
                    raise ValidationError("period_end must be after period_start.")
                if period_end > source_updated_at + timedelta(minutes=5):
                    raise ValidationError("period_end is newer than the source watermark.")
                granularity = str(item.get("granularity") or "").strip()
                if granularity not in {"5m", "hour", "day", "month"}:
                    raise ValidationError("Historical metric granularity is invalid.")
                historical_values = {
                    "metric_id": definition.id,
                    "environment_id": environment.id,
                    "service_id": values["service_id"],
                    "tenant_id": values["tenant_id"],
                    "plan_id": values["plan_id"],
                    "region_id": values["region_id"],
                    "release_id": release.id if release else False,
                    "feature_id": values["feature_id"],
                    "integration_id": values["integration_id"],
                    "country_id": values["country_id"],
                    "currency_id": values["currency_id"],
                    "tenant_size_band": values["tenant_size_band"],
                    "endpoint_group": values["endpoint_group"],
                    "job_type": values["job_type"],
                    "acquisition_source": values["acquisition_source"],
                    "browser": values["browser"],
                    "operating_system": values["operating_system"],
                    "device": values["device"],
                    "model_code": values["model_code"],
                    "incident_severity": values["incident_severity"],
                    "scope_key": scope_key,
                    "period_start": period_start,
                    "period_end": period_end,
                    "granularity": granularity,
                    "value": values["current_value"],
                    "numerator": numerator,
                    "denominator": denominator,
                    "sample_count": sample_count,
                    "status": status,
                    "data_quality_status": "valid",
                    "source_updated_at": source_updated_at,
                    "external_key": external_key,
                    "drilldown_url": values["drilldown_url"],
                }
                historical = self.env["saas.metric.timeseries"].search(
                    [("external_key", "=", external_key)], limit=1
                )
                if historical:
                    if historical.environment_id != environment or historical.metric_id != definition:
                        raise ValidationError("external_key already belongs to another metric or environment.")
                    historical.write(historical_values)
                    history_updated += 1
                else:
                    self.env["saas.metric.timeseries"].create(historical_values)
                    history_created += 1
        sync_run.write(
            {
                "finished_at": fields.Datetime.now(),
                "status": "success",
                "records_created": created + history_created,
                "records_updated": updated + history_updated,
            }
        )
        return {
            "ok": True,
            "environment": environment_code,
            "created": created,
            "updated": updated,
            "stale_skipped": stale_skipped,
            "history_created": history_created,
            "history_updated": history_updated,
            "alerts_opened": alerts_opened,
            "alerts_resolved": alerts_resolved,
            "processed": len(raw_metrics),
        }


class SaasMetricTimeseries(models.Model):
    _name = "saas.metric.timeseries"
    _description = "Historical SaaS metric value"
    _order = "period_start desc, id desc"

    metric_id = fields.Many2one(
        "saas.metric.definition", required=True, ondelete="restrict", index=True
    )
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    service_id = fields.Many2one("saas.service", ondelete="restrict", index=True)
    tenant_id = fields.Many2one("saas.tenant", ondelete="restrict", index=True)
    plan_id = fields.Many2one("saas.plan", ondelete="restrict", index=True)
    region_id = fields.Many2one("saas.region", ondelete="restrict", index=True)
    release_id = fields.Many2one("saas.release", ondelete="restrict", index=True)
    feature_id = fields.Many2one("saas.feature", ondelete="restrict", index=True)
    integration_id = fields.Many2one("saas.integration", ondelete="restrict", index=True)
    country_id = fields.Many2one("res.country", ondelete="restrict", index=True)
    tenant_size_band = fields.Selection(
        [
            ("micro", "Micro"),
            ("small", "Small"),
            ("medium", "Medium"),
            ("large", "Large"),
            ("enterprise", "Enterprise"),
        ],
        index=True,
    )
    endpoint_group = fields.Char(index=True)
    job_type = fields.Char(index=True)
    acquisition_source = fields.Char(index=True)
    browser = fields.Char(index=True)
    operating_system = fields.Char(index=True)
    device = fields.Char(index=True)
    model_code = fields.Char(index=True)
    incident_severity = fields.Selection(
        [("p0", "P0"), ("p1", "P1"), ("p2", "P2"), ("p3", "P3")],
        index=True,
    )
    scope_key = fields.Char(required=True, default="global", index=True)
    period_start = fields.Datetime(required=True, index=True)
    period_end = fields.Datetime(required=True, index=True)
    granularity = fields.Selection(
        [
            ("5m", "5 minutes"),
            ("hour", "Hour"),
            ("day", "Day"),
            ("month", "Month"),
            ("event", "Event"),
        ],
        required=True,
        index=True,
    )
    value = fields.Float(required=True)
    numerator = fields.Float()
    denominator = fields.Float()
    sample_count = fields.Integer()
    minimum = fields.Float()
    maximum = fields.Float()
    p50 = fields.Float()
    p95 = fields.Float()
    p99 = fields.Float()
    currency_id = fields.Many2one("res.currency", ondelete="restrict")
    status = fields.Selection(STATUS_SELECTION, required=True, default="unknown", index=True)
    data_quality_status = fields.Selection(
        [("valid", "Valid"), ("warning", "Warning"), ("invalid", "Invalid")],
        required=True,
        default="valid",
        index=True,
    )
    source_updated_at = fields.Datetime()
    external_key = fields.Char(required=True, index=True)
    drilldown_url = fields.Char()

    _external_key_unique = models.Constraint(
        "UNIQUE(external_key)", "Timeseries external key must be unique."
    )

    @api.model
    def retention_preview(self):
        if not (
            self.env.user.has_group("arcigy_saas_control_center.group_saas_administrator")
            or self.env.user.has_group("base.group_system")
        ):
            raise AccessError("Only a SaaS administrator can preview retention candidates.")
        self = self.sudo()
        now = fields.Datetime.now()
        candidates = []
        total = 0
        for definition in self.env["saas.metric.definition"].search([], order="code"):
            cutoff = now - timedelta(days=definition.retention_days)
            count = self.search_count(
                [("metric_id", "=", definition.id), ("period_end", "<", cutoff)]
            )
            if count:
                candidates.append(
                    {
                        "metricCode": definition.code,
                        "retentionDays": definition.retention_days,
                        "cutoff": fields.Datetime.to_string(cutoff),
                        "candidateCount": count,
                    }
                )
                total += count
        return {
            "generatedAt": fields.Datetime.to_string(now),
            "candidateCount": total,
            "metrics": candidates,
            "destructiveActionEnabled": False,
            "reason": "Deletion requires an approved retention policy, backup evidence, dry run and rollback plan.",
        }

    @api.constrains("period_start", "period_end", "scope_key", "sample_count")
    def _check_timeseries_contract(self):
        for record in self:
            if record.period_end <= record.period_start:
                raise ValidationError("Period end must be after period start.")
            if not SCOPE_KEY_PATTERN.fullmatch(record.scope_key or ""):
                raise ValidationError("Invalid scope key.")
            if record.sample_count < 0:
                raise ValidationError("Sample count cannot be negative.")
