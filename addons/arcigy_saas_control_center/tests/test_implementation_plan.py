from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError, ValidationError


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

    def test_codex_worker_can_claim_plan_and_mark_task_ready(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {
                "name": "Codex queue test worker",
                "login": "codex-queue-test@example.invalid",
                "group_ids": [(6, 0, [group.id])],
            }
        )
        item = plan.create(
            {
                "name": "Codex workflow contract",
                "priority": "p1",
                "scope": "arcigy",
                "full_prompt": "Add the bounded implementation workflow without changing production behavior.",
            }
        )
        worker_plan = plan.with_user(worker)
        claim = worker_plan.codex_claim_next({"position": len(plan.search([("status", "in", ["planned", "changes_requested"])])), "worker_name": "test"})
        self.assertEqual(claim["task"]["id"], item.id)
        self.assertEqual(item.status, "planning")
        saved = worker_plan.codex_save_plan(
            {
                "task_id": item.id,
                "run_token": claim["run_token"],
                "plan": "Inspect the current contract, add a focused regression test, implement the queue change, and verify the resulting workflow.",
                "risk_level": "medium",
                "target_repository": "kitchen",
                "target_environment": "develop",
                "model": "gpt-5.6-sol",
            }
        )
        self.assertFalse(saved["approval_required"])
        self.assertEqual(item.status, "in_progress")
        self.assertEqual(item.current_codex_run_id.phase, "ready")
        execution = worker_plan.codex_claim_execution(
            {"task_id": item.id, "worker_name": "executor", "lease_minutes": 30}
        )
        self.assertNotEqual(execution["run_token"], claim["run_token"])
        worker_plan.codex_update_run(
            {"task_id": item.id, "run_token": execution["run_token"], "phase": "testing", "execute_model": "gpt-5.6-terra"}
        )
        self.assertEqual(item.status, "testing")
        worker_plan.codex_mark_ready(
            {
                "task_id": item.id,
                "run_token": execution["run_token"],
                "result_summary": "The bounded workflow is implemented and its focused contract test passed.",
                "review_checklist": "Open the task, confirm the plan and test evidence, then verify the linked development preview.",
                "test_summary": "Focused Odoo implementation-plan test passed.",
            }
        )
        self.assertEqual(item.status, "ready_for_review")
        self.assertEqual(item.current_codex_run_id.phase, "ready")

    def test_codex_queue_rejects_unprivileged_access(self):
        user = self.env["res.users"].create({"name": "No queue access", "login": "no-queue@example.invalid"})
        with self.assertRaises(AccessError):
            self.env["saas.implementation.plan.item"].with_user(user).codex_claim_next({})

    def test_codex_daily_limit_reserves_no_more_than_the_requested_number_of_tasks(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {"name": "Codex daily worker", "login": "codex-daily@example.invalid", "group_ids": [(6, 0, [group.id])]}
        )
        worker_plan = plan.with_user(worker)
        first = worker_plan.codex_claim_next(
            {"worker_name": "daily", "daily_batch_date": "2026-08-04", "daily_limit": 1}
        )
        self.assertTrue(first["task"])
        self.assertEqual(plan.browse(first["task"]["id"]).daily_batch_slot, 1)
        second = worker_plan.codex_claim_next(
            {"worker_name": "daily", "daily_batch_date": "2026-08-04", "daily_limit": 1}
        )
        self.assertFalse(second["task"])
        self.assertEqual(second["reason"], "daily_limit_reached")

    def test_codex_worker_splits_a_broad_task_into_ordered_review_slices(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {"name": "Codex split worker", "login": "codex-split@example.invalid", "group_ids": [(6, 0, [group.id])]}
        )
        broad_task = plan.create(
            {"name": "Broad workflow", "priority": "p1", "scope": "arcigy", "full_prompt": "Deliver the complete long-running implementation workflow."}
        )
        worker_plan = plan.with_user(worker)
        claim = worker_plan.codex_claim_next({"position": len(plan.search([("status", "=", "planned")])), "worker_name": "test"})
        self.assertEqual(claim["task"]["id"], broad_task.id)
        result = worker_plan.codex_split(
            {
                "task_id": broad_task.id,
                "run_token": claim["run_token"],
                "items": [
                    {"name": "Plan contract", "prompt": "Create and verify the bounded planning contract for the automation workflow."},
                    {"name": "Implement runner", "prompt": "Implement and test the isolated runner that consumes the approved planning contract."},
                ],
            }
        )
        self.assertEqual(result["status"], "split")
        children = plan.search([("parent_id", "=", broad_task.id)], order="sequence, id")
        broad_task.invalidate_recordset(["sequence"])
        self.assertEqual(len(children), 2)
        self.assertEqual(children.mapped("status"), ["planned", "planned"])
        self.assertLess(children[-1].sequence, broad_task.sequence)
