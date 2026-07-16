from odoo import fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests.common import TransactionCase


class TestSaasMetricContract(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.current_model = cls.env["saas.metric.current"]
        cls.metric = cls.env.ref("arcigy_saas_control_center.metric_request_success")
        cls.develop = cls.env.ref("arcigy_saas_control_center.environment_develop")
        cls.main = cls.env.ref("arcigy_saas_control_center.environment_main")
        cls.service = cls.env["saas.service"].create(
            {"name": "Core API", "code": "core-api"}
        )
        cls.region = cls.env["saas.region"].create(
            {"name": "EU Central", "code": "eu-central"}
        )
        cls.bot_group = cls.env.ref("arcigy_saas_control_center.group_saas_integration_bot")
        cls.executive_group = cls.env.ref("arcigy_saas_control_center.group_saas_executive")
        cls.finance_group = cls.env.ref("arcigy_saas_control_center.group_saas_finance")
        cls.engineering_group = cls.env.ref("arcigy_saas_control_center.group_saas_engineering")
        cls.security_group = cls.env.ref("arcigy_saas_control_center.group_saas_security")
        cls.bot = cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "SaaS metric bot",
                "login": "saas-metric-bot-test",
                "group_ids": [(6, 0, [cls.bot_group.id])],
            }
        )
        cls.executive = cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "SaaS executive",
                "login": "saas-executive-test",
                "group_ids": [(6, 0, [cls.executive_group.id])],
            }
        )
        cls.finance = cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "SaaS finance",
                "login": "saas-finance-test",
                "group_ids": [(6, 0, [cls.finance_group.id])],
            }
        )
        cls.engineering = cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "SaaS engineering",
                "login": "saas-engineering-test",
                "group_ids": [(6, 0, [cls.engineering_group.id])],
            }
        )
        cls.security = cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "SaaS security",
                "login": "saas-security-test",
                "group_ids": [(6, 0, [cls.security_group.id])],
            }
        )

    def _payload(self, environment, value):
        return {
            "environment": environment,
            "source_updated_at": "2026-07-16T10:00:00Z",
            "release_version": f"test-{environment}",
            "metrics": [
                {
                    "code": self.metric.code,
                    "value": value,
                    "numerator": value,
                    "denominator": 100,
                    "sample_count": 100,
                    "status": "healthy",
                    "measured_at": "2026-07-16T10:00:00Z",
                    "external_key": f"{environment}:http_request_success_rate:2026-07-16T10:00:00Z",
                    "period_start": "2026-07-16T09:55:00Z",
                    "period_end": "2026-07-16T10:00:00Z",
                    "granularity": "5m",
                }
            ],
        }

    def test_develop_and_main_are_distinct_and_idempotent(self):
        as_bot = self.current_model.with_user(self.bot)
        develop_result = as_bot.ingest_metric_batch(self._payload("develop", 99.1))
        main_result = as_bot.ingest_metric_batch(self._payload("main", 99.9))
        self.assertEqual(develop_result["created"], 1)
        self.assertEqual(main_result["created"], 1)
        self.assertEqual(develop_result["history_created"], 1)
        self.assertEqual(main_result["history_created"], 1)

        records = self.current_model.search([("metric_id", "=", self.metric.id)])
        self.assertEqual(len(records), 2)
        by_environment = {record.environment_id.code: record.current_value for record in records}
        self.assertEqual(by_environment, {"develop": 99.1, "main": 99.9})

        update_result = as_bot.ingest_metric_batch(self._payload("develop", 98.7))
        self.assertEqual(update_result["created"], 0)
        self.assertEqual(update_result["updated"], 1)
        self.assertEqual(update_result["history_updated"], 1)
        records = self.current_model.search([("metric_id", "=", self.metric.id)])
        by_environment = {record.environment_id.code: record.current_value for record in records}
        self.assertEqual(by_environment, {"develop": 98.7, "main": 99.9})
        history = self.env["saas.metric.timeseries"].search([("metric_id", "=", self.metric.id)])
        self.assertEqual(len(history), 2)

    def test_dashboard_returns_paired_columns(self):
        as_bot = self.current_model.with_user(self.bot)
        as_bot.ingest_metric_batch(self._payload("develop", 99.2))
        as_bot.ingest_metric_batch(self._payload("main", 99.8))

        payload = self.current_model.with_user(self.executive).dashboard_payload("live_operations")
        row = next(
            row
            for section in payload["sections"]
            for row in section["rows"]
            if row["code"] == self.metric.code
        )
        self.assertEqual(row["develop"]["value"], 99.2)
        self.assertEqual(row["main"]["value"], 99.8)

    def test_every_dashboard_row_has_develop_and_main_columns(self):
        dashboards = self.env["saas.dashboard"].search([("active", "=", True)])
        self.assertEqual(len(dashboards), 24)
        for dashboard in dashboards:
            payload = self.current_model.with_user(self.executive).dashboard_payload(
                dashboard.code
            )
            self.assertEqual(len(payload["sections"]), 1)
            rows = payload["sections"][0]["rows"]
            self.assertTrue(rows, dashboard.code)
            for row in rows:
                self.assertIn("develop", row, f"{dashboard.code}/{row['code']}")
                self.assertIn("main", row, f"{dashboard.code}/{row['code']}")

    def test_ingest_rejects_invalid_environment(self):
        with self.assertRaises(ValidationError):
            self.current_model.with_user(self.bot).ingest_metric_batch(self._payload("production", 99))

    def test_ingest_rejects_non_bot(self):
        with self.assertRaises(AccessError):
            self.current_model.with_user(self.executive).ingest_metric_batch(
                self._payload("develop", 99)
            )

    def test_role_access_blocks_finance_security_and_engineering_cost_cross_read(self):
        with self.assertRaises(AccessError):
            self.env["saas.security.daily"].with_user(self.finance).search([])
        with self.assertRaises(AccessError):
            self.env["saas.cost.daily"].with_user(self.engineering).search([])
        self.assertEqual(
            self.env["saas.security.daily"].with_user(self.security).search_count([]), 0
        )

    def test_retention_is_preview_only_without_destructive_approval(self):
        preview = self.env["saas.metric.timeseries"].retention_preview()
        self.assertFalse(preview["destructiveActionEnabled"])
        self.assertIn("candidateCount", preview)

    def test_every_seeded_metric_has_complete_definition_contract(self):
        definitions = self.env["saas.metric.definition"].search([])
        self.assertGreaterEqual(len(definitions), 191)
        for definition in definitions:
            self.assertTrue(definition.code)
            self.assertTrue(definition.name)
            self.assertTrue(definition.description)
            self.assertTrue(definition.formula)
            self.assertTrue(definition.unit)
            self.assertTrue(definition.source)
            self.assertTrue(definition.dimensions)
            self.assertTrue(definition.owner_id)
            self.assertTrue(definition.numerator_name)
            self.assertTrue(definition.denominator_name)
            self.assertTrue(definition.runbook)
            self.assertGreater(definition.freshness_seconds, 0)
            self.assertGreater(definition.retention_days, 0)

    def test_required_dashboard_catalogue_and_p0_metrics_are_seeded(self):
        expected_dashboards = {
            "founder", "live_operations", "slo_incidents", "api_backend",
            "frontend_experience", "database", "cache_cdn_search", "queues_jobs",
            "dependencies", "infrastructure_capacity", "thousand_users",
            "product_funnel", "engagement_retention", "tenant_health",
            "revenue_billing", "marketing_sales", "support_voice", "finops",
            "security", "privacy_compliance", "engineering_releases",
            "data_quality_sync", "backup_dr", "ai_llm",
        }
        actual_dashboards = set(self.env["saas.dashboard"].search([]).mapped("code"))
        self.assertEqual(actual_dashboards, expected_dashboards)

        expected_p0_metrics = {
            "http_request_count", "http_5xx_rate", "http_latency_p95_seconds",
            "http_latency_p99_seconds", "core_action_success_rate",
            "http_requests_in_flight", "db_operation_p95_seconds",
            "db_pool_utilization", "db_pool_wait_p95_seconds",
            "db_pool_timeout_count", "db_storage_bytes", "queue_depth",
            "queue_oldest_age_seconds", "job_failure_rate", "cache_hit_ratio",
            "cache_timeout_rate", "dependency_success_rate",
            "dependency_p95_seconds", "cpu_saturation", "memory_saturation",
            "healthy_app_instances", "active_tenants", "activation_rate", "mrr",
            "payment_success_rate", "total_operational_cost",
            "cost_per_active_tenant", "slo_availability",
            "error_budget_remaining", "backup_age_seconds",
            "restore_test_age_seconds", "odoo_sync_freshness_seconds",
        }
        actual_metrics = set(
            self.env["saas.metric.definition"].search([]).mapped("code")
        )
        self.assertFalse(expected_p0_metrics - actual_metrics)

    def test_ingest_rejects_unsafe_drilldown_url(self):
        payload = self._payload("develop", 99)
        payload["metrics"][0]["drilldown_url"] = "javascript:alert(1)"
        with self.assertRaises(ValidationError):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

    def test_dimension_filters_keep_develop_and_main_paired(self):
        as_bot = self.current_model.with_user(self.bot)
        for environment, value in (("develop", 99.2), ("main", 99.8)):
            payload = self._payload(environment, value)
            payload["metrics"][0].update(
                {
                    "scope_key": "service:core-api",
                    "service_code": self.service.code,
                    "region_code": self.region.code,
                }
            )
            as_bot.ingest_metric_batch(payload)

        result = self.current_model.with_user(self.executive).dashboard_payload(
            "live_operations",
            "service:core-api",
            {"service_id": self.service.id, "region_id": self.region.id},
        )
        row = next(
            row
            for section in result["sections"]
            for row in section["rows"]
            if row["code"] == self.metric.code
        )
        self.assertEqual(row["develop"]["value"], 99.2)
        self.assertEqual(row["main"]["value"], 99.8)
        self.assertEqual(result["appliedFilters"]["service_id"], self.service.id)

    def test_ingest_rejects_unknown_dimension(self):
        payload = self._payload("develop", 99)
        payload["metrics"][0]["service_code"] = "does-not-exist"
        with self.assertRaises(ValidationError):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

    def test_dashboard_rejects_unbounded_filter(self):
        with self.assertRaises(ValidationError):
            self.current_model.with_user(self.executive).dashboard_payload(
                "live_operations", "global", {"service_id": "not-an-id"}
            )

    def test_critical_metric_opens_one_alert_and_healthy_recovery_resolves_it(self):
        as_bot = self.current_model.with_user(self.bot)
        critical = self._payload("develop", 80)
        critical["metrics"][0]["status"] = "critical"
        first = as_bot.ingest_metric_batch(critical)
        second = as_bot.ingest_metric_batch(critical)
        self.assertEqual(first["alerts_opened"], 1)
        self.assertEqual(second["alerts_opened"], 0)

        alert = self.env["saas.alert"].sudo().search(
            [
                ("metric_id", "=", self.metric.id),
                ("environment_id", "=", self.develop.id),
                ("scope_key", "=", "global"),
            ]
        )
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.severity, self.metric.critical_alert_severity)
        self.assertTrue(alert.incident_id)

        healthy = self._payload("develop", 99.9)
        healthy["source_updated_at"] = "2026-07-16T10:05:00Z"
        healthy["metrics"][0].update(
            {
                "status": "healthy",
                "measured_at": "2026-07-16T10:05:00Z",
                "external_key": "develop:http_request_success_rate:2026-07-16T10:05:00Z",
                "period_start": "2026-07-16T10:00:00Z",
                "period_end": "2026-07-16T10:05:00Z",
            }
        )
        recovered = as_bot.ingest_metric_batch(healthy)
        alert.invalidate_recordset()
        self.assertEqual(recovered["alerts_resolved"], 1)
        self.assertEqual(alert.status, "resolved")

    def test_aggregate_ingest_is_idempotent_and_direct_bot_create_is_denied(self):
        endpoint_model = self.env["saas.endpoint.hourly"]
        item = {
            "external_key": "develop:endpoint:core-api:2026-07-16T10:00:00Z",
            "period_start": "2026-07-16T09:00:00Z",
            "period_end": "2026-07-16T10:00:00Z",
            "service_code": self.service.code,
            "method": "POST",
            "endpoint_group": "/api/projects/:id",
            "slo_class": "critical",
            "request_count": 10,
            "success_count": 9,
            "error_count": 1,
            "p95_seconds": 0.4,
            "status": "warning",
        }
        payload = {
            "environment": "develop",
            "source_updated_at": "2026-07-16T10:00:00Z",
            "items": [item],
        }
        first = endpoint_model.with_user(self.bot).ingest_aggregate_batch(payload)
        item["request_count"] = 11
        second = endpoint_model.with_user(self.bot).ingest_aggregate_batch(payload)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["updated"], 1)
        aggregate = endpoint_model.search([("external_key", "=", item["external_key"])])
        self.assertEqual(len(aggregate), 1)
        self.assertEqual(aggregate.request_count, 11)
        with self.assertRaises(AccessError):
            endpoint_model.with_user(self.bot).create(
                {
                    "environment_id": self.develop.id,
                    "period_start": fields.Datetime.now(),
                    "period_end": fields.Datetime.now(),
                    "source_updated_at": fields.Datetime.now(),
                    "external_key": "develop:direct-create-denied",
                    "method": "GET",
                    "endpoint_group": "/forbidden",
                }
            )

    def test_failed_backup_ingest_is_idempotent_and_opens_p1_incident(self):
        backup_model = self.env["saas.backup.run"]
        payload = {
            "environment": "main",
            "source_updated_at": "2026-07-16T11:00:00Z",
            "items": [
                {
                    "external_key": "main:backup:2026-07-16T11:00:00Z",
                    "name": "Main off-host backup",
                    "started_at": "2026-07-16T10:55:00Z",
                    "finished_at": "2026-07-16T11:00:00Z",
                    "status": "failed",
                    "backup_type": "full",
                    "size_bytes": 0,
                    "encrypted": True,
                    "off_host": True,
                    "drilldown_url": "https://monitoring.example.com/backups/main",
                }
            ],
        }
        first = backup_model.with_user(self.bot).ingest_operational_batch(payload)
        second = backup_model.with_user(self.bot).ingest_operational_batch(payload)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["updated"], 1)
        backup = backup_model.search(
            [("external_key", "=", "main:backup:2026-07-16T11:00:00Z")]
        )
        self.assertEqual(len(backup), 1)
        alert = self.env["saas.alert"].sudo().search(
            [
                ("environment_id", "=", self.main.id),
                ("scope_key", "=", "saas.backup.run"),
                ("status", "!=", "resolved"),
            ]
        )
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.severity, "p1")
        self.assertTrue(alert.incident_id)

    def test_invalid_data_quality_run_opens_p1_alert(self):
        payload = {
            "environment": "develop",
            "source_updated_at": "2026-07-16T12:00:00Z",
            "items": [
                {
                    "external_key": "develop:data-quality:2026-07-16T12:00:00Z",
                    "name": "Metric reconciliation",
                    "started_at": "2026-07-16T11:55:00Z",
                    "finished_at": "2026-07-16T12:00:00Z",
                    "status": "invalid",
                    "events_sent": 100,
                    "events_received": 99,
                    "events_processed": 98,
                    "events_rejected": 1,
                    "reconciliation_difference": 2,
                }
            ],
        }
        result = self.env["saas.data.quality.run"].with_user(
            self.bot
        ).ingest_operational_batch(payload)
        self.assertEqual(result["created"], 1)
        alert = self.env["saas.alert"].sudo().search(
            [
                ("environment_id", "=", self.develop.id),
                ("scope_key", "=", "saas.data.quality.run"),
                ("status", "!=", "resolved"),
            ]
        )
        self.assertEqual(len(alert), 1)
        self.assertEqual(alert.severity, "p1")

    def test_delayed_delivery_does_not_replace_newer_current_value(self):
        as_bot = self.current_model.with_user(self.bot)
        newer = self._payload("develop", 99.8)
        newer["source_updated_at"] = "2026-07-16T10:05:00Z"
        newer["metrics"][0].update(
            {
                "measured_at": "2026-07-16T10:05:00Z",
                "external_key": "develop:http_request_success_rate:2026-07-16T10:05:00Z",
                "period_start": "2026-07-16T10:00:00Z",
                "period_end": "2026-07-16T10:05:00Z",
            }
        )
        as_bot.ingest_metric_batch(newer)

        delayed = self._payload("develop", 97.0)
        result = as_bot.ingest_metric_batch(delayed)

        current = self.current_model.search(
            [
                ("metric_id", "=", self.metric.id),
                ("environment_id", "=", self.develop.id),
                ("scope_key", "=", "global"),
            ],
            limit=1,
        )
        self.assertEqual(result["stale_skipped"], 1)
        self.assertEqual(current.current_value, 99.8)
        self.assertEqual(
            self.env["saas.metric.timeseries"].search_count(
                [
                    ("metric_id", "=", self.metric.id),
                    ("environment_id", "=", self.develop.id),
                ]
            ),
            2,
        )

    def test_p1_alert_creates_incident_and_requires_postmortem_closure(self):
        alert = self.env["saas.alert"].create(
            {
                "name": "Core API unavailable",
                "severity": "p1",
                "status": "open",
                "metric_id": self.metric.id,
                "environment_id": self.main.id,
                "detected_at": fields.Datetime.now(),
                "owner_id": self.env.user.id,
                "runbook_url": "https://runbooks.example.com/core-api",
                "drilldown_url": "https://metrics.example.com/core-api",
                "recovery_condition": "Three healthy readiness probes",
                "deduplication_key": "test-main-core-api-unavailable",
            }
        )
        self.assertTrue(alert.incident_id)
        self.assertEqual(alert.incident_id.alert_id, alert)
        self.assertTrue(
            self.env["mail.activity"].search_count(
                [("res_model", "=", "saas.incident"), ("res_id", "=", alert.incident_id.id)]
            )
        )
        with self.assertRaises(ValidationError):
            with self.env.cr.savepoint():
                alert.incident_id.write({"status": "resolved"})
        alert.incident_id.invalidate_recordset()
        self.env["saas.postmortem.action"].create(
            {
                "name": "Add regression for readiness outage",
                "incident_id": alert.incident_id.id,
                "owner_id": self.env.user.id,
                "deadline": fields.Date.today(),
            }
        )
        alert.incident_id.write({"root_cause": "Test dependency outage", "status": "resolved"})
        self.assertEqual(alert.incident_id.status, "resolved")
