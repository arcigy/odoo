import re

from odoo import api, fields, models
from odoo.tools import html2plaintext

# Types the app sends since the client CRM; the legacy ones stay for history and are grouped.
NEW_KINDS = [("usability_error", "Niečo nefunguje"), ("price_error", "Chyba v cene"), ("wish", "Želanie")]
KIND_GROUPS = [
    ("price_error", "Chyba v cene"),
    ("usability_error", "Niečo nefunguje / použiteľnosť"),
    ("wish", "Želanie"),
    ("question", "Otázka"),
    ("other", "Iné"),
]
KIND_TO_GROUP = {
    "price_error": "price_error",
    "usability_error": "usability_error",
    "bug": "usability_error",
    "wish": "wish",
    "feature_request": "wish",
    "improvement": "wish",
    "question": "question",
    "other": "other",
}
OPEN_STATES = ("review", "new", "working")
# Only reports from production (or legacy ones without an environment) belong to a client.
CLIENT_ENVIRONMENTS = ("main", "unknown")


class ProjectTask(models.Model):
    _inherit = "project.task"

    arcigy_kind = fields.Selection(
        selection_add=NEW_KINDS,
        ondelete={value: "set default" for value, _label in NEW_KINDS},
    )
    arcigy_kind_group = fields.Selection(KIND_GROUPS, string="Typ hlásenia (CRM)", compute="_compute_arcigy_kind_group", store=True, index=True)
    arcigy_partner_id = fields.Many2one("res.partner", string="Klient", compute="_compute_arcigy_partner_id", store=True, index=True)

    @api.depends("arcigy_kind")
    def _compute_arcigy_kind_group(self):
        for task in self:
            task.arcigy_kind_group = KIND_TO_GROUP.get(task.arcigy_kind) if task.arcigy_report else False

    @api.depends("arcigy_tenant_id", "arcigy_environment", "arcigy_report")
    def _compute_arcigy_partner_id(self):
        keys = {task.arcigy_tenant_id for task in self if task.arcigy_tenant_id}
        partners = self.env["res.partner"].sudo().search([("arcigy_client_key", "in", list(keys))]) if keys else self.env["res.partner"]
        by_key = {partner.arcigy_client_key: partner for partner in partners}
        for task in self:
            belongs = task.arcigy_report and task.arcigy_environment in CLIENT_ENVIRONMENTS
            task.arcigy_partner_id = by_key.get(task.arcigy_tenant_id, False) if belongs else False

    def _arcigy_migrate_legacy(self, task_ids=None):
        """arcigy_feedback only knows its own report types; apply the new ones from `type=` in the report."""
        result = super()._arcigy_migrate_legacy(task_ids)
        domain = [("arcigy_report", "=", True), ("arcigy_kind", "=", "bug")]
        if task_ids is not None:
            domain.append(("id", "in", task_ids))
        domain += ["|"] * (len(NEW_KINDS) - 1) + [("description", "ilike", f"type={value}") for value, _label in NEW_KINDS]
        new_kinds = {value for value, _label in NEW_KINDS}
        for task in self.sudo().with_context(active_test=False).search(domain):
            match = re.search(r"^type=(\w+)\s*$", html2plaintext(task.description or ""), re.M)
            if match and match[1] in new_kinds:
                task.with_context(tracking_disable=True).write({"arcigy_kind": match[1]})
        return result
