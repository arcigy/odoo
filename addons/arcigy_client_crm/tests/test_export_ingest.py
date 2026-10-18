import copy
import json

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, tagged
from odoo.tools.misc import file_path


def example_batch():
    """The batch the app produces for its golden rows (kitchen_app: src/core/crm-export/fixtures)."""
    with open(file_path("arcigy_client_crm/tests/fixtures/crm-export.v1.example.json"), encoding="utf-8") as handle:
        return json.load(handle)


@tagged("post_install", "-at_install")
class TestExportIngest(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.client = cls.env["res.partner"].create({
            "name": "DELFI", "is_company": True, "arcigy_client_key": "client_delfi",
            "arcigy_client_stage": "paying", "arcigy_mrr": 150.0,
        })
        cls.ingest_user = cls.env["res.users"].create({
            "name": "Arcigy CRM ingest", "login": "arcigy-crm-ingest-test",
            "group_ids": [Command.set([cls.env.ref("base.group_portal").id, cls.env.ref("arcigy_client_crm.group_crm_ingest").id])],
        })
        cls.Batch = cls.env["arcigy.client.export.batch"]

    def ingest(self, payload, user=None):
        return self.Batch.with_user(user or self.ingest_user).arcigy_ingest(payload)

    def test_example_batch_is_applied(self):
        result = self.ingest(example_batch())
        self.assertEqual(result["state"], "applied")
        daily = self.env["arcigy.client.metric.daily"].search([("partner_id", "=", self.client.id)], order="date")
        self.assertEqual(daily.mapped("date"), [fields.Date.to_date("2026-10-11"), fields.Date.to_date("2026-10-12")])
        self.assertEqual(daily.mapped("offers_sent"), [0, 1])
        self.assertEqual(daily.mapped("users_active_30d"), [2, 2])
        self.assertAlmostEqual(sum(daily.mapped("ai_cost_usd_total")), 0.125, places=6)

        month = self.env["arcigy.client.metric.monthly"].search([("partner_id", "=", self.client.id)])
        self.assertEqual(month.projects_over_target, 1)
        self.assertAlmostEqual(month.ai_cost_per_project_p90_usd, 0.11)
        self.assertEqual((month.pdf_items_proposed, month.pdf_items_corrected), (3, 1))
        self.assertAlmostEqual(month.pdf_corrected_pct, 33.3)
        self.assertEqual(month.currency_id, self.env.company.currency_id)
        self.assertAlmostEqual(month.revenue_amount, 150.0)
        self.assertAlmostEqual(month.contribution_margin, month.revenue_amount - month.ai_cost_amount)

        contacts = self.env["res.partner"].search([("parent_id", "=", self.client.id), ("arcigy_user_key", "!=", False)], order="arcigy_user_key")
        self.assertEqual(contacts.mapped("name"), ["Aleš", "Bea", "Cyril"])
        self.assertEqual(contacts.mapped("function"), ["Konštruktér", "Obchod", "Archív"])
        self.assertEqual(contacts.mapped("arcigy_app_role"), ["administrator", "team_member", "observer"])
        self.assertEqual(contacts.sudo().mapped("arcigy_app_user_state"), ["active_30d", "active_30d", "deactivated"])
        user_months = self.env["arcigy.client.user.monthly"].search([("partner_id", "=", self.client.id)])
        self.assertEqual(len(user_months), 3)
        self.assertEqual(user_months.filtered(lambda row: row.contact_id.name == "Aleš").offers_sent, 1)

        self.client.invalidate_recordset()
        self.assertEqual(self.client.arcigy_data_as_of, fields.Date.to_date("2026-10-12"))
        self.assertEqual(self.client.arcigy_offers_sent_4w, 1)
        self.assertEqual(self.client.arcigy_users_active_30d, 2)
        self.assertEqual(self.client.arcigy_app_user_count, 3)

    def test_replay_is_idempotent(self):
        self.ingest(example_batch())
        result = self.ingest(example_batch())
        self.assertTrue(result["replayed"])
        self.assertEqual(self.Batch.search_count([]), 1)
        self.assertEqual(self.env["arcigy.client.metric.daily"].search_count([("partner_id", "=", self.client.id)]), 2)

    def test_older_batch_never_overwrites_newer_rows(self):
        self.ingest(example_batch())
        older = example_batch()
        older["batch_id"] = "prod:2026-10-11..2026-10-12:0000000000000000"
        older["generated_at"] = "2026-10-12T00:30:00.000Z"
        for row in older["clients"][0]["daily"]:
            row["offers_sent"] = 0
        older["control_totals"]["offers_sent"] = 0
        self.assertEqual(self.ingest(older)["state"], "applied")
        day = self.env["arcigy.client.metric.daily"].search([("partner_id", "=", self.client.id), ("date", "=", "2026-10-12")])
        self.assertEqual(day.offers_sent, 1)

    def test_unmatched_clients_are_reported_not_stored(self):
        batch = example_batch()
        stranger = copy.deepcopy(batch["clients"][0])
        stranger["client_key"] = "client_unknown"
        for user in stranger["users"]:
            user["user_key"] = user["user_key"].replace("client_delfi", "client_unknown")
        for row in stranger["user_monthly"]:
            row["user_key"] = row["user_key"].replace("client_delfi", "client_unknown")
        batch["clients"].append(stranger)
        for key in ("projects_created", "offers_sent", "ai_calls"):
            batch["control_totals"][key] *= 2
        batch["control_totals"]["ai_cost_usd"] = round(batch["control_totals"]["ai_cost_usd"] * 2, 6)
        batch["batch_id"] = "prod:2026-10-11..2026-10-12:1111111111111111"
        result = self.ingest(batch)
        self.assertEqual(result["state"], "applied_with_unmatched")
        self.assertEqual(result["unmatched"], ["client_unknown"])
        self.assertFalse(self.env["res.partner"].search([("arcigy_user_key", "like", "client_unknown:%")]))

    def test_invalid_batches_are_recorded_as_rejected_and_not_applied(self):
        tampered = example_batch()
        tampered["clients"][0]["daily"][1]["offers_sent"] = 5
        result = self.ingest(tampered)
        self.assertEqual(result["state"], "rejected")
        self.assertIn("control total offers_sent", result["error"])

        other_environment = example_batch()
        other_environment["batch_id"] = "dev:2026-10-11..2026-10-12:2222222222222222"
        other_environment["environment"] = "dev"
        self.assertIn("not accepted", self.ingest(other_environment)["error"])

        future = example_batch()
        future["batch_id"] = "prod:2026-10-11..2026-10-12:3333333333333333"
        future["schema_version"] = 2
        self.assertIn("schema_version 2", self.ingest(future)["error"])

        self.assertFalse(self.env["arcigy.client.metric.daily"].search([("partner_id", "=", self.client.id)]))
        self.assertEqual(set(self.Batch.search([]).mapped("state")), {"rejected"})

    def test_users_who_left_the_app_are_deactivated(self):
        self.ingest(example_batch())
        later = example_batch()
        later["batch_id"] = "prod:2026-10-11..2026-10-12:4444444444444444"
        later["generated_at"] = "2026-10-14T00:30:00.000Z"
        later["clients"][0]["users"] = [user for user in later["clients"][0]["users"] if user["name"] != "Bea"]
        later["clients"][0]["user_monthly"] = [row for row in later["clients"][0]["user_monthly"] if row["user_key"].endswith(":u1")]
        self.ingest(later)
        bea = self.env["res.partner"].search([("arcigy_user_key", "=", "client_delfi:u2")])
        self.assertEqual(bea.sudo().arcigy_app_user_state, "deactivated")

    def test_only_the_ingest_group_may_send(self):
        internal = self.env["res.users"].create({
            "name": "Someone", "login": "someone-crm-test",
            "group_ids": [Command.set([self.env.ref("base.group_user").id])],
        })
        with self.assertRaises(AccessError):
            self.ingest(example_batch(), user=internal)
