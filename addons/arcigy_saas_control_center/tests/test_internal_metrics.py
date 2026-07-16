from datetime import timedelta

from odoo import fields
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
        self.assertEqual(len(result["omitted"]), 6)

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

        result = self.current._cron_refresh_internal_operational_metrics()
        self.assertEqual(result["refreshed"], 5)
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
        self.assertEqual(len(histories), 4)
        self.assertTrue(
            all(
                history.external_key.startswith("develop:odoo-internal:")
                for history in histories
            )
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
