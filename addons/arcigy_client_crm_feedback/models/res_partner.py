from odoo import api, fields, models

from .project_task import OPEN_STATES

PRICE_ERROR_DAYS_PARAM = "arcigy_client_crm.signal_price_error_open_days"


class ResPartner(models.Model):
    _inherit = "res.partner"

    arcigy_report_count = fields.Integer(string="Hlásenia", compute="_compute_arcigy_report_counts")
    arcigy_open_report_count = fields.Integer(string="Otvorené hlásenia", compute="_compute_arcigy_report_counts")
    arcigy_open_price_error_count = fields.Integer(string="Otvorené chyby v cene", compute="_compute_arcigy_report_counts")

    def _compute_arcigy_report_counts(self):
        Task = self.env["project.task"].sudo().with_context(active_test=False)
        totals = dict(Task._read_group([("arcigy_partner_id", "in", self.ids)], ["arcigy_partner_id"], ["__count"]))
        opened = dict(Task._read_group([("arcigy_partner_id", "in", self.ids), ("arcigy_status", "in", OPEN_STATES)], ["arcigy_partner_id"], ["__count"]))
        prices = dict(Task._read_group(
            [("arcigy_partner_id", "in", self.ids), ("arcigy_status", "in", OPEN_STATES), ("arcigy_kind_group", "=", "price_error")],
            ["arcigy_partner_id"], ["__count"]))
        for partner in self:
            partner.arcigy_report_count = totals.get(partner, 0)
            partner.arcigy_open_report_count = opened.get(partner, 0)
            partner.arcigy_open_price_error_count = prices.get(partner, 0)

    @api.model_create_multi
    def create(self, vals_list):
        partners = super().create(vals_list)
        partners.filtered("arcigy_client_key")._arcigy_relink_reports()
        return partners

    def write(self, vals):
        result = super().write(vals)
        if "arcigy_client_key" in vals:
            self._arcigy_relink_reports()
        return result

    def _arcigy_relink_reports(self):
        """Linking a company to its app tenant also links that tenant's past reports."""
        Task = self.env["project.task"].sudo().with_context(active_test=False)
        keys = [key for key in self.mapped("arcigy_client_key") if key]
        tasks = Task.search(["|", ("arcigy_tenant_id", "in", keys), ("arcigy_partner_id", "in", self.ids)])
        if tasks:
            self.env.add_to_compute(Task._fields["arcigy_partner_id"], tasks)
            tasks._recompute_recordset(["arcigy_partner_id"])

    def action_arcigy_open_reports(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("arcigy_client_crm_feedback.action_arcigy_client_reports")
        action["domain"] = [("arcigy_partner_id", "=", self.id)]
        return action

    def _arcigy_signal_findings(self):
        findings = super()._arcigy_signal_findings()
        days = int(float(self.env["ir.config_parameter"].sudo().get_param(PRICE_ERROR_DAYS_PARAM) or 3))
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.env["project.task"].sudo().search_count([
            ("arcigy_partner_id", "=", self.id), ("arcigy_kind_group", "=", "price_error"),
            ("arcigy_status", "in", OPEN_STATES), ("create_date", "<", limit)])
        if stale:
            findings.append(("Otvorená chyba v cene", f"{stale} hlásení typu Chyba v cene je otvorených dlhšie ako {days} dni. Chyby v cene podkopávajú dôveru klienta."))
        return findings
