from odoo.tests.common import TransactionCase
from odoo.exceptions import AccessError, ValidationError


class TestSaasImplementationPlan(TransactionCase):
    def _administrator_plan(self):
        """Create queue-eligible records as Odoo's actual Administrator user."""
        administrator = self.env.ref("base.user_admin")
        administrator.write(
            {
                "group_ids": [
                    (4, self.env.ref("arcigy_saas_control_center.group_saas_administrator").id)
                ]
            }
        )
        return self.env["saas.implementation.plan.item"].with_user(administrator)

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

    def test_administrator_can_retry_a_transiently_blocked_item(self):
        plan = self.env["saas.implementation.plan.item"]
        item = plan.create({"name": "Retry bridge startup", "priority": "p1", "scope": "odoo"})
        item.write({"status": "blocked", "blocker": "The local automation bridge stopped before Codex could start."})
        item.action_retry_automation()
        self.assertEqual(item.status, "planned")
        self.assertFalse(item.blocker)

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

    def test_deleting_a_task_compacts_all_later_positions(self):
        plan = self.env["saas.implementation.plan.item"]
        initial_count = len(plan.search([]))
        first = plan.create({"name": "Delete first", "priority": "p1", "scope": "odoo"})
        removed = plan.create({"name": "Delete middle", "priority": "p1", "scope": "odoo"})
        last = plan.create({"name": "Delete last", "priority": "p1", "scope": "odoo"})
        self.assertEqual([first.sequence, removed.sequence, last.sequence], [initial_count + 1, initial_count + 2, initial_count + 3])
        removed.unlink()
        self.assertEqual(first.sequence, initial_count + 1)
        self.assertEqual(last.sequence, initial_count + 2)
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
        item = self._administrator_plan().create(
            {
                "name": "Codex workflow contract",
                "priority": "p1",
                "scope": "arcigy",
                "full_prompt": "Add the bounded implementation workflow without changing production behavior.",
            }
        )
        worker_plan = plan.with_user(worker)
        claim = worker_plan.codex_claim_next({"task_id": item.id, "worker_name": "test"})
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
        self.assertEqual(worker_plan._codex_payload(item)["plan_model"], "gpt-5.6-sol")
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
        self.assertTrue(
            item.activity_ids.filtered(
                lambda activity: activity.summary == "Skontrolovať dokončené úpravy"
            )
        )
        self.assertTrue(
            item.message_ids.filtered(
                lambda message: "Dokončené úpravy čakajú na kontrolu" in (message.body or "")
            )
        )
        lifecycle_message = item.message_ids.filtered(
            lambda message: "Dokončené úpravy čakajú na kontrolu" in (message.body or "")
        )
        self.assertEqual(lifecycle_message.author_id, self.env.ref("base.partner_root"))

    def test_approval_ready_plan_notifies_the_owner_without_reopening_the_task(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {
                "name": "Codex approval notification worker",
                "login": "codex-approval-notification@example.invalid",
                "group_ids": [(6, 0, [group.id])],
            }
        )
        item = self._administrator_plan().create(
            {
                "name": "Approval notification contract",
                "priority": "p1",
                "scope": "cross_system",
                "full_prompt": "Create a plan that needs founder approval before any production-facing work starts.",
            }
        )
        worker_plan = plan.with_user(worker)
        claim = worker_plan.codex_claim_next({"task_id": item.id, "worker_name": "approval-notification"})
        saved = worker_plan.codex_save_plan(
            {
                "task_id": item.id,
                "run_token": claim["run_token"],
                "plan": "Inspect the release contract, document the production effect, wait for founder approval, then implement only after the approval is recorded.",
                "risk_level": "approval",
                "target_repository": "cross_system",
                "target_environment": "approval",
                "model": "gpt-5.6-sol",
            }
        )
        self.assertTrue(saved["approval_required"])
        self.assertEqual(item.status, "awaiting_approval")
        self.assertTrue(
            item.activity_ids.filtered(
                lambda activity: activity.summary == "Schváliť Codex plán"
            )
        )
        self.assertTrue(
            item.message_ids.filtered(
                lambda message: "Codex plán čaká na schválenie" in (message.body or "")
            )
        )
        lifecycle_message = item.message_ids.filtered(
            lambda message: "Codex plán čaká na schválenie" in (message.body or "")
        )
        self.assertEqual(lifecycle_message.author_id, self.env.ref("base.partner_root"))

    def test_codex_worker_can_claim_an_exact_eligible_task_id(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {
                "name": "Codex exact-task worker",
                "login": "codex-exact-task@example.invalid",
                "group_ids": [(6, 0, [group.id])],
            }
        )
        first = self._administrator_plan().create({"name": "Earlier task", "priority": "p1", "scope": "arcigy"})
        target = self._administrator_plan().create({"name": "Exact task", "priority": "p1", "scope": "arcigy"})
        claim = plan.with_user(worker).codex_claim_next(
            {"task_id": target.id, "expected_name": target.name, "worker_name": "exact-task-test"}
        )
        self.assertEqual(claim["task"]["id"], target.id)
        self.assertEqual(target.status, "planning")
        self.assertEqual(first.status, "planned")

    def test_exact_task_claim_rejects_a_name_mismatch_without_claiming(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {
                "name": "Codex exact-task mismatch worker",
                "login": "codex-exact-task-mismatch@example.invalid",
                "group_ids": [(6, 0, [group.id])],
            }
        )
        target = self._administrator_plan().create({"name": "Protected exact task", "priority": "p1", "scope": "arcigy"})
        with self.assertRaises(ValidationError):
            plan.with_user(worker).codex_claim_next(
                {"task_id": target.id, "expected_name": "Different task", "worker_name": "exact-task-test"}
            )
        self.assertEqual(target.status, "planned")

    def test_codex_task_retains_one_thread_across_retries(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {
                "name": "Codex one-thread worker",
                "login": "codex-one-thread@example.invalid",
                "group_ids": [(6, 0, [group.id])],
            }
        )
        item = self._administrator_plan().create(
            {"name": "One Codex conversation", "priority": "p1", "scope": "arcigy"}
        )
        worker_plan = plan.with_user(worker)
        initial = worker_plan.codex_claim_next({"task_id": item.id, "worker_name": "one-thread"})
        worker_plan.codex_update_run(
            {
                "task_id": item.id,
                "run_token": initial["run_token"],
                "phase": "planning",
                "codex_thread_id": "thread-original",
            }
        )
        self.assertEqual(worker_plan._codex_payload(item)["codex_thread_id"], "thread-original")

        item.write({"status": "planned", "current_codex_run_id": False})
        retry = worker_plan.codex_claim_next({"task_id": item.id, "worker_name": "one-thread-retry"})
        self.assertEqual(retry["task"]["codex_thread_id"], "thread-original")
        with self.assertRaises(AccessError):
            worker_plan.codex_update_run(
                {
                    "task_id": item.id,
                    "run_token": retry["run_token"],
                    "phase": "planning",
                    "codex_thread_id": "thread-duplicate",
                }
            )
        self.assertEqual(item.codex_thread_id, "thread-original")
        worker_plan.codex_update_run(
            {
                "task_id": item.id,
                "run_token": retry["run_token"],
                "phase": "planning",
                "codex_thread_id": "thread-original",
            }
        )

    def test_codex_claims_only_tasks_created_by_the_base_administrator(self):
        plan = self.env["saas.implementation.plan.item"]
        codex_group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        administrator_group = self.env.ref("arcigy_saas_control_center.group_saas_administrator")
        worker = self.env["res.users"].create(
            {
                "name": "Codex author filter worker",
                "login": "codex-author-filter@example.invalid",
                "group_ids": [(6, 0, [codex_group.id])],
            }
        )
        other_administrator = self.env["res.users"].create(
            {
                "name": "Other SaaS administrator",
                "login": "other-saas-administrator@example.invalid",
                "group_ids": [(6, 0, [administrator_group.id])],
            }
        )
        disallowed = plan.with_user(other_administrator).create(
            {"name": "Not founder-authored", "priority": "p1", "scope": "arcigy"}
        )
        allowed = self._administrator_plan().create({"name": "Founder-authored", "priority": "p1", "scope": "arcigy"})
        worker_plan = plan.with_user(worker)

        skipped = worker_plan.codex_claim_next({"task_id": disallowed.id, "worker_name": "author-filter"})
        self.assertFalse(skipped["task"])
        self.assertEqual(disallowed.status, "planned")

        claimed = worker_plan.codex_claim_next({"task_id": allowed.id, "worker_name": "author-filter"})
        self.assertEqual(claimed["task"]["id"], allowed.id)

    def test_non_administrator_threads_can_be_listed_and_detached_only(self):
        plan = self.env["saas.implementation.plan.item"]
        codex_group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        administrator_group = self.env.ref("arcigy_saas_control_center.group_saas_administrator")
        worker = self.env["res.users"].create(
            {
                "name": "Codex stale-thread worker",
                "login": "codex-stale-thread@example.invalid",
                "group_ids": [(6, 0, [codex_group.id])],
            }
        )
        other_administrator = self.env["res.users"].create(
            {
                "name": "Other stale-thread administrator",
                "login": "other-stale-thread-administrator@example.invalid",
                "group_ids": [(6, 0, [administrator_group.id])],
            }
        )
        disallowed = plan.with_user(other_administrator).create(
            {"name": "Stale non-founder chat", "priority": "p1", "scope": "arcigy"}
        )
        disallowed.write({"codex_thread_id": "thread-from-other-administrator"})
        allowed = self._administrator_plan().create({"name": "Protected founder chat", "priority": "p1", "scope": "arcigy"})
        allowed.write({"codex_thread_id": "founder-thread"})
        worker_plan = plan.with_user(worker)

        listed = worker_plan.codex_list_disallowed_threads({})
        self.assertIn(disallowed.id, [item["task_id"] for item in listed["threads"]])
        self.assertNotIn(allowed.id, [item["task_id"] for item in listed["threads"]])
        with self.assertRaises(AccessError):
            worker_plan.codex_discard_disallowed_threads({"task_ids": [allowed.id]})
        discarded = worker_plan.codex_discard_disallowed_threads({"task_ids": [disallowed.id]})
        self.assertEqual(discarded["discarded_task_ids"], [disallowed.id])
        self.assertFalse(disallowed.codex_thread_id)

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
        eligible = self._administrator_plan().create(
            {"name": "Administrator daily item", "priority": "p1", "scope": "arcigy"}
        )
        first = worker_plan.codex_claim_next(
            {"task_id": eligible.id, "worker_name": "daily", "daily_batch_date": "2026-08-04", "daily_limit": 1}
        )
        self.assertTrue(first["task"])
        self.assertEqual(plan.browse(first["task"]["id"]).daily_batch_slot, 1)
        second = worker_plan.codex_claim_next(
            {"worker_name": "daily", "daily_batch_date": "2026-08-04", "daily_limit": 1}
        )
        self.assertFalse(second["task"])
        self.assertEqual(second["reason"], "daily_limit_reached")

    def test_multiple_review_notes_are_batched_for_the_same_codex_thread(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {"name": "Codex review worker", "login": "codex-review@example.invalid", "group_ids": [(6, 0, [group.id])]}
        )
        item = self._administrator_plan().create(
            {
                "name": "Review continuation",
                "priority": "p1",
                "scope": "arcigy",
                "codex_thread_id": "thr_review_continuation",
                "status": "ready_for_review",
                "review_checklist": "Open the implementation plan and verify that the prepared result is available after reload.",
            }
        )
        item.message_post(body="First correction: retain the existing layout.", subtype_xmlid="mail.mt_note")
        item.message_post(body="Second correction: add the missing mobile check.", subtype_xmlid="mail.mt_note")
        self.assertEqual(item.status, "changes_requested")
        self.assertEqual(item.review_feedback_ids.mapped("sequence"), [1, 2])
        self.assertEqual(item.review_feedback_ids.mapped("state"), ["pending", "pending"])

        claimed = plan.with_user(worker).codex_claim_review_followup({"worker_name": "review-test"})
        self.assertEqual(claimed["task"]["id"], item.id)
        self.assertEqual(claimed["codex_thread_id"], "thr_review_continuation")
        self.assertEqual([note["sequence"] for note in claimed["review_notes"]], [1, 2])
        self.assertEqual(item.review_feedback_ids.mapped("state"), ["leased", "leased"])

        plan.with_user(worker).codex_mark_ready(
            {
                "task_id": item.id,
                "run_token": claimed["run_token"],
                "result_summary": "Both reviewer corrections were applied in the original Codex conversation.",
                "review_checklist": "Reload the task, verify both corrections, then check the recorded Codex run and its test evidence.",
            }
        )
        self.assertEqual(item.status, "ready_for_review")
        self.assertEqual(item.review_feedback_ids.mapped("state"), ["processed", "processed"])

    def test_p2_items_stay_out_of_new_and_review_followup_queues(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {"name": "Codex deferred worker", "login": "codex-deferred@example.invalid", "group_ids": [(6, 0, [group.id])]}
        )
        deferred = self._administrator_plan().create(
            {
                "name": "Deferred future work",
                "priority": "p2",
                "scope": "arcigy",
                "codex_thread_id": "thr_deferred",
                "status": "changes_requested",
                "review_checklist": "Keep this task out of the implementation queue until the founder changes its priority.",
            }
        )
        deferred.message_post(body="This remains future work.", subtype_xmlid="mail.mt_note")
        followup = plan.with_user(worker).codex_claim_review_followup({"worker_name": "deferred-test"})
        self.assertFalse(followup["task"])
        self.assertEqual(followup["reason"], "queue_empty")

    def test_codex_worker_splits_a_broad_task_into_ordered_review_slices(self):
        plan = self.env["saas.implementation.plan.item"]
        group = self.env.ref("arcigy_saas_control_center.group_saas_codex_worker")
        worker = self.env["res.users"].create(
            {"name": "Codex split worker", "login": "codex-split@example.invalid", "group_ids": [(6, 0, [group.id])]}
        )
        broad_task = self._administrator_plan().create(
            {"name": "Broad workflow", "priority": "p1", "scope": "arcigy", "full_prompt": "Deliver the complete long-running implementation workflow."}
        )
        worker_plan = plan.with_user(worker)
        claim = worker_plan.codex_claim_next({"task_id": broad_task.id, "worker_name": "test"})
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
