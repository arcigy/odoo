import math
from datetime import datetime, timezone
from urllib.parse import urlparse

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError


STATUS_SELECTION = [
    ("healthy", "Healthy"),
    ("warning", "Warning"),
    ("critical", "Critical"),
    ("unknown", "Unknown"),
]
QUALITY_SELECTION = [
    ("valid", "Valid"),
    ("warning", "Warning"),
    ("invalid", "Invalid"),
]

AGGREGATE_ALLOWED_FIELDS = {
    "saas.tenant.daily": {
        "active_users", "active_seats", "purchased_seats", "core_actions", "error_rate",
        "p95_seconds", "incident_count", "failed_jobs", "mrr", "operational_cost",
        "health_score", "health_status", "health_reasons",
    },
    "saas.endpoint.hourly": {
        "method", "endpoint_group", "slo_class", "request_count", "success_count",
        "error_count", "timeout_count", "rate_limited_count", "duration_seconds_sum",
        "p50_seconds", "p95_seconds", "p99_seconds", "request_bytes_p95", "response_bytes_p95",
    },
    "saas.database.hourly": {
        "open_connections", "active_connections", "waiting_connections", "max_connections",
        "pool_utilization", "pool_wait_p95_seconds", "pool_timeout_count", "query_p95_seconds",
        "slow_query_count", "lock_wait_count", "deadlock_count", "rollback_count",
        "storage_bytes", "storage_growth_bytes", "cache_hit_ratio", "wal_lag_bytes",
        "replication_lag_seconds",
    },
    "saas.cache.hourly": {
        "namespace", "request_count", "hit_count", "miss_count", "timeout_count", "error_count",
        "hit_ratio", "get_p95_seconds", "set_p95_seconds", "evicted_keys",
        "invalidation_lag_seconds", "stale_served_count", "consistency_incident_count",
    },
    "saas.queue.hourly": {
        "queue_name", "job_type", "queue_depth", "oldest_age_seconds", "enqueue_rate",
        "processing_rate", "drain_time_seconds", "started_count", "completed_count",
        "failed_count", "retry_count", "duplicate_suppressed_count", "idempotency_conflict_count",
        "dlq_size", "worker_count",
    },
    "saas.dependency.hourly": {
        "request_count", "success_count", "timeout_count", "retry_count", "p50_seconds",
        "p95_seconds", "p99_seconds", "quota_utilization", "cost",
    },
    "saas.cost.daily": {
        "provider", "category", "amount", "active_users", "active_tenants", "core_actions",
        "cost_per_active_user", "cost_per_active_tenant", "cost_per_core_action",
    },
    "saas.product.daily": {
        "active_users", "active_tenants", "core_actions", "signup_count", "activated_tenants",
        "eligible_new_tenants", "activation_rate", "retention_rate", "feature_adoption_rate",
        "time_to_value_p50_seconds", "time_to_value_p90_seconds",
    },
    "saas.security.daily": {
        "login_attempts", "login_failures", "rate_limit_events", "suspicious_login_count",
        "cross_tenant_denied_count", "confirmed_cross_tenant_exposure_count",
        "privileged_action_count", "webhook_signature_failure_count",
        "critical_vulnerability_count", "high_vulnerability_count", "secret_finding_count",
        "audit_delivery_failure_count",
    },
    "saas.capacity.daily": {
        "peak_rps", "tested_safe_rps", "current_concurrent_users", "tested_concurrent_users",
        "capacity_headroom", "db_connection_headroom", "cpu_headroom", "memory_headroom",
        "storage_days_to_full", "queue_drain_headroom", "readiness",
    },
}


def _aggregate_datetime(value, field_name):
    if not value:
        return fields.Datetime.now()
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValidationError(f"{field_name} must be an ISO-8601 timestamp.") from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def _bounded_aggregate_text(value, field_name, maximum=4000):
    normalized = str(value or "").strip()
    if len(normalized) > maximum or any(ord(char) < 32 and char not in "\n\t" for char in normalized):
        raise ValidationError(f"{field_name} is invalid or too long.")
    return normalized or False


class SaasAggregateMixin(models.AbstractModel):
    _name = "saas.aggregate.mixin"
    _description = "Shared SaaS aggregate fields"

    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    service_id = fields.Many2one("saas.service", ondelete="restrict", index=True)
    region_id = fields.Many2one("saas.region", ondelete="restrict", index=True)
    tenant_id = fields.Many2one("saas.tenant", ondelete="restrict", index=True)
    period_start = fields.Datetime(required=True, index=True)
    period_end = fields.Datetime(required=True, index=True)
    status = fields.Selection(STATUS_SELECTION, required=True, default="unknown", index=True)
    data_quality_status = fields.Selection(
        QUALITY_SELECTION, required=True, default="valid", index=True
    )
    source_updated_at = fields.Datetime(required=True)
    external_key = fields.Char(required=True, index=True)
    drilldown_url = fields.Char()

    _external_key_unique = models.Constraint(
        "UNIQUE(external_key)", "Aggregate external key must be unique."
    )

    @api.constrains("period_start", "period_end", "external_key", "drilldown_url", "environment_id")
    def _check_period(self):
        for record in self:
            if record.period_end <= record.period_start:
                raise ValidationError("Aggregate period end must be after period start.")
            if len(record.external_key or "") > 255 or not record.external_key.startswith(
                f"{record.environment_id.code}:"
            ):
                raise ValidationError("Aggregate external key must be environment-prefixed.")
            if record.drilldown_url:
                parsed = urlparse(record.drilldown_url)
                if (
                    len(record.drilldown_url) > 1024
                    or parsed.scheme not in {"https", "http"}
                    or not parsed.netloc
                    or parsed.username
                    or parsed.password
                ):
                    raise ValidationError("Drilldown URL must be http(s) without credentials.")

    @api.model
    def ingest_aggregate_batch(self, payload):
        if not (
            self.env.user.has_group("arcigy_saas_control_center.group_saas_integration_bot")
            or self.env.user.has_group("arcigy_saas_control_center.group_saas_administrator")
        ):
            raise AccessError("Only the SaaS integration bot can ingest aggregates.")
        allowed_fields = AGGREGATE_ALLOWED_FIELDS.get(self._name)
        if not allowed_fields:
            raise ValidationError("This model is not an approved aggregate ingest target.")
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object.")
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 500:
            raise ValidationError("items must contain between 1 and 500 records.")
        environment_code = str(payload.get("environment") or "").strip().lower()
        if environment_code not in {"develop", "main"}:
            raise ValidationError("environment must be develop or main.")
        model = self.sudo()
        environment = model.env["saas.environment"].search(
            [("code", "=", environment_code)], limit=1
        )
        if not environment:
            raise ValidationError("Configured SaaS environment was not found.")
        source_updated_at = _aggregate_datetime(
            payload.get("source_updated_at"), "source_updated_at"
        )
        sync_run = model.env["saas.sync.run"].create(
            {
                "name": f"{model._name} sync {environment_code} {fields.Datetime.to_string(source_updated_at)}",
                "environment_id": environment.id,
                "started_at": source_updated_at,
                "status": "running",
                "records_read": len(raw_items),
            }
        )

        def dimension(model_name, code, lookup="code"):
            normalized = _bounded_aggregate_text(code, lookup, 120)
            if not normalized:
                return model.env[model_name]
            record = model.env[model_name].search([(lookup, "=", normalized)], limit=1)
            if not record:
                raise ValidationError(f"Unknown {model_name} {lookup}: {normalized}.")
            return record

        created = updated = 0
        common_input_fields = {
            "external_key", "period_start", "period_end", "status", "data_quality_status",
            "drilldown_url", "service_code", "tenant_external_id", "plan_code", "region_code",
            "feature_code", "integration_code", "currency_code",
        }
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValidationError("Every aggregate item must be an object.")
            unknown = set(raw_item) - allowed_fields - common_input_fields
            if unknown:
                raise ValidationError(f"Unsupported aggregate fields: {', '.join(sorted(unknown))}.")
            external_key = _bounded_aggregate_text(raw_item.get("external_key"), "external_key", 255)
            if not external_key or not external_key.startswith(f"{environment_code}:"):
                raise ValidationError("external_key must be environment-prefixed.")
            period_start = _aggregate_datetime(raw_item.get("period_start"), "period_start")
            period_end = _aggregate_datetime(raw_item.get("period_end"), "period_end")
            if period_end <= period_start:
                raise ValidationError("period_end must be after period_start.")
            status = str(raw_item.get("status") or "unknown").strip().lower()
            quality = str(raw_item.get("data_quality_status") or "valid").strip().lower()
            if status not in {value for value, _label in STATUS_SELECTION}:
                raise ValidationError("Invalid aggregate status.")
            if quality not in {value for value, _label in QUALITY_SELECTION}:
                raise ValidationError("Invalid aggregate data quality status.")
            values = {
                "environment_id": environment.id,
                "service_id": dimension("saas.service", raw_item.get("service_code")).id or False,
                "tenant_id": dimension(
                    "saas.tenant", raw_item.get("tenant_external_id"), "external_id"
                ).id
                or False,
                "region_id": dimension("saas.region", raw_item.get("region_code")).id or False,
                "period_start": period_start,
                "period_end": period_end,
                "status": status,
                "data_quality_status": quality,
                "source_updated_at": source_updated_at,
                "external_key": external_key,
                "drilldown_url": _bounded_aggregate_text(
                    raw_item.get("drilldown_url"), "drilldown_url", 1024
                ),
            }
            relation_inputs = {
                "plan_id": ("saas.plan", raw_item.get("plan_code"), "code"),
                "feature_id": ("saas.feature", raw_item.get("feature_code"), "code"),
                "integration_id": (
                    "saas.integration", raw_item.get("integration_code"), "code"
                ),
                "currency_id": ("res.currency", raw_item.get("currency_code"), "name"),
            }
            for field_name, (model_name, code, lookup) in relation_inputs.items():
                if field_name in model._fields:
                    related_record = dimension(model_name, code, lookup)
                    if related_record:
                        values[field_name] = related_record.id
                    elif model._fields[field_name].required:
                        raise ValidationError(f"{field_name} is required for {model._name}.")
            for field_name in allowed_fields:
                if field_name not in raw_item:
                    continue
                field = model._fields[field_name]
                value = raw_item[field_name]
                if field.type in {"integer", "float", "monetary"}:
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
                    values[field_name] = _bounded_aggregate_text(value, field_name)
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
        sync_run.write(
            {
                "finished_at": fields.Datetime.now(),
                "status": "success",
                "records_created": created,
                "records_updated": updated,
            }
        )
        return {
            "ok": True,
            "model": model._name,
            "environment": environment_code,
            "created": created,
            "updated": updated,
            "processed": len(raw_items),
        }


class SaasTenantDaily(models.Model):
    _name = "saas.tenant.daily"
    _description = "Daily SaaS tenant aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, tenant_id"

    plan_id = fields.Many2one("saas.plan", ondelete="restrict", index=True)
    active_users = fields.Integer()
    active_seats = fields.Integer()
    purchased_seats = fields.Integer()
    core_actions = fields.Integer()
    error_rate = fields.Float()
    p95_seconds = fields.Float()
    incident_count = fields.Integer()
    failed_jobs = fields.Integer()
    mrr = fields.Monetary(currency_field="currency_id")
    operational_cost = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency", required=True, default=lambda self: self.env.company.currency_id
    )
    health_score = fields.Float()
    health_status = fields.Selection(
        [("healthy", "Healthy"), ("watch", "Watch"), ("at_risk", "At risk"), ("critical", "Critical")],
        index=True,
    )
    health_reasons = fields.Text()


class SaasEndpointHourly(models.Model):
    _name = "saas.endpoint.hourly"
    _description = "Hourly SaaS endpoint aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, endpoint_group"

    method = fields.Char(required=True, index=True)
    endpoint_group = fields.Char(required=True, index=True)
    slo_class = fields.Selection(
        [("critical", "Critical"), ("high_fast", "High fast"), ("high_slow", "High slow"), ("low", "Low"), ("no_slo", "No SLO")],
        required=True,
        default="no_slo",
    )
    request_count = fields.Integer()
    success_count = fields.Integer()
    error_count = fields.Integer()
    timeout_count = fields.Integer()
    rate_limited_count = fields.Integer()
    duration_seconds_sum = fields.Float()
    p50_seconds = fields.Float()
    p95_seconds = fields.Float()
    p99_seconds = fields.Float()
    request_bytes_p95 = fields.Float()
    response_bytes_p95 = fields.Float()


class SaasDatabaseHourly(models.Model):
    _name = "saas.database.hourly"
    _description = "Hourly SaaS database aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, service_id"

    open_connections = fields.Integer()
    active_connections = fields.Integer()
    waiting_connections = fields.Integer()
    max_connections = fields.Integer()
    pool_utilization = fields.Float()
    pool_wait_p95_seconds = fields.Float()
    pool_timeout_count = fields.Integer()
    query_p95_seconds = fields.Float()
    slow_query_count = fields.Integer()
    lock_wait_count = fields.Integer()
    deadlock_count = fields.Integer()
    rollback_count = fields.Integer()
    storage_bytes = fields.Integer()
    storage_growth_bytes = fields.Integer()
    cache_hit_ratio = fields.Float()
    wal_lag_bytes = fields.Integer()
    replication_lag_seconds = fields.Float()


class SaasCacheHourly(models.Model):
    _name = "saas.cache.hourly"
    _description = "Hourly SaaS cache aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, service_id"

    namespace = fields.Char(required=True, default="global", index=True)
    request_count = fields.Integer()
    hit_count = fields.Integer()
    miss_count = fields.Integer()
    timeout_count = fields.Integer()
    error_count = fields.Integer()
    hit_ratio = fields.Float()
    get_p95_seconds = fields.Float()
    set_p95_seconds = fields.Float()
    evicted_keys = fields.Integer()
    invalidation_lag_seconds = fields.Float()
    stale_served_count = fields.Integer()
    consistency_incident_count = fields.Integer()


class SaasQueueHourly(models.Model):
    _name = "saas.queue.hourly"
    _description = "Hourly SaaS queue aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, queue_name, job_type"

    queue_name = fields.Char(required=True, index=True)
    job_type = fields.Char(required=True, index=True)
    queue_depth = fields.Integer()
    oldest_age_seconds = fields.Float()
    enqueue_rate = fields.Float()
    processing_rate = fields.Float()
    drain_time_seconds = fields.Float()
    started_count = fields.Integer()
    completed_count = fields.Integer()
    failed_count = fields.Integer()
    retry_count = fields.Integer()
    duplicate_suppressed_count = fields.Integer()
    idempotency_conflict_count = fields.Integer()
    dlq_size = fields.Integer()
    worker_count = fields.Integer()


class SaasDependencyHourly(models.Model):
    _name = "saas.dependency.hourly"
    _description = "Hourly SaaS dependency aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, integration_id"

    integration_id = fields.Many2one(
        "saas.integration", required=True, ondelete="restrict", index=True
    )
    request_count = fields.Integer()
    success_count = fields.Integer()
    timeout_count = fields.Integer()
    retry_count = fields.Integer()
    p50_seconds = fields.Float()
    p95_seconds = fields.Float()
    p99_seconds = fields.Float()
    quota_utilization = fields.Float()
    cost = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency", required=True, default=lambda self: self.env.company.currency_id
    )


class SaasCostDaily(models.Model):
    _name = "saas.cost.daily"
    _description = "Daily SaaS cost aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, provider, category"

    provider = fields.Char(required=True, index=True)
    category = fields.Selection(
        [
            ("compute", "Compute"),
            ("database", "Database"),
            ("storage", "Storage"),
            ("network", "Network"),
            ("observability", "Observability"),
            ("email", "Email"),
            ("ai", "AI"),
            ("payment", "Payment"),
            ("other", "Other"),
        ],
        required=True,
        index=True,
    )
    amount = fields.Monetary(required=True, currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency", required=True, default=lambda self: self.env.company.currency_id
    )
    active_users = fields.Integer()
    active_tenants = fields.Integer()
    core_actions = fields.Integer()
    cost_per_active_user = fields.Monetary(currency_field="currency_id")
    cost_per_active_tenant = fields.Monetary(currency_field="currency_id")
    cost_per_core_action = fields.Monetary(currency_field="currency_id")


class SaasProductDaily(models.Model):
    _name = "saas.product.daily"
    _description = "Daily SaaS product aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, feature_id"

    feature_id = fields.Many2one("saas.feature", ondelete="restrict", index=True)
    active_users = fields.Integer()
    active_tenants = fields.Integer()
    core_actions = fields.Integer()
    signup_count = fields.Integer()
    activated_tenants = fields.Integer()
    eligible_new_tenants = fields.Integer()
    activation_rate = fields.Float()
    retention_rate = fields.Float()
    feature_adoption_rate = fields.Float()
    time_to_value_p50_seconds = fields.Float()
    time_to_value_p90_seconds = fields.Float()


class SaasSecurityDaily(models.Model):
    _name = "saas.security.daily"
    _description = "Daily SaaS security aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, service_id"

    login_attempts = fields.Integer()
    login_failures = fields.Integer()
    rate_limit_events = fields.Integer()
    suspicious_login_count = fields.Integer()
    cross_tenant_denied_count = fields.Integer()
    confirmed_cross_tenant_exposure_count = fields.Integer()
    privileged_action_count = fields.Integer()
    webhook_signature_failure_count = fields.Integer()
    critical_vulnerability_count = fields.Integer()
    high_vulnerability_count = fields.Integer()
    secret_finding_count = fields.Integer()
    audit_delivery_failure_count = fields.Integer()


class SaasCapacityDaily(models.Model):
    _name = "saas.capacity.daily"
    _description = "Daily SaaS capacity aggregate"
    _inherit = "saas.aggregate.mixin"
    _order = "period_start desc, service_id"

    peak_rps = fields.Float()
    tested_safe_rps = fields.Float()
    current_concurrent_users = fields.Integer()
    tested_concurrent_users = fields.Integer()
    capacity_headroom = fields.Float()
    db_connection_headroom = fields.Float()
    cpu_headroom = fields.Float()
    memory_headroom = fields.Float()
    storage_days_to_full = fields.Float()
    queue_drain_headroom = fields.Float()
    readiness = fields.Selection(
        [("ready", "Ready"), ("ready_with_risk", "Ready with risk"), ("not_ready", "Not ready"), ("test_stale", "Test stale")],
        index=True,
    )


class SaasDataQualityRun(models.Model):
    _name = "saas.data.quality.run"
    _inherit = "saas.operational.ingest.mixin"
    _description = "SaaS data quality run"
    _order = "started_at desc, id desc"

    name = fields.Char(required=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    started_at = fields.Datetime(required=True, index=True)
    finished_at = fields.Datetime()
    status = fields.Selection(QUALITY_SELECTION, required=True, default="valid", index=True)
    events_sent = fields.Integer()
    events_received = fields.Integer()
    events_processed = fields.Integer()
    events_rejected = fields.Integer()
    event_stream_complete = fields.Boolean(
        help="True only when the evidence covers the complete event stream window."
    )
    retry_adjustment_count = fields.Integer(
        help="Retry attempts already included in events sent and excluded from event loss."
    )
    duplicate_count = fields.Integer()
    schema_failure_count = fields.Integer()
    missing_field_count = fields.Integer()
    late_event_count = fields.Integer()
    unknown_tenant_count = fields.Integer()
    reconciliation_difference = fields.Float()
    oldest_unsynced_at = fields.Datetime()
    drilldown_url = fields.Char()

    _complete_event_count_fields = {
        "events_sent",
        "events_received",
        "events_processed",
        "events_rejected",
        "retry_adjustment_count",
        "duplicate_count",
        "schema_failure_count",
        "missing_field_count",
        "late_event_count",
        "unknown_tenant_count",
    }

    @api.model_create_multi
    def create(self, vals_list):
        for values in vals_list:
            if values.get("event_stream_complete") is True:
                missing = self._complete_event_count_fields - set(values)
                if missing:
                    raise ValidationError(
                        "Complete event-stream evidence requires all event counts: "
                        f"{', '.join(sorted(missing))}."
                    )
        return super().create(vals_list)

    def write(self, values):
        if values.get("event_stream_complete") is True:
            for record in self.filtered(lambda item: not item.event_stream_complete):
                missing = self._complete_event_count_fields - set(values)
                if missing:
                    raise ValidationError(
                        "Completing event-stream evidence requires all event counts: "
                        f"{', '.join(sorted(missing))}."
                    )
        return super().write(values)

    @api.constrains(
        "events_sent",
        "events_received",
        "events_processed",
        "events_rejected",
        "event_stream_complete",
        "retry_adjustment_count",
        "duplicate_count",
        "schema_failure_count",
        "missing_field_count",
        "late_event_count",
        "unknown_tenant_count",
        "started_at",
        "finished_at",
    )
    def _check_event_stream_contract(self):
        for record in self:
            values = [
                record.events_sent,
                record.events_received,
                record.events_processed,
                record.events_rejected,
                record.retry_adjustment_count,
                record.duplicate_count,
                record.schema_failure_count,
                record.missing_field_count,
                record.late_event_count,
                record.unknown_tenant_count,
            ]
            if any(value < 0 for value in values):
                raise ValidationError("Data quality counts cannot be negative.")
            if not record.event_stream_complete:
                continue
            if not record.finished_at or record.finished_at <= record.started_at:
                raise ValidationError(
                    "Complete event-stream evidence requires finished_at after started_at."
                )
            if record.events_processed + record.events_rejected > record.events_received:
                raise ValidationError(
                    "Processed and rejected events cannot exceed received events."
                )
            received_bounded_counts = [
                record.duplicate_count,
                record.schema_failure_count,
                record.missing_field_count,
                record.late_event_count,
                record.unknown_tenant_count,
            ]
            if any(value > record.events_received for value in received_bounded_counts):
                raise ValidationError(
                    "Event-quality issue counts cannot exceed received events."
                )
            maximum_retry_adjustment = max(
                record.events_sent - record.events_received, 0
            )
            if record.retry_adjustment_count > maximum_retry_adjustment:
                raise ValidationError(
                    "Retry adjustment cannot exceed the sent/received difference."
                )
