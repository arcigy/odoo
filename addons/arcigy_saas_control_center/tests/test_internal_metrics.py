from datetime import timedelta

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestInternalOperationalMetrics(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.current = cls.env["saas.metric.current"]
        cls.develop = cls.env.ref("arcigy_saas_control_center.environment_develop")
        cls.main = cls.env.ref("arcigy_saas_control_center.environment_main")
        cls.admin = cls.env.ref("base.user_admin")

    def _incident(self, environment, name):
        return self.env["saas.incident"].create(
            {
                "name": name,
                "severity": "p1",
                "status": "open",
                "environment_id": environment.id,
                "runbook_url": "https://runbooks.example.test/incident",
                "owner_id": self.admin.id,
            }
        )

    def test_refresh_emits_true_zero_incident_counts_but_omits_missing_ages(self):
        result = self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(result["refreshed"], 2)
        self.assertEqual(len(result["omitted"]), 100)

        metric = self.env.ref(
            "arcigy_saas_control_center.metric_open_critical_incidents"
        )
        records = self.current.search([("metric_id", "=", metric.id)])
        self.assertEqual(len(records), 2)
        self.assertEqual(set(records.mapped("environment_id.code")), {"develop", "main"})
        self.assertTrue(all(record.current_value == 0 for record in records))
        self.assertTrue(all(record.status == "healthy" for record in records))
        self.assertFalse(
            self.current.search(
                [
                    (
                        "metric_id.code",
                        "in",
                        [
                            "backup_age_seconds",
                            "restore_test_age_seconds",
                            "odoo_sync_freshness_seconds",
                            "restore_test_success_rate",
                            "actual_rpo_seconds",
                            "actual_rto_seconds",
                            "tested_concurrent_users",
                            "load_test_age_days",
                            "capacity_readiness_status",
                        ],
                    )
                ]
            )
        )

        history_before = self.env["saas.metric.timeseries"].search_count(
            [("metric_id", "=", metric.id)]
        )
        self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(len(self.current.search([("metric_id", "=", metric.id)])), 2)
        self.assertEqual(
            self.env["saas.metric.timeseries"].search_count(
                [("metric_id", "=", metric.id)]
            ),
            history_before,
        )

    def test_refresh_derives_only_valid_operational_evidence(self):
        now = fields.Datetime.now()
        self._incident(self.develop, "Develop incident")
        self.env["saas.backup.run"].create(
            {
                "name": "Verified encrypted off-host backup",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=3),
                "finished_at": now - timedelta(minutes=2),
                "status": "success",
                "backup_type": "full",
                "encrypted": True,
                "off_host": True,
                "external_key": "develop:test:valid-backup",
                "source_updated_at": now - timedelta(minutes=2),
            }
        )
        self.env["saas.backup.run"].create(
            {
                "name": "Unencrypted backup must not qualify",
                "environment_id": self.main.id,
                "started_at": now - timedelta(minutes=3),
                "finished_at": now - timedelta(minutes=2),
                "status": "success",
                "backup_type": "full",
                "encrypted": False,
                "off_host": True,
                "external_key": "main:test:unencrypted-backup",
                "source_updated_at": now - timedelta(minutes=2),
            }
        )
        self.env["saas.restore.test"].create(
            {
                "name": "Verified isolated restore",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=5),
                "finished_at": now - timedelta(minutes=4),
                "status": "success",
                "actual_rpo_seconds": 0,
                "actual_rto_seconds": 240,
                "rpo_measured": True,
                "rto_measured": True,
                "checksum_valid": True,
                "application_smoke_passed": True,
                "tenant_isolation_passed": True,
                "external_key": "develop:test:valid-restore",
                "source_updated_at": now - timedelta(minutes=4),
                "owner_id": self.admin.id,
            }
        )
        self.env["saas.sync.run"].create(
            {
                "name": "Verified external metric sync",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=2),
                "finished_at": now - timedelta(minutes=1),
                "status": "success",
            }
        )
        self.env["saas.load.test"].create(
            {
                "name": "Representative architecture load test",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=10),
                "finished_at": now - timedelta(minutes=8),
                "status": "ready_with_risk",
                "test_type": "ramp",
                "representative": True,
                "architecture_version": "architecture-v1",
                "concurrent_users": 1000,
                "requests_per_second": 25,
                "p95_seconds": 1.2,
                "p99_seconds": 2.5,
                "error_rate": 0.2,
                "external_key": "develop:test:representative-load",
                "source_updated_at": now - timedelta(minutes=8),
            }
        )

        result = self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(result["refreshed"], 11)
        self.assertEqual(len(result["omitted"]), 91)
        by_code = {
            record.metric_id.code: record
            for record in self.current.search(
                [("environment_id", "=", self.develop.id)]
            )
        }
        self.assertEqual(by_code["open_p0_p1_incidents"].current_value, 1)
        self.assertEqual(by_code["open_p0_p1_incidents"].status, "warning")
        self.assertGreaterEqual(by_code["backup_age_seconds"].current_value, 119)
        self.assertGreaterEqual(by_code["restore_test_age_seconds"].current_value, 239)
        self.assertGreaterEqual(by_code["odoo_sync_freshness_seconds"].current_value, 59)
        self.assertEqual(by_code["restore_test_success_rate"].current_value, 100)
        self.assertEqual(by_code["restore_test_success_rate"].numerator, 1)
        self.assertEqual(by_code["restore_test_success_rate"].denominator, 1)
        self.assertEqual(by_code["actual_rpo_seconds"].current_value, 0)
        self.assertEqual(by_code["actual_rto_seconds"].current_value, 240)
        self.assertEqual(by_code["tested_concurrent_users"].current_value, 1000)
        self.assertGreaterEqual(by_code["load_test_age_days"].current_value, 8 / 1440)
        self.assertEqual(by_code["capacity_readiness_status"].current_value, 0.66)
        self.assertEqual(by_code["capacity_readiness_status"].status, "warning")
        self.assertFalse(
            self.current.search(
                [
                    ("environment_id", "=", self.main.id),
                    ("metric_id.code", "=", "backup_age_seconds"),
                ]
            )
        )
        histories = self.env["saas.metric.timeseries"].search(
            [("environment_id", "=", self.develop.id)]
        )
        self.assertEqual(len(histories), 10)
        self.assertTrue(
            all(
                history.external_key.startswith("develop:odoo-internal:")
                for history in histories
            )
        )
        self.assertEqual(
            len(histories.filtered(lambda history: history.granularity == "event")),
            5,
        )
        self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(
            self.env["saas.metric.timeseries"].search_count(
                [("environment_id", "=", self.develop.id)]
            ),
            10,
        )

    def test_latest_failed_restore_is_not_reported_as_success(self):
        now = fields.Datetime.now()
        self.env["saas.restore.test"].create(
            {
                "name": "Earlier verified restore",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(hours=2, minutes=5),
                "finished_at": now - timedelta(hours=2),
                "status": "success",
                "actual_rpo_seconds": 0,
                "actual_rto_seconds": 300,
                "rpo_measured": True,
                "rto_measured": True,
                "checksum_valid": True,
                "application_smoke_passed": True,
                "tenant_isolation_passed": True,
                "external_key": "develop:test:earlier-restore",
                "source_updated_at": now - timedelta(hours=2),
            }
        )
        self.env["saas.restore.test"].create(
            {
                "name": "Latest failed restore",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=10),
                "finished_at": now - timedelta(minutes=5),
                "status": "failed",
                "external_key": "develop:test:latest-failed-restore",
                "source_updated_at": now - timedelta(minutes=5),
            }
        )

        self.current._cron_refresh_internal_operational_metrics()
        by_code = {
            record.metric_id.code: record
            for record in self.current.search(
                [("environment_id", "=", self.develop.id)]
            )
        }
        self.assertEqual(by_code["restore_test_success_rate"].current_value, 0)
        self.assertEqual(by_code["restore_test_success_rate"].numerator, 0)
        self.assertEqual(by_code["restore_test_success_rate"].denominator, 1)
        self.assertEqual(by_code["actual_rpo_seconds"].current_value, 0)
        self.assertEqual(by_code["actual_rto_seconds"].current_value, 300)
        self.assertGreaterEqual(by_code["restore_test_age_seconds"].current_value, 7200)

    def test_refresh_derives_only_complete_event_stream_evidence(self):
        now = fields.Datetime.now()
        self.env["saas.data.quality.run"].create(
            {
                "name": "Complete Develop event stream",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=5),
                "finished_at": now - timedelta(minutes=1),
                "status": "warning",
                "event_stream_complete": True,
                "events_sent": 100,
                "events_received": 97,
                "events_processed": 97,
                "events_rejected": 0,
                "retry_adjustment_count": 2,
                "duplicate_count": 1,
                "schema_failure_count": 0,
                "missing_field_count": 0,
                "late_event_count": 1,
                "unknown_tenant_count": 0,
                "clock_skew_seconds": 3.5,
                "processing_lag_p95_seconds": 0.8,
                "dead_letter_count": 1,
                "external_key": "develop:test:complete-event-stream",
                "source_updated_at": now - timedelta(minutes=1),
            }
        )
        self.env["saas.data.quality.run"].create(
            {
                "name": "Complete empty Main event stream",
                "environment_id": self.main.id,
                "started_at": now - timedelta(minutes=5),
                "finished_at": now - timedelta(minutes=1),
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
                "unknown_tenant_count": 0,
                "clock_skew_seconds": 0,
                "processing_lag_p95_seconds": 0,
                "dead_letter_count": 0,
                "external_key": "main:test:complete-empty-event-stream",
                "source_updated_at": now - timedelta(minutes=1),
            }
        )

        result = self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(result["refreshed"], 26)
        self.assertEqual(len(result["omitted"]), 76)
        develop_values = {
            record.metric_id.code: record
            for record in self.current.search(
                [("environment_id", "=", self.develop.id)]
            )
        }
        self.assertEqual(develop_values["events_sent_count"].current_value, 100)
        self.assertEqual(develop_values["events_received_count"].current_value, 97)
        self.assertEqual(develop_values["events_processed_count"].current_value, 97)
        self.assertEqual(develop_values["events_rejected_count"].current_value, 0)
        self.assertEqual(develop_values["event_loss_estimate"].current_value, 1)
        self.assertAlmostEqual(
            develop_values["event_duplicate_rate"].current_value,
            100 / 97,
        )
        self.assertEqual(develop_values["event_duplicate_rate"].numerator, 1)
        self.assertEqual(develop_values["event_duplicate_rate"].denominator, 97)
        self.assertAlmostEqual(develop_values["late_event_rate"].current_value, 100 / 97)
        self.assertEqual(
            develop_values["unknown_tenant_mapping_count"].current_value,
            0,
        )
        self.assertEqual(develop_values["event_clock_skew_seconds"].current_value, 3.5)
        self.assertEqual(
            develop_values["event_processing_lag_p95_seconds"].current_value,
            0.8,
        )
        self.assertEqual(develop_values["dead_letter_event_count"].current_value, 1)
        self.assertFalse(
            self.current.search(
                [
                    ("environment_id", "=", self.main.id),
                    (
                        "metric_id.code",
                        "in",
                        ["event_duplicate_rate", "late_event_rate"],
                    ),
                ]
            )
        )
        history_count = self.env["saas.metric.timeseries"].search_count([])
        self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(
            self.env["saas.metric.timeseries"].search_count([]),
            history_count,
        )

    def test_complete_event_stream_rejects_inconsistent_counts(self):
        now = fields.Datetime.now()
        with self.assertRaisesRegex(
            ValidationError,
            "Processed and rejected events cannot exceed received events",
        ):
            self.env["saas.data.quality.run"].create(
                {
                    "name": "Inconsistent event stream",
                    "environment_id": self.develop.id,
                    "started_at": now - timedelta(minutes=5),
                    "finished_at": now - timedelta(minutes=1),
                    "status": "invalid",
                    "event_stream_complete": True,
                    "events_sent": 10,
                    "events_received": 9,
                    "events_processed": 9,
                    "events_rejected": 1,
                    "retry_adjustment_count": 1,
                    "duplicate_count": 0,
                    "schema_failure_count": 0,
                    "missing_field_count": 0,
                    "late_event_count": 0,
                    "unknown_tenant_count": 0,
                    "clock_skew_seconds": 0,
                    "processing_lag_p95_seconds": 0,
                    "dead_letter_count": 0,
                    "external_key": "develop:test:inconsistent-event-stream",
                    "source_updated_at": now - timedelta(minutes=1),
                }
            )

    def test_refresh_derives_only_complete_metric_quality_evidence(self):
        now = fields.Datetime.now()
        self.env["saas.data.quality.run"].create(
            {
                "name": "Complete Develop metric quality scan",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=5),
                "finished_at": now - timedelta(minutes=1),
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
                "external_key": "develop:test:complete-metric-quality",
                "source_updated_at": now - timedelta(minutes=1),
            }
        )
        self.env["saas.data.quality.run"].create(
            {
                "name": "Complete empty Main metric quality scan",
                "environment_id": self.main.id,
                "started_at": now - timedelta(minutes=5),
                "finished_at": now - timedelta(minutes=1),
                "status": "valid",
                "metric_quality_contract_complete": True,
                "eligible_metric_count": 0,
                "fresh_metric_count": 0,
                "complete_metric_count": 0,
                "unique_metric_count": 0,
                "valid_metric_count": 0,
                "consistent_metric_count": 0,
                "reconciliation_difference": 0,
                "outlier_count": 0,
                "unexpected_zero_count": 0,
                "unexpected_volume_spike_count": 0,
                "numerator_denominator_violation_count": 0,
                "negative_value_violation_count": 0,
                "missing_dimension_count": 0,
                "external_key": "main:test:complete-empty-metric-quality",
                "source_updated_at": now - timedelta(minutes=1),
            }
        )

        result = self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(result["refreshed"], 21)
        self.assertEqual(len(result["omitted"]), 81)
        develop_values = {
            record.metric_id.code: record
            for record in self.current.search(
                [("environment_id", "=", self.develop.id)]
            )
        }
        self.assertEqual(develop_values["metric_freshness_rate"].current_value, 90)
        self.assertEqual(develop_values["metric_freshness_rate"].numerator, 90)
        self.assertEqual(develop_values["metric_freshness_rate"].denominator, 100)
        self.assertEqual(develop_values["metric_completeness_rate"].current_value, 95)
        self.assertEqual(develop_values["metric_uniqueness_rate"].current_value, 100)
        self.assertEqual(develop_values["metric_validity_rate"].current_value, 98)
        self.assertEqual(develop_values["metric_consistency_rate"].current_value, 97)
        self.assertEqual(
            develop_values["data_quality_reconciliation_difference"].current_value,
            2.5,
        )
        self.assertEqual(develop_values["data_quality_outlier_count"].current_value, 3)
        self.assertEqual(develop_values["unexpected_zero_value_count"].current_value, 2)
        self.assertEqual(develop_values["unexpected_volume_spike_count"].current_value, 1)
        self.assertEqual(
            develop_values["numerator_denominator_violation_count"].current_value,
            1,
        )
        self.assertEqual(develop_values["negative_value_violation_count"].current_value, 2)
        self.assertEqual(develop_values["missing_dimension_count"].current_value, 5)
        self.assertFalse(
            self.current.search(
                [
                    ("environment_id", "=", self.main.id),
                    (
                        "metric_id.code",
                        "in",
                        [
                            "metric_freshness_rate",
                            "metric_completeness_rate",
                            "metric_uniqueness_rate",
                            "metric_validity_rate",
                            "metric_consistency_rate",
                        ],
                    ),
                ]
            )
        )

    def test_complete_metric_quality_rejects_counts_above_population(self):
        now = fields.Datetime.now()
        with self.assertRaisesRegex(
            ValidationError,
            "Metric-quality result counts cannot exceed eligible metrics",
        ):
            self.env["saas.data.quality.run"].create(
                {
                    "name": "Invalid metric quality scan",
                    "environment_id": self.develop.id,
                    "started_at": now - timedelta(minutes=5),
                    "finished_at": now - timedelta(minutes=1),
                    "status": "invalid",
                    "metric_quality_contract_complete": True,
                    "eligible_metric_count": 1,
                    "fresh_metric_count": 2,
                    "complete_metric_count": 1,
                    "unique_metric_count": 1,
                    "valid_metric_count": 1,
                    "consistent_metric_count": 1,
                    "reconciliation_difference": 0,
                    "outlier_count": 0,
                    "unexpected_zero_count": 0,
                    "unexpected_volume_spike_count": 0,
                    "numerator_denominator_violation_count": 0,
                    "negative_value_violation_count": 0,
                    "missing_dimension_count": 0,
                    "external_key": "develop:test:invalid-metric-quality",
                    "source_updated_at": now - timedelta(minutes=1),
                }
            )

    def test_refresh_derives_complete_sync_attempt_and_backlog(self):
        now = fields.Datetime.now()
        self.env["saas.sync.run"].create(
            {
                "name": "Complete partial Develop sync",
                "environment_id": self.develop.id,
                "started_at": now - timedelta(minutes=5),
                "finished_at": now - timedelta(minutes=1),
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
                "oldest_unsynced_at": now - timedelta(minutes=30),
                "error_code": "PARTIAL_SOURCE_REJECTS",
                "external_key": "develop:test:complete-sync-attempt",
                "source_updated_at": now - timedelta(minutes=1),
            }
        )

        result = self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(result["refreshed"], 18)
        self.assertEqual(len(result["omitted"]), 84)
        sync_codes = {
            "odoo_sync_attempt_age_seconds",
            "odoo_sync_duration_seconds",
            "odoo_sync_records_read_count",
            "odoo_sync_records_created_count",
            "odoo_sync_records_updated_count",
            "odoo_sync_records_skipped_count",
            "odoo_sync_records_rejected_count",
            "odoo_sync_duplicate_upsert_count",
            "odoo_sync_api_error_count",
            "odoo_sync_authentication_error_count",
            "odoo_sync_permission_error_count",
            "odoo_sync_rate_limit_error_count",
            "odoo_sync_retry_count",
            "odoo_sync_backlog_count",
            "odoo_sync_oldest_unsynced_age_seconds",
            "odoo_sync_error_rate",
        }
        sync_values = {
            record.metric_id.code: record
            for record in self.current.search(
                [
                    ("environment_id", "=", self.develop.id),
                    ("metric_id.code", "in", list(sync_codes)),
                ]
            )
        }
        self.assertEqual(set(sync_values), sync_codes)
        self.assertGreaterEqual(
            sync_values["odoo_sync_attempt_age_seconds"].current_value,
            300,
        )
        self.assertEqual(sync_values["odoo_sync_duration_seconds"].current_value, 240)
        self.assertEqual(sync_values["odoo_sync_records_read_count"].current_value, 100)
        self.assertEqual(sync_values["odoo_sync_records_created_count"].current_value, 30)
        self.assertEqual(sync_values["odoo_sync_records_updated_count"].current_value, 40)
        self.assertEqual(sync_values["odoo_sync_records_skipped_count"].current_value, 20)
        self.assertEqual(sync_values["odoo_sync_records_rejected_count"].current_value, 10)
        self.assertEqual(sync_values["odoo_sync_duplicate_upsert_count"].current_value, 2)
        self.assertEqual(sync_values["odoo_sync_api_error_count"].current_value, 2)
        self.assertEqual(
            sync_values["odoo_sync_authentication_error_count"].current_value,
            1,
        )
        self.assertEqual(sync_values["odoo_sync_rate_limit_error_count"].current_value, 1)
        self.assertEqual(sync_values["odoo_sync_retry_count"].current_value, 3)
        self.assertEqual(sync_values["odoo_sync_backlog_count"].current_value, 5)
        self.assertGreaterEqual(
            sync_values["odoo_sync_oldest_unsynced_age_seconds"].current_value,
            1800,
        )
        self.assertAlmostEqual(
            sync_values["odoo_sync_error_rate"].current_value,
            12 / 102 * 100,
        )
        self.assertEqual(sync_values["odoo_sync_error_rate"].numerator, 12)
        self.assertEqual(sync_values["odoo_sync_error_rate"].denominator, 102)
        sync_history = self.env["saas.metric.timeseries"].search(
            [
                ("environment_id", "=", self.develop.id),
                ("metric_id.code", "in", list(sync_codes)),
            ]
        )
        self.assertEqual(len(sync_history), 16)
        self.assertEqual(
            len(sync_history.filtered(lambda row: row.granularity == "event")),
            14,
        )
        history_count = len(sync_history)
        self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(
            self.env["saas.metric.timeseries"].search_count(
                [
                    ("environment_id", "=", self.develop.id),
                    ("metric_id.code", "in", list(sync_codes)),
                ]
            ),
            history_count,
        )

    def test_invalid_restore_and_capacity_claims_fail_closed(self):
        now = fields.Datetime.now()
        with self.assertRaises(ValidationError):
            self.env["saas.restore.test"].create(
                {
                    "name": "Unverified restore claim",
                    "environment_id": self.develop.id,
                    "started_at": now - timedelta(minutes=5),
                    "finished_at": now - timedelta(minutes=4),
                    "status": "success",
                    "external_key": "develop:test:unverified-restore",
                    "source_updated_at": now - timedelta(minutes=4),
                }
            )
        with self.assertRaises(ValidationError):
            self.env["saas.load.test"].create(
                {
                    "name": "Unqualified representative claim",
                    "environment_id": self.develop.id,
                    "started_at": now - timedelta(minutes=5),
                    "finished_at": now - timedelta(minutes=4),
                    "status": "ready",
                    "test_type": "ramp",
                    "representative": True,
                    "concurrent_users": 1000,
                    "external_key": "develop:test:unqualified-load",
                    "source_updated_at": now - timedelta(minutes=4),
                }
            )

    def test_incident_metric_does_not_count_its_own_generated_incident(self):
        self._incident(self.develop, "First Develop incident")
        self._incident(self.develop, "Second Develop incident")
        self.current._cron_refresh_internal_operational_metrics()
        metric = self.env.ref(
            "arcigy_saas_control_center.metric_open_critical_incidents"
        )
        value = self.current.search(
            [
                ("metric_id", "=", metric.id),
                ("environment_id", "=", self.develop.id),
            ],
            limit=1,
        )
        self.assertEqual(value.current_value, 2)
        self.assertEqual(value.status, "critical")
        self.assertTrue(
            self.env["saas.incident"].search(
                [
                    ("environment_id", "=", self.develop.id),
                    ("triggering_metric_id", "=", metric.id),
                ],
                limit=1,
            )
        )
        self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(value.current_value, 2)
