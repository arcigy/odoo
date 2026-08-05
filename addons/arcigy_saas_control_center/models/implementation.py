"""Controlled Codex access to the founder-owned implementation queue.

The JSON-2 methods in this file deliberately expose only a narrow task workflow.
They are not a generic Odoo RPC bridge and cannot create arbitrary business data.
"""

from uuid import uuid4

from odoo import api, fields, models
from odoo.exceptions import AccessError, ValidationError
from odoo.tools import html2plaintext


CODEX_GROUP = "arcigy_saas_control_center.group_saas_codex_worker"
ADMIN_GROUP = "arcigy_saas_control_center.group_saas_administrator"
ACTIVE_QUEUE_STATES = ("planned", "changes_requested")
WORKER_STATES = {"planning", "implementing", "testing", "deploying"}
RUN_PHASES = [
    ("planning", "Planning"),
    ("implementing", "Implementing"),
    ("testing", "Testing"),
    ("deploying", "Deploying"),
    ("ready", "Ready"),
    ("blocked", "Blocked"),
]


class SaasImplementationPlanRun(models.Model):
    _name = "saas.implementation.plan.run"
    _description = "Implementation plan Codex run"
    _order = "started_at desc, id desc"

    task_id = fields.Many2one(
        "saas.implementation.plan.item", required=True, ondelete="cascade", index=True
    )
    run_token = fields.Char(required=True, readonly=True, index=True)
    worker_name = fields.Char(required=True, default="codex")
    phase = fields.Selection(RUN_PHASES, required=True, default="planning", index=True)
    started_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    heartbeat_at = fields.Datetime(required=True, default=fields.Datetime.now, index=True)
    lease_expires_at = fields.Datetime(required=True, index=True)
    finished_at = fields.Datetime(index=True)
    codex_thread_id = fields.Char(index=True)
    plan_model = fields.Char()
    execute_model = fields.Char()
    branch_name = fields.Char()
    commit_sha = fields.Char()
    pull_request_url = fields.Char()
    ci_url = fields.Char()
    preview_url = fields.Char()
    deployment_url = fields.Char()
    test_summary = fields.Text()
    safe_error = fields.Text()

    _run_token_unique = models.Constraint("UNIQUE(run_token)", "Run token must be unique.")


class SaasImplementationPlanReviewFeedback(models.Model):
    """Immutable reviewer feedback captured from Odoo's internal-note chatter."""

    _name = "saas.implementation.plan.review.feedback"
    _description = "Implementation plan reviewer feedback"
    _order = "sequence, id"

    task_id = fields.Many2one(
        "saas.implementation.plan.item", required=True, ondelete="cascade", index=True
    )
    message_id = fields.Many2one("mail.message", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(required=True, index=True)
    body = fields.Text(required=True)
    state = fields.Selection(
        [("pending", "Pending"), ("leased", "Leased"), ("processed", "Processed")],
        required=True,
        default="pending",
        index=True,
    )
    claimed_by_run_id = fields.Many2one("saas.implementation.plan.run", ondelete="set null", index=True)

    _message_unique = models.Constraint("UNIQUE(message_id)", "A reviewer note can only be captured once.")


class SaasImplementationPlanItem(models.Model):
    _name = "saas.implementation.plan.item"
    _inherit = ["saas.implementation.plan.item", "mail.thread", "mail.activity.mixin"]

    status = fields.Selection(
        selection_add=[
            ("planning", "Planning"),
            ("testing", "Testing"),
            ("deploying", "Deploying"),
            ("awaiting_approval", "Awaiting approval"),
            ("changes_requested", "Changes requested"),
            ("split", "Split into reviewable slices"),
        ],
        ondelete={
            "planning": "set default",
            "testing": "set default",
            "deploying": "set default",
            "awaiting_approval": "set default",
            "changes_requested": "set default",
            "split": "set default",
        },
        tracking=True,
    )
    full_prompt = fields.Text(string="Celé zadanie", tracking=True)
    implementation_plan = fields.Text(string="Codex Implementation Plan", tracking=True)
    plan_version = fields.Integer(default=0, readonly=True)
    result_summary = fields.Text(string="Výsledok", tracking=True)
    followup_notes = fields.Text(string="Codex follow-upy")
    risk_level = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("approval", "Approval required")],
        default="medium",
        tracking=True,
    )
    target_repository = fields.Selection(
        [("kitchen", "Kitchen App"), ("odoo", "Odoo"), ("cross_system", "Cross-system")],
        default="cross_system",
        tracking=True,
    )
    target_environment = fields.Selection(
        [("develop", "Kitchen develop"), ("odoo_staging", "Odoo staging"), ("approval", "Manual approval")],
        default="approval",
        tracking=True,
    )
    daily_batch_date = fields.Date(index=True)
    daily_batch_slot = fields.Integer(index=True)
    codex_run_ids = fields.One2many("saas.implementation.plan.run", "task_id", string="Codex runs")
    current_codex_run_id = fields.Many2one("saas.implementation.plan.run", readonly=True, copy=False)
    codex_thread_id = fields.Char(readonly=True, copy=False, index=True)
    review_feedback_ids = fields.One2many(
        "saas.implementation.plan.review.feedback", "task_id", string="Reviewer feedback"
    )
    parent_id = fields.Many2one("saas.implementation.plan.item", ondelete="set null", index=True)
    child_ids = fields.One2many("saas.implementation.plan.item", "parent_id", string="Podúlohy")

    def _ensure_codex_access(self):
        if not (self.env.user.has_group(CODEX_GROUP) or self.env.user.has_group(ADMIN_GROUP)):
            raise AccessError("Only the Arcigy Codex worker can operate the implementation queue.")

    @api.model
    def _codex_administrator_creator(self):
        """Return the one founder account allowed to feed the Codex queue.

        This deliberately uses Odoo's stable administrator XML id rather than a
        display name or a role. A different SaaS administrator may administer
        Odoo, but only records created by this Administrator account may start
        or resume a Codex conversation.
        """
        return self.env.ref("base.user_admin", raise_if_not_found=False)

    @api.model
    def _codex_administrator_task_domain(self):
        administrator = self._codex_administrator_creator()
        # Fail closed if the expected founder account is unavailable.
        return [("id", "=", 0)] if not administrator else [("create_uid", "=", administrator.id)]

    @api.model
    def _ensure_codex_administrator_task(self, task):
        administrator = self._codex_administrator_creator()
        if not administrator or task.create_uid.id != administrator.id:
            raise AccessError("Only tasks created by Administrator may be processed by Codex.")

    def message_post(self, **kwargs):
        """Turn every confirmed internal review note into durable, ordered rework input.

        Only a human note posted while the task is Ready for review reopens the task.
        System messages and normal chatter updates remain informational and never requeue work.
        """
        feedback_before_post = self.filtered(
            lambda task: task.status in {"ready_for_review", "changes_requested"}
        )
        message = super().message_post(**kwargs)
        note_subtype = self.env.ref("mail.mt_note", raise_if_not_found=False)
        if not feedback_before_post or not note_subtype or message.subtype_id != note_subtype:
            return message
        body = html2plaintext(message.body or "").strip()
        if not body:
            return message
        feedback_model = self.env["saas.implementation.plan.review.feedback"].sudo()
        for task in feedback_before_post:
            next_sequence = (max(task.review_feedback_ids.mapped("sequence")) if task.review_feedback_ids else 0) + 1
            feedback_model.create(
                {
                    "task_id": task.id,
                    "message_id": message.id,
                    "sequence": next_sequence,
                    "body": body,
                }
            )
            task.write({"status": "changes_requested"})
        return message

    def unlink(self):
        """Delete selected plan items without leaving an ambiguous queue gap.

        Odoo's normal administrator delete permission remains the authority for
        this operation.  Runs and captured feedback belonging to the deleted
        item follow their existing cascade rules; surviving tasks are compacted
        atomically to positions 1..N.
        """
        self._codex_lock_queue()
        result = super().unlink()
        self._normalize_sequence()
        return result

    @api.model
    def _codex_payload(self, record):
        return {
            "id": record.id,
            "position": record.sequence,
            "name": record.name,
            "scope": record.scope,
            "target_repository": record.target_repository,
            "target_environment": record.target_environment,
            "status": record.status,
            "codex_thread_id": record.codex_thread_id or "",
            "prompt": record.full_prompt or record.next_action or "",
            "acceptance_criteria": record.acceptance_criteria or "",
            "plan": record.implementation_plan or "",
            "plan_model": record.current_codex_run_id.plan_model or "",
            "risk_level": record.risk_level,
        }

    @api.model
    def _codex_lock_queue(self):
        self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ["arcigy-codex-implementation-queue"])

    @api.model
    def _codex_require_payload(self, payload):
        if not isinstance(payload, dict):
            raise ValidationError("payload must be an object.")
        return payload

    @api.model
    def _codex_task_from_payload(self, payload):
        task_id = payload.get("task_id")
        if not isinstance(task_id, int) or task_id <= 0:
            raise ValidationError("task_id must be a positive integer.")
        task = self.browse(task_id).exists()
        if not task:
            raise ValidationError("Implementation task was not found.")
        return task

    @api.model
    def _codex_run_from_payload(self, payload):
        task = self._codex_task_from_payload(payload)
        self._ensure_codex_administrator_task(task)
        token = str(payload.get("run_token") or "").strip()
        if not token:
            raise ValidationError("run_token is required.")
        run = self.env["saas.implementation.plan.run"].search(
            [("task_id", "=", task.id), ("run_token", "=", token)], limit=1
        )
        if not run:
            raise AccessError("The run token does not own this implementation task.")
        return task, run

    @api.model
    def codex_claim_next(self, payload):
        """Atomically claim one eligible task by ID, queue position, or first position."""
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        worker_name = str(payload.get("worker_name") or "codex").strip()[:120]
        requested_position = payload.get("position")
        requested_task_id = payload.get("task_id")
        expected_name = payload.get("expected_name")
        if requested_position not in (None, False):
            if not isinstance(requested_position, int) or requested_position < 1:
                raise ValidationError("position must be a positive integer.")
        if requested_task_id not in (None, False):
            if not isinstance(requested_task_id, int) or requested_task_id < 1:
                raise ValidationError("task_id must be a positive integer.")
        if requested_position and requested_task_id:
            raise ValidationError("Specify either position or task_id, not both.")
        if expected_name not in (None, False) and not isinstance(expected_name, str):
            raise ValidationError("expected_name must be a string.")
        lease_minutes = payload.get("lease_minutes", 90)
        if not isinstance(lease_minutes, int) or lease_minutes < 15 or lease_minutes > 240:
            raise ValidationError("lease_minutes must be between 15 and 240.")
        batch_date = payload.get("daily_batch_date")
        daily_limit = payload.get("daily_limit")
        if batch_date:
            try:
                batch_date = fields.Date.to_date(batch_date)
            except (TypeError, ValueError) as error:
                raise ValidationError("daily_batch_date must be an ISO date.") from error
            if not isinstance(daily_limit, int) or not 1 <= daily_limit <= 20:
                raise ValidationError("daily_limit must be between 1 and 20 when daily_batch_date is set.")
        elif daily_limit not in (None, False):
            raise ValidationError("daily_limit requires daily_batch_date.")
        self._codex_lock_queue()
        daily_slot = False
        if batch_date:
            used_slots = self.search_count([("daily_batch_date", "=", batch_date)])
            if used_slots >= daily_limit:
                return {"task": False, "reason": "daily_limit_reached", "daily_limit": daily_limit}
            daily_slot = used_slots + 1
        candidates = self.search(
            [
                ("status", "in", ACTIVE_QUEUE_STATES),
                ("priority", "!=", "p2"),
                *self._codex_administrator_task_domain(),
            ],
            order="sequence, id",
        )
        if requested_task_id:
            task = candidates.filtered(lambda candidate: candidate.id == requested_task_id)
        elif requested_position:
            task = candidates[requested_position - 1:requested_position]
        else:
            task = candidates[:1]
        if not task:
            return {"task": False, "reason": "queue_empty"}
        if expected_name and task.name != expected_name:
            raise ValidationError("The requested task ID does not match the expected task name.")
        now = fields.Datetime.now()
        token = uuid4().hex
        run = self.env["saas.implementation.plan.run"].create(
            {
                "task_id": task.id,
                "run_token": token,
                "worker_name": worker_name,
                "lease_expires_at": fields.Datetime.add(now, minutes=lease_minutes),
            }
        )
        task.write(
            {
                "status": "planning",
                "current_codex_run_id": run.id,
                "daily_batch_date": batch_date or task.daily_batch_date,
                "daily_batch_slot": daily_slot or task.daily_batch_slot,
            }
        )
        return {"task": self._codex_payload(task), "run_token": token, "run_id": run.id}

    @api.model
    def codex_save_plan(self, payload):
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task, run = self._codex_run_from_payload(payload)
        plan = str(payload.get("plan") or "").strip()
        if len(plan) < 40 or len(plan) > 50000:
            raise ValidationError("plan must contain between 40 and 50000 characters.")
        risk = payload.get("risk_level", "medium")
        if risk not in {"low", "medium", "high", "approval"}:
            raise ValidationError("Unsupported risk_level.")
        repository = payload.get("target_repository", "cross_system")
        environment = payload.get("target_environment", "approval")
        if repository not in {"kitchen", "odoo", "cross_system"}:
            raise ValidationError("Unsupported target_repository.")
        if environment not in {"develop", "odoo_staging", "approval"}:
            raise ValidationError("Unsupported target_environment.")
        approval_required = risk == "approval" or environment == "approval"
        task.write(
            {
                "implementation_plan": plan,
                "plan_version": task.plan_version + 1,
                "risk_level": risk,
                "target_repository": repository,
                "target_environment": environment,
                "status": "awaiting_approval" if approval_required else "in_progress",
            }
        )
        run.write(
            {
                "phase": "ready",
                "finished_at": fields.Datetime.now(),
                "heartbeat_at": fields.Datetime.now(),
                "plan_model": str(payload.get("model") or "")[:120],
                "test_summary": "Implementation plan saved and handed off to the execution task.",
            }
        )
        return {"task_id": task.id, "status": task.status, "approval_required": approval_required}

    @api.model
    def codex_claim_execution(self, payload):
        """Create a fresh execution lease after a separate planning task approved the plan."""
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task = self._codex_task_from_payload(payload)
        self._ensure_codex_administrator_task(task)
        worker_name = str(payload.get("worker_name") or "codex-executor").strip()[:120]
        lease_minutes = payload.get("lease_minutes", 180)
        if not isinstance(lease_minutes, int) or lease_minutes < 15 or lease_minutes > 240:
            raise ValidationError("lease_minutes must be between 15 and 240.")
        self._codex_lock_queue()
        task.invalidate_recordset()
        if task.status != "in_progress" or not (task.implementation_plan or "").strip():
            raise ValidationError("Only a planned, non-approved implementation task can be claimed for execution.")
        current_run = task.current_codex_run_id
        if current_run and current_run.phase not in {"ready", "blocked"}:
            raise AccessError("The planning task has not released this implementation item yet.")
        now = fields.Datetime.now()
        token = uuid4().hex
        run = self.env["saas.implementation.plan.run"].create(
            {
                "task_id": task.id,
                "run_token": token,
                "worker_name": worker_name,
                "phase": "implementing",
                "lease_expires_at": fields.Datetime.add(now, minutes=lease_minutes),
            }
        )
        task.write({"current_codex_run_id": run.id})
        return {"task": self._codex_payload(task), "run_token": token, "run_id": run.id}

    @api.model
    def codex_claim_review_followup(self, payload):
        """Lease every currently pending reviewer note for one existing Codex thread.

        Notes are leased as one ordered batch. A later note never overwrites an
        earlier one: it stays pending for the next continuation turn.
        """
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        worker_name = str(payload.get("worker_name") or "codex-review").strip()[:120]
        lease_minutes = payload.get("lease_minutes", 180)
        if not isinstance(lease_minutes, int) or lease_minutes < 15 or lease_minutes > 240:
            raise ValidationError("lease_minutes must be between 15 and 240.")
        self._codex_lock_queue()
        candidates = self.search(
            [
                ("status", "=", "changes_requested"),
                ("priority", "!=", "p2"),
                ("codex_thread_id", "!=", False),
                ("review_feedback_ids.state", "=", "pending"),
                *self._codex_administrator_task_domain(),
            ],
            order="sequence, id",
        )
        task = candidates[:1]
        if not task:
            return {"task": False, "reason": "queue_empty"}
        notes = task.review_feedback_ids.filtered(lambda note: note.state == "pending").sorted(
            key=lambda note: (note.sequence, note.id)
        )
        now = fields.Datetime.now()
        token = uuid4().hex
        run = self.env["saas.implementation.plan.run"].create(
            {
                "task_id": task.id,
                "run_token": token,
                "worker_name": worker_name,
                "phase": "implementing",
                "lease_expires_at": fields.Datetime.add(now, minutes=lease_minutes),
                "codex_thread_id": task.codex_thread_id,
            }
        )
        notes.write({"state": "leased", "claimed_by_run_id": run.id})
        task.write({"status": "in_progress", "current_codex_run_id": run.id})
        return {
            "task": self._codex_payload(task),
            "run_token": token,
            "run_id": run.id,
            "codex_thread_id": task.codex_thread_id,
            "review_notes": [{"id": note.id, "sequence": note.sequence, "body": note.body} for note in notes],
        }

    @api.model
    def codex_list_disallowed_threads(self, payload):
        """List only existing Codex chats created from non-Administrator tasks."""
        self._ensure_codex_access()
        self._codex_require_payload(payload)
        administrator = self._codex_administrator_creator()
        domain = [("codex_thread_id", "!=", False)]
        if administrator:
            domain.append(("create_uid", "!=", administrator.id))
        tasks = self.search(domain, order="sequence, id")
        return {
            "threads": [
                {
                    "task_id": task.id,
                    "thread_id": task.codex_thread_id,
                    "status": task.status,
                    "created_by": task.create_uid.name,
                }
                for task in tasks
            ]
        }

    @api.model
    def codex_discard_disallowed_threads(self, payload):
        """Clear stale pointers after their non-Administrator Codex chats are deleted."""
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task_ids = payload.get("task_ids")
        if not isinstance(task_ids, list) or not task_ids or len(task_ids) > 100:
            raise ValidationError("task_ids must contain between 1 and 100 task IDs.")
        if any(not isinstance(task_id, int) or task_id <= 0 for task_id in task_ids):
            raise ValidationError("task_ids must contain only positive integers.")
        self._codex_lock_queue()
        tasks = self.browse(task_ids).exists()
        if len(tasks) != len(set(task_ids)):
            raise ValidationError("One or more implementation tasks were not found.")
        administrator = self._codex_administrator_creator()
        if administrator and any(task.create_uid.id == administrator.id for task in tasks):
            raise AccessError("Administrator-created tasks must never be detached from Codex.")
        active_runs = self.env["saas.implementation.plan.run"].search(
            [("task_id", "in", tasks.ids), ("phase", "in", ["planning", "implementing", "testing", "deploying"])]
        )
        now = fields.Datetime.now()
        active_runs.write(
            {
                "phase": "blocked",
                "finished_at": now,
                "heartbeat_at": now,
                "safe_error": "Stopped because the task was not created by Administrator.",
            }
        )
        active_tasks = tasks.filtered(lambda task: task.status in {"planning", "in_progress", "testing", "deploying"})
        active_tasks.write({"status": "planned"})
        tasks.write({"codex_thread_id": False, "current_codex_run_id": False})
        return {"discarded_task_ids": tasks.ids}

    @api.model
    def codex_update_run(self, payload):
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task, run = self._codex_run_from_payload(payload)
        phase = payload.get("phase")
        if phase not in {item[0] for item in RUN_PHASES}:
            raise ValidationError("Unsupported run phase.")
        values = {"phase": phase, "heartbeat_at": fields.Datetime.now()}
        for key, limit in {
            "codex_thread_id": 255, "branch_name": 255, "commit_sha": 255,
            "pull_request_url": 2048, "ci_url": 2048, "preview_url": 2048,
            "deployment_url": 2048, "plan_model": 120, "execute_model": 120,
        }.items():
            if key in payload:
                values[key] = str(payload.get(key) or "").strip()[:limit]
        if "test_summary" in payload:
            values["test_summary"] = str(payload.get("test_summary") or "")[:20000]
        requested_thread_id = values.get("codex_thread_id")
        if requested_thread_id and task.codex_thread_id and task.codex_thread_id != requested_thread_id:
            raise AccessError("This implementation task already belongs to a different Codex thread.")
        run.write(values)
        if values.get("codex_thread_id"):
            task.write({"codex_thread_id": values["codex_thread_id"]})
        task_status_by_phase = {
            "planning": "planning",
            "implementing": "in_progress",
            "testing": "testing",
            "deploying": "deploying",
            "blocked": "blocked",
        }
        if phase in task_status_by_phase:
            task.write({"status": task_status_by_phase[phase]})
        return {"task_id": task.id, "status": task.status, "phase": run.phase}

    @api.model
    def codex_mark_ready(self, payload):
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task, run = self._codex_run_from_payload(payload)
        checklist = str(payload.get("review_checklist") or "").strip()
        summary = str(payload.get("result_summary") or "").strip()
        if len(checklist) < 30 or len(summary) < 20:
            raise ValidationError("Ready tasks require a precise checklist and result summary.")
        run.write({"phase": "ready", "finished_at": fields.Datetime.now(), "heartbeat_at": fields.Datetime.now(), "test_summary": str(payload.get("test_summary") or "")[:20000]})
        task.write({"status": "ready_for_review", "review_checklist": checklist, "result_summary": summary})
        self.env["saas.implementation.plan.review.feedback"].search(
            [("claimed_by_run_id", "=", run.id), ("state", "=", "leased")]
        ).write({"state": "processed"})
        if task.owner_id:
            task.activity_schedule(
                "mail.mail_activity_data_todo",
                user_id=task.owner_id.id,
                summary="Skontrolovať Codex úlohu",
                note=checklist,
            )
        return {"task_id": task.id, "status": task.status}

    @api.model
    def codex_block(self, payload):
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task, run = self._codex_run_from_payload(payload)
        reason = str(payload.get("reason") or "").strip()
        if len(reason) < 10:
            raise ValidationError("Blocked tasks require a concrete reason.")
        run.write({"phase": "blocked", "finished_at": fields.Datetime.now(), "safe_error": reason[:20000]})
        task.write({"status": "blocked", "blocker": reason[:20000]})
        if task.owner_id:
            task.activity_schedule("mail.mail_activity_data_todo", user_id=task.owner_id.id, summary="Rozhodnutie potrebné", note=reason)
        return {"task_id": task.id, "status": task.status}

    @api.model
    def codex_add_followup(self, payload):
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task = self._codex_task_from_payload(payload)
        note = str(payload.get("note") or "").strip()
        if len(note) < 3 or len(note) > 20000:
            raise ValidationError("note must contain between 3 and 20000 characters.")
        entry = f"[{fields.Datetime.now()}] {note}"
        task.write({"followup_notes": f"{task.followup_notes}\n{entry}".strip()})
        return {"task_id": task.id, "status": task.status}

    @api.model
    def codex_split(self, payload):
        """Split an already claimed broad task into bounded, ordered review slices."""
        self._ensure_codex_access()
        payload = self._codex_require_payload(payload)
        task, run = self._codex_run_from_payload(payload)
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not 2 <= len(raw_items) <= 10:
            raise ValidationError("items must contain between 2 and 10 reviewable slices.")
        prepared_items = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise ValidationError("Every split item must be an object.")
            name = str(raw_item.get("name") or "").strip()
            prompt = str(raw_item.get("prompt") or "").strip()
            criteria = str(raw_item.get("acceptance_criteria") or "").strip()
            if not 5 <= len(name) <= 255 or not 20 <= len(prompt) <= 20000:
                raise ValidationError("Each split item needs a short name and a concrete prompt.")
            prepared_items.append({"name": name, "full_prompt": prompt, "acceptance_criteria": criteria})
        self._codex_lock_queue()
        child_values_base = {
            "priority": task.priority,
            "scope": task.scope,
            "owner_id": task.owner_id.id,
            "source_document": task.source_document,
            "parent_id": task.id,
            "target_repository": task.target_repository,
            "target_environment": task.target_environment,
            "risk_level": task.risk_level,
        }
        original_position = task.sequence
        created = self.browse()
        for offset, item_values in enumerate(prepared_items):
            created |= self.sudo().create_at_position(
                {**child_values_base, **item_values}, original_position + offset
            )
        run.write(
            {
                "phase": "ready",
                "finished_at": fields.Datetime.now(),
                "heartbeat_at": fields.Datetime.now(),
                "test_summary": "Task split into bounded reviewable slices.",
            }
        )
        task.write(
            {
                "status": "split",
                "result_summary": "The original task was split into reviewable slices; each child keeps its own Codex run and review gate.",
            }
        )
        return {"task_id": task.id, "status": task.status, "children": [self._codex_payload(item) for item in created]}

    def action_mark_done(self):
        for task in self:
            task.write({"status": "done"})
            task.activity_feedback(feedback="Marked done by reviewer.")
        return True

    def action_request_changes(self):
        for task in self:
            task.write({"status": "changes_requested"})
        return True

    def action_retry_automation(self):
        """Return a transiently blocked automation item to the normal queue."""
        for task in self:
            if task.status != "blocked":
                raise ValidationError("Only blocked implementation tasks can be retried.")
            task.write({"status": "planned", "blocker": False})
        return True
