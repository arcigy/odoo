from odoo.tests.common import TransactionCase


class TestSaasImplementationPlan(TransactionCase):
    def test_seeded_items_and_admin_create_contract(self):
        plan = self.env["saas.implementation.plan.item"]
        seeded = plan.search([])
        self.assertGreaterEqual(len(seeded), 12)
        self.assertTrue(
            seeded.mapped("source_document")
            and all(item.source_document == "docs/SAAS_IMPLEMENTATION_PLAN_REMAINING.md" for item in seeded)
        )
        created = plan.create(
            {"name": "Founder-added plan item", "priority": "p1", "scope": "odoo"}
        )
        self.assertEqual(created.status, "planned")
        self.assertEqual(created.owner_id, self.env.user)
