from odoo.tests.common import TransactionCase
from odoo.exceptions import ValidationError


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

    def test_ready_for_review_requires_a_precise_checklist(self):
        plan = self.env["saas.implementation.plan.item"]
        item = plan.create({"name": "Review gate", "priority": "p1", "scope": "odoo"})
        with self.assertRaises(ValidationError):
            item.status = "ready_for_review"
        item.write({
            "status": "ready_for_review",
            "review_checklist": "Open the feature and verify the saved result after reload.",
        })
        self.assertEqual(item.status, "ready_for_review")

    def test_inserting_and_moving_tasks_keeps_one_unique_queue(self):
        plan = self.env["saas.implementation.plan.item"]
        initial_count = len(plan.search([]))
        first = plan.create({"name": "First", "priority": "p1", "scope": "odoo"})
        second = plan.create({"name": "Second", "priority": "p1", "scope": "odoo"})
        inserted = plan.create_at_position(
            {"name": "Inserted", "priority": "p1", "scope": "odoo"}, 2
        )
        self.assertEqual(inserted.sequence, 2)
        self.assertEqual(first.sequence, initial_count + 2)
        self.assertEqual(second.sequence, initial_count + 3)
        second.sequence = 1
        self.assertEqual(second.sequence, 1)
        self.assertEqual(inserted.sequence, 3)
        sequences = plan.search([], order="sequence").mapped("sequence")
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
