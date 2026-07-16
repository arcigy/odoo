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
        self.assertEqual(len(result["omitted"]), 18)

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
        self.assertEqual(len(result["omitted"]), 9)
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
