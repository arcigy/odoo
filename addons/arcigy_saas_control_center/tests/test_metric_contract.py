from datetime import timedelta

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
        cls.customer_success_group = cls.env.ref(
            "arcigy_saas_control_center.group_saas_customer_success"
        )
        cls.support_group = cls.env.ref("arcigy_saas_control_center.group_saas_support")
        cls.engineering_group = cls.env.ref("arcigy_saas_control_center.group_saas_engineering")
        cls.security_group = cls.env.ref("arcigy_saas_control_center.group_saas_security")
        cls.administrator_group = cls.env.ref(
            "arcigy_saas_control_center.group_saas_administrator"
        )
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
        cls.customer_success = cls.env["res.users"].with_context(
            no_reset_password=True
        ).create(
            {
                "name": "SaaS customer success",
                "login": "saas-customer-success-test",
                "group_ids": [(6, 0, [cls.customer_success_group.id])],
            }
        )
        cls.support = cls.env["res.users"].with_context(no_reset_password=True).create(
            {
                "name": "SaaS support",
                "login": "saas-support-test",
                "group_ids": [(6, 0, [cls.support_group.id])],
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
        cls.administrator = cls.env["res.users"].with_context(
            no_reset_password=True
        ).create(
            {
                "name": "SaaS administrator",
                "login": "saas-administrator-test",
                "group_ids": [(6, 0, [cls.administrator_group.id])],
            }
        )
        cls.plan = cls.env["saas.plan"].create(
            {"name": "Role filter plan", "code": "role-filter-plan"}
        )
        cls.tenant = cls.env["saas.tenant"].create(
            {
                "name": "Role filter tenant",
                "external_id": "role-filter-tenant",
                "plan_id": cls.plan.id,
            }
        )
        cls.feature = cls.env["saas.feature"].create(
            {"name": "Role filter feature", "code": "role-filter-feature"}
        )
        cls.integration = cls.env["saas.integration"].create(
            {"name": "Role filter integration", "code": "role-filter-integration"}
        )
        cls.release = cls.env["saas.release"].create(
            {
                "version": "role-filter-release",
                "environment_id": cls.develop.id,
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

    def test_dashboard_freshness_summary_is_paired_and_honest_about_missing_data(self):
        payload = self.current_model.with_user(self.executive).dashboard_payload(
            "live_operations"
        )
        self.assertEqual(set(payload["freshnessSummary"]), {"develop", "main"})
        expected = len(payload["sections"][0]["rows"])
        for environment in ("develop", "main"):
            summary = payload["freshnessSummary"][environment]
            self.assertEqual(summary["status"], "missing")
            self.assertEqual(summary["expectedMetricCount"], expected)
            self.assertLess(summary["presentMetricCount"], expected)

    def test_freshness_uses_one_and_a_half_times_cadence(self):
        now = fields.Datetime.now()
        record = self.current_model.sudo().create(
            {
                "metric_id": self.metric.id,
                "environment_id": self.develop.id,
                "scope_key": "freshness:test",
                "status": "healthy",
                "current_value": 1,
                "measured_at": now - timedelta(seconds=72),
                "fresh_until": now - timedelta(seconds=12),
                "source_updated_at": now,
            }
        )
        self.assertEqual(record.freshness_status, "fresh")

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

    def test_all_human_roles_can_load_every_global_dashboard_filter(self):
        expected_ids = {
            "services": self.service.id,
            "regions": self.region.id,
            "releases": self.release.id,
            "tenants": self.tenant.id,
            "plans": self.plan.id,
            "features": self.feature.id,
            "integrations": self.integration.id,
        }
        users = [
            self.executive,
            self.finance,
            self.customer_success,
            self.support,
            self.engineering,
            self.security,
            self.administrator,
        ]

        for user in users:
            options = self.current_model.with_user(user).dashboard_filter_options()
            self.assertIn("founder", {item["code"] for item in options["dashboards"]})
            for option_name, expected_id in expected_ids.items():
                self.assertIn(
                    expected_id,
                    {item["id"] for item in options[option_name]},
                    f"{user.login} cannot load the {option_name} dashboard filter",
                )
            self.assertTrue(options["countries"], user.login)
            self.assertTrue(options["currencies"], user.login)

    def test_role_access_preserves_sensitive_aggregate_boundaries(self):
        with self.assertRaises(AccessError):
            self.env["saas.security.daily"].with_user(self.finance).search([])
        with self.assertRaises(AccessError):
            self.env["saas.cost.daily"].with_user(self.engineering).search([])
        with self.assertRaises(AccessError):
            self.env["saas.cost.daily"].with_user(self.security).search([])
        for user in (self.customer_success, self.support):
            with self.assertRaises(AccessError):
                self.env["saas.cost.daily"].with_user(user).search([])
            with self.assertRaises(AccessError):
                self.env["saas.security.daily"].with_user(user).search([])

        self.assertEqual(
            self.env["saas.cost.daily"].with_user(self.finance).search_count([]), 0
        )
        self.assertEqual(
            self.env["saas.endpoint.hourly"].with_user(self.engineering).search_count([]),
            0,
        )
        self.assertEqual(
            self.env["saas.product.daily"].with_user(self.customer_success).search_count([]),
            0,
        )
        self.assertEqual(
            self.env["saas.tenant.daily"].with_user(self.support).search_count([]), 0
        )
        self.assertEqual(
            self.env["saas.security.daily"].with_user(self.security).search_count([]), 0
        )
        self.assertEqual(
            self.env["saas.cost.daily"].with_user(self.executive).search_count([]), 0
        )
        self.assertEqual(
            self.env["saas.security.daily"].with_user(self.executive).search_count([]), 0
        )

    def test_integration_bot_cannot_mutate_business_or_operational_records(self):
        protected_models = [
            "res.partner",
            "crm.lead",
            "saas.incident",
            "saas.metric.current",
            "saas.metric.timeseries",
            "saas.backup.run",
            "saas.restore.test",
            "saas.dr.drill",
            "saas.load.test",
            "saas.sync.run",
        ]
        if "account.move" in self.env:
            protected_models.append("account.move")

        for model_name in protected_models:
            model = self.env[model_name].with_user(self.bot)
            for operation in ("create", "write", "unlink"):
                with self.assertRaises(
                    AccessError,
                    msg=f"Integration bot unexpectedly has {operation} access to {model_name}",
                ):
                    model.check_access(operation)

    def test_retention_is_preview_only_without_destructive_approval(self):
        preview = self.env["saas.metric.timeseries"].retention_preview()
        self.assertFalse(preview["destructiveActionEnabled"])
        self.assertIn("candidateCount", preview)

    def test_every_seeded_metric_has_complete_definition_contract(self):
        definitions = self.env["saas.metric.definition"].search([])
        self.assertEqual(len(definitions), 376)
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
            "events_sent_count", "events_received_count", "events_processed_count",
            "events_rejected_count", "event_loss_estimate", "event_duplicate_rate",
            "schema_validation_failure_count", "missing_required_field_count",
            "late_event_rate", "unknown_tenant_mapping_count",
            "event_clock_skew_seconds", "event_processing_lag_p95_seconds",
            "dead_letter_event_count", "metric_freshness_rate",
            "metric_completeness_rate", "metric_uniqueness_rate",
            "metric_validity_rate", "metric_consistency_rate",
            "data_quality_reconciliation_difference", "data_quality_outlier_count",
            "unexpected_zero_value_count", "unexpected_volume_spike_count",
            "numerator_denominator_violation_count", "negative_value_violation_count",
            "missing_dimension_count",
            "backup_duration_seconds", "backup_size_bytes",
            "backup_failure_count_24h", "backup_snapshot_count",
            "backup_pitr_enabled_status", "backup_pitr_window_seconds",
            "backup_wal_archive_health_status", "backup_secondary_copy_status",
            "backup_encryption_status", "backup_storage_cost_monthly_eur",
            "restore_duration_seconds", "restore_checksum_status",
            "restore_missing_record_count", "restore_application_smoke_status",
            "restore_tenant_isolation_status", "restore_next_test_overdue_seconds",
            "dr_drill_age_seconds", "dr_drill_success_status",
            "dr_failover_duration_seconds", "dr_failback_duration_seconds",
            "dr_dns_propagation_duration_seconds", "dr_unavailable_dependency_count",
            "dr_runbook_accuracy_rate", "dr_open_remediation_action_count",
            "dr_next_drill_overdue_seconds",
            "odoo_sync_attempt_age_seconds", "odoo_sync_duration_seconds",
            "odoo_sync_records_read_count", "odoo_sync_records_created_count",
            "odoo_sync_records_updated_count", "odoo_sync_records_skipped_count",
            "odoo_sync_records_rejected_count", "odoo_sync_duplicate_upsert_count",
            "odoo_sync_api_error_count", "odoo_sync_authentication_error_count",
            "odoo_sync_permission_error_count", "odoo_sync_rate_limit_error_count",
            "odoo_sync_retry_count", "odoo_sync_backlog_count",
            "odoo_sync_oldest_unsynced_age_seconds",
        }
        actual_metrics = set(
            self.env["saas.metric.definition"].search([]).mapped("code")
        )
        self.assertFalse(expected_p0_metrics - actual_metrics)

    def test_complete_engineering_quality_metric_contract_is_seeded(self):
        expected_metrics = {
            "open_pr_count", "pr_cycle_time_p50_seconds",
            "pr_time_to_first_review_p50_seconds", "pr_approval_to_merge_p50_seconds",
            "pr_average_diff_lines", "pr_average_files_changed",
            "pr_average_modules_changed", "pr_average_comment_count",
            "pr_requested_changes_count", "stale_pr_count", "reopened_pr_count",
            "branch_age_average_seconds", "branch_oldest_age_seconds",
            "stale_branch_count", "merge_conflict_count", "direct_main_push_count",
            "branch_protection_bypass_count", "failed_required_check_count",
            "emergency_merge_count", "unreviewed_production_change_count",
            "build_count", "build_duration_p95_seconds", "build_queue_p95_seconds",
            "flaky_job_rate", "unit_test_pass_rate", "integration_test_pass_rate",
            "e2e_test_pass_rate", "performance_test_pass_rate",
            "security_test_pass_rate", "flaky_test_count", "flaky_test_age_seconds",
            "ignored_test_count", "skipped_test_count", "test_duration_p95_seconds",
            "critical_path_coverage_rate", "mutation_score_rate", "bug_reopen_rate",
            "active_feature_flag_count", "feature_flag_without_owner_count",
            "feature_flag_without_expiry_count", "feature_flag_age_p95_seconds",
            "permanent_feature_flag_count", "feature_flag_untested_on_count",
            "feature_flag_untested_off_count", "feature_flag_rollback_count",
            "module_boundary_violation_count", "cyclic_dependency_count",
            "duplicate_code_rate", "complexity_regression_rate", "oversized_file_count",
            "oversized_function_count", "dead_code_finding_count",
            "unused_dependency_count", "todo_count", "tech_debt_item_count",
            "tech_debt_age_average_seconds",
            "sensitive_code_without_codeowner_review_count",
        }
        actual_metrics = set(
            self.env["saas.metric.definition"].search([]).mapped("code")
        )
        self.assertEqual(len(expected_metrics), 57)
        self.assertFalse(expected_metrics - actual_metrics)

    def test_complete_ai_llm_metric_contract_is_seeded(self):
        expected_metrics = {
            "ai_request_count", "ai_successful_request_count", "ai_failed_request_count",
            "ai_request_success_rate", "ai_input_token_count", "ai_output_token_count",
            "ai_cached_token_count", "ai_cost", "ai_cost_per_request",
            "ai_cost_per_tenant", "ai_cost_per_successful_outcome",
            "ai_latency_p95_seconds", "ai_time_to_first_token_p95_seconds",
            "ai_model_processing_p95_seconds", "ai_tool_call_duration_p95_seconds",
            "ai_retry_rate", "ai_fallback_model_use_rate", "ai_provider_timeout_count",
            "ai_provider_rate_limit_count", "ai_task_completion_rate",
            "ai_user_acceptance_rate", "ai_regenerate_rate", "ai_correction_rate",
            "ai_thumbs_up_count", "ai_thumbs_down_count", "ai_human_escalation_rate",
            "ai_structured_output_validation_failure_rate", "ai_tool_call_success_rate",
            "ai_citation_grounding_coverage_rate", "ai_detected_hallucination_rate",
            "ai_moderation_block_count", "ai_prompt_injection_detection_count",
            "ai_jailbreak_attempt_count", "ai_sensitive_data_detection_count",
            "ai_output_policy_violation_count", "ai_tool_permission_denial_count",
            "ai_tenant_quota_exceeded_count",
        }
        actual_metrics = set(
            self.env["saas.metric.definition"].search([]).mapped("code")
        )
        self.assertEqual(len(expected_metrics), 37)
        self.assertFalse(expected_metrics - actual_metrics)

    def test_ingest_rejects_unsafe_drilldown_url(self):
        payload = self._payload("develop", 99)
        payload["metrics"][0]["drilldown_url"] = "javascript:alert(1)"
        with self.assertRaises(ValidationError):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

    def test_ingest_rejects_unknown_or_raw_payload_fields_before_writing(self):
        payload = self._payload("develop", 99)
        payload["raw_logs"] = [{"message": "must remain outside Odoo"}]
        before = self.env["saas.sync.run"].search_count([])
        with self.assertRaisesRegex(ValidationError, "Unsupported metric payload fields: raw_logs"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)
        self.assertEqual(self.env["saas.sync.run"].search_count([]), before)

    def test_ingest_requires_utc_watermark_and_complete_history_window(self):
        payload = self._payload("develop", 99)
        payload.pop("source_updated_at")
        with self.assertRaisesRegex(ValidationError, "source_updated_at is required"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

        payload = self._payload("develop", 99)
        payload["source_updated_at"] = "2026-07-16T10:00:00"
        with self.assertRaisesRegex(ValidationError, "source_updated_at must explicitly use UTC"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

        payload = self._payload("develop", 99)
        payload["metrics"][0].pop("period_end")
        with self.assertRaisesRegex(ValidationError, "Historical metric fields must be supplied together"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

    def test_ingest_rejects_future_or_non_integer_measurement_metadata(self):
        payload = self._payload("develop", 99)
        payload["metrics"][0]["measured_at"] = "2026-07-16T10:06:00Z"
        with self.assertRaisesRegex(ValidationError, "newer than the source watermark"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

        payload = self._payload("develop", 99)
        payload["metrics"][0]["freshness_seconds"] = "300"
        with self.assertRaisesRegex(ValidationError, "freshness_seconds must be an integer"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

        payload = self._payload("develop", 99)
        payload["metrics"][0]["sample_count"] = 1.5
        with self.assertRaisesRegex(ValidationError, "sample_count must be an integer"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

        payload = self._payload("develop", 99)
        payload["metrics"][0]["raw_request"] = {"authorization": "forbidden"}
        with self.assertRaisesRegex(ValidationError, "Unsupported metric item fields: raw_request"):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)
        self.assertEqual(self.env["saas.sync.run"].search_count([]), before)

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
            "global",
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
        self.assertEqual(result["scopeKey"], "filtered")

    def test_ai_model_filter_keeps_develop_and_main_paired(self):
        as_bot = self.current_model.with_user(self.bot)
        for environment, value in (("develop", 98.1), ("main", 99.1)):
            payload = self._payload(environment, value)
            payload["metrics"][0].update(
                {
                    "scope_key": "model:model-v1",
                    "model_code": "model-v1",
                    "external_key": f"{environment}:model-v1:2026-07-16T10:00:00Z",
                }
            )
            as_bot.ingest_metric_batch(payload)

        result = self.current_model.with_user(self.executive).dashboard_payload(
            "live_operations", "global", {"model_code": "model-v1"}
        )
        row = next(
            row
            for section in result["sections"]
            for row in section["rows"]
            if row["code"] == self.metric.code
        )
        self.assertEqual(row["develop"]["value"], 98.1)
        self.assertEqual(row["main"]["value"], 99.1)
        self.assertEqual(result["scopeKey"], "filtered")
        self.assertEqual(result["appliedFilters"]["model_code"], "model-v1")
        self.assertEqual(
            set(
                self.env["saas.metric.timeseries"]
                .search([("model_code", "=", "model-v1")])
                .mapped("environment_id.code")
            ),
            {"develop", "main"},
        )

    def test_ingest_rejects_unknown_dimension(self):
        payload = self._payload("develop", 99)
        payload["metrics"][0]["service_code"] = "does-not-exist"
        with self.assertRaises(ValidationError):
            self.current_model.with_user(self.bot).ingest_metric_batch(payload)

    def test_ingest_resolves_inactive_currency_without_activating_it(self):
        currency = self.env["res.currency"].with_context(active_test=False).create(
            {
                "name": "ZZZ",
                "symbol": "Z",
                "rounding": 0.01,
                "decimal_places": 2,
                "active": False,
            }
        )
        payload = self._payload("develop", 99)
        payload["metrics"][0]["currency_code"] = currency.name

        result = self.current_model.with_user(self.bot).ingest_metric_batch(payload)

        self.assertEqual(result["created"], 1)
        current = self.current_model.search(
            [
                ("metric_id", "=", self.metric.id),
                ("environment_id", "=", self.develop.id),
                ("currency_id", "=", currency.id),
            ]
        )
        self.assertEqual(len(current), 1)
        self.assertFalse(currency.active)

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

    def test_actionable_alert_form_and_state_actions_cover_required_contract(self):
        required_fields = {
            "severity", "metric_id", "scope_key", "current_value", "threshold",
            "detected_at", "status", "owner_id", "service_id", "tenant_id",
            "release_id", "runbook_url", "drilldown_url", "incident_id",
            "acknowledged_at", "resolved_at", "recovery_condition",
        }
        arch = self.env.ref(
            "arcigy_saas_control_center.view_saas_alert_form"
        ).arch_db
        for field_name in required_fields:
            self.assertIn(f'name="{field_name}"', arch)
        self.assertIn('name="action_acknowledge"', arch)
        self.assertIn('name="action_resolve"', arch)

        payload = self._payload("develop", 80)
        payload["metrics"][0]["status"] = "critical"
        self.current_model.with_user(self.bot).ingest_metric_batch(payload)
        alert = self.env["saas.alert"].sudo().search(
            [
                ("metric_id", "=", self.metric.id),
                ("environment_id", "=", self.develop.id),
                ("scope_key", "=", "global"),
            ],
            limit=1,
        )
        self.assertTrue(alert)
        alert.action_acknowledge()
        acknowledged_at = alert.acknowledged_at
        self.assertEqual(alert.status, "acknowledged")
        self.assertTrue(acknowledged_at)
        self.assertEqual(alert.incident_id.status, "acknowledged")
        self.assertEqual(alert.incident_id.acknowledged_at, acknowledged_at)
        alert.action_acknowledge()
        self.assertEqual(alert.acknowledged_at, acknowledged_at)

        alert.action_resolve()
        self.assertEqual(alert.status, "resolved")
        self.assertTrue(alert.resolved_at)
        self.assertEqual(alert.incident_id.status, "mitigated")
        self.assertEqual(alert.incident_id.mitigated_at, alert.resolved_at)
        resolved_at = alert.resolved_at
        alert.action_resolve()
        self.assertEqual(alert.resolved_at, resolved_at)

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

    def test_failed_dr_ingest_is_idempotent_and_success_resolves_alert(self):
        dr_model = self.env["saas.dr.drill"]
        item = {
            "external_key": "develop:dr-drill:2026-07-16T12:00:00Z",
            "name": "Develop disaster recovery drill",
            "started_at": "2026-07-16T11:00:00Z",
            "finished_at": "2026-07-16T12:00:00Z",
            "status": "failed",
            "dr_contract_complete": True,
            "failover_duration_seconds": 900,
            "failback_duration_seconds": 1800,
            "dns_propagation_duration_seconds": 300,
            "unavailable_dependency_count": 1,
            "runbook_accuracy_rate": 90,
            "open_remediation_action_count": 2,
            "owner_team": "Engineering",
            "next_drill_at": "2026-08-16T11:00:00Z",
            "evidence_url": "https://evidence.example.test/drills/develop",
        }
        payload = {
            "environment": "develop",
            "source_updated_at": "2026-07-16T12:05:00Z",
            "items": [item],
        }
        first = dr_model.with_user(self.bot).ingest_operational_batch(payload)
        second = dr_model.with_user(self.bot).ingest_operational_batch(payload)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["updated"], 1)
        self.assertEqual(
            dr_model.search_count(
                [("external_key", "=", "develop:dr-drill:2026-07-16T12:00:00Z")]
            ),
            1,
        )
        alert = self.env["saas.alert"].sudo().search(
            [
                ("environment_id", "=", self.develop.id),
                ("scope_key", "=", "saas.dr.drill"),
            ],
            limit=1,
        )
        self.assertEqual(alert.severity, "p1")
        self.assertTrue(alert.incident_id)

        item.update(
            {
                "status": "success",
                "unavailable_dependency_count": 0,
                "runbook_accuracy_rate": 100,
                "open_remediation_action_count": 0,
            }
        )
        recovered = dr_model.with_user(self.bot).ingest_operational_batch(payload)
        self.assertEqual(recovered["updated"], 1)
        self.assertEqual(alert.status, "resolved")

    def test_complete_data_quality_ingest_requires_every_event_count(self):
        payload = {
            "environment": "develop",
            "source_updated_at": "2026-07-16T12:00:00Z",
            "items": [
                {
                    "external_key": "develop:data-quality:complete-missing-count",
                    "name": "Incomplete event-stream claim",
                    "started_at": "2026-07-16T11:55:00Z",
                    "finished_at": "2026-07-16T12:00:00Z",
                    "status": "valid",
                    "event_stream_complete": True,
                    "events_sent": 0,
                    "events_received": 0,
                    "events_processed": 0,
                    "events_rejected": 0,
                    "retry_adjustment_count": 0,
                    "duplicate_count": 0,
                    "schema_failure_count": 0,
                    "missing_field_count": 0,
                    "late_event_count": 0,
                }
            ],
        }
        with self.assertRaisesRegex(
            ValidationError,
            "Complete event-stream evidence requires all event fields",
        ):
            self.env["saas.data.quality.run"].with_user(
                self.bot
            ).ingest_operational_batch(payload)

    def test_complete_metric_quality_ingest_is_idempotent(self):
        payload = {
            "environment": "develop",
            "source_updated_at": "2026-07-16T12:05:00Z",
            "items": [
                {
                    "external_key": "develop:metric-quality:2026-07-16T12:00:00Z",
                    "name": "Complete metric quality scan",
                    "started_at": "2026-07-16T12:00:00Z",
                    "finished_at": "2026-07-16T12:04:00Z",
                    "status": "warning",
                    "metric_quality_contract_complete": True,
                    "eligible_metric_count": 100,
                    "fresh_metric_count": 90,
                    "complete_metric_count": 95,
                    "unique_metric_count": 100,
                    "valid_metric_count": 98,
                    "consistent_metric_count": 97,
                    "reconciliation_difference": -2.5,
                    "outlier_count": 3,
                    "unexpected_zero_count": 2,
                    "unexpected_volume_spike_count": 1,
                    "numerator_denominator_violation_count": 1,
                    "negative_value_violation_count": 2,
                    "missing_dimension_count": 5,
                }
            ],
        }
        model = self.env["saas.data.quality.run"].with_user(self.bot)
        first = model.ingest_operational_batch(payload)
        second = model.ingest_operational_batch(payload)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["updated"], 1)
        record = self.env["saas.data.quality.run"].search(
            [
                (
                    "external_key",
                    "=",
                    "develop:metric-quality:2026-07-16T12:00:00Z",
                )
            ]
        )
        self.assertEqual(len(record), 1)
        self.assertTrue(record.metric_quality_contract_complete)
        self.assertEqual(record.eligible_metric_count, 100)

    def test_complete_sync_evidence_is_idempotent_and_stale_safe(self):
        payload = {
            "environment": "develop",
            "source_updated_at": "2026-07-16T12:05:00Z",
            "items": [
                {
                    "external_key": "develop:sync:2026-07-16T12:00:00Z",
                    "name": "Complete Odoo sync attempt",
                    "started_at": "2026-07-16T12:00:00Z",
                    "finished_at": "2026-07-16T12:04:00Z",
                    "status": "partial",
                    "sync_contract_complete": True,
                    "records_read": 100,
                    "records_created": 30,
                    "records_updated": 40,
                    "records_skipped": 20,
                    "records_rejected": 10,
                    "duplicate_upsert_count": 2,
                    "api_error_count": 2,
                    "authentication_error_count": 1,
                    "permission_error_count": 0,
                    "rate_limit_error_count": 1,
                    "retry_count": 3,
                    "backlog_count": 5,
                    "oldest_unsynced_at": "2026-07-16T11:30:00Z",
                    "error_code": "PARTIAL_SOURCE_REJECTS",
                    "drilldown_url": "https://evidence.example.test/sync/123",
                }
            ],
        }
        sync_model = self.env["saas.sync.run"].with_user(self.bot)
        first = sync_model.ingest_sync_run_batch(payload)
        second = sync_model.ingest_sync_run_batch(payload)
        self.assertEqual(first["created"], 1)
        self.assertEqual(second["updated"], 1)

        stale = dict(payload)
        stale["source_updated_at"] = "2026-07-16T12:04:00Z"
        stale_result = sync_model.ingest_sync_run_batch(stale)
        self.assertEqual(stale_result["stale_skipped"], 1)
        record = self.env["saas.sync.run"].search(
            [("external_key", "=", "develop:sync:2026-07-16T12:00:00Z")]
        )
        self.assertEqual(len(record), 1)
        self.assertTrue(record.sync_contract_complete)
        self.assertEqual(record.backlog_count, 5)

        invalid_success = dict(payload)
        invalid_success["source_updated_at"] = "2026-07-16T12:06:00Z"
        invalid_success["items"] = [dict(payload["items"][0], status="success")]
        with self.assertRaisesRegex(
            ValidationError,
            "successful complete sync cannot contain errors",
        ):
            sync_model.ingest_sync_run_batch(invalid_success)

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
