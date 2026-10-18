import re
from collections import defaultdict

from odoo import api, fields, models
from odoo.exceptions import ValidationError

CLIENT_STAGES = [
    ("pilot", "Pilot"),
    ("mvp", "MVP"),
    ("paying", "Platiaci"),
    ("paused", "Pozastavený"),
    ("churned", "Odišiel"),
]
# organization_id of a tenant in the Arcigy app, e.g. "client_delfi".
CLIENT_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
APP_ROLES = [("administrator", "Administrátor"), ("team_member", "Člen tímu"), ("observer", "Pozorovateľ")]
APP_USER_STATES = [("active_30d", "Aktívny (30 dní)"), ("inactive", "Neaktívny"), ("deactivated", "Deaktivovaný")]
MANAGER_GROUP = "arcigy_client_crm.group_crm_manager"
ACCOUNTING_GROUPS = "account.group_account_invoice,account.group_account_readonly"


class ResPartner(models.Model):
    _inherit = "res.partner"

    arcigy_client_key = fields.Char(
        string="Kľúč klienta v aplikácii",
        index=True,
        copy=False,
        tracking=True,
        help="organization_id klienta v produkčnej aplikácii Arcigy (napr. client_delfi). "
        "Podľa neho sa k firme priraďujú denné exporty a hlásenia.",
    )
    arcigy_client_stage = fields.Selection(CLIENT_STAGES, string="Štádium klienta", tracking=True)
    arcigy_mvp_start_date = fields.Date(string="Začiatok MVP", tracking=True)
    arcigy_billing_start_date = fields.Date(
        string="Začiatok platby",
        tracking=True,
        help="Prvý deň, za ktorý klient platí po skončení MVP.",
    )
    arcigy_mrr = fields.Monetary(
        string="MRR",
        currency_field="arcigy_mrr_currency_id",
        tracking=True,
        help="Dohodnutý mesačný opakovaný príjem od klienta. História zmien je v chatteri.",
    )
    arcigy_mrr_currency_id = fields.Many2one(
        "res.currency",
        string="Mena MRR",
        default=lambda self: self.env.company.currency_id,
    )

    # Receivables are derived from posted customer invoices, never typed in by hand.
    # Amounts are in the company currency, like account's own `credit` field.
    arcigy_amount_due = fields.Monetary(
        string="Neuhradené",
        compute="_compute_arcigy_receivables",
        currency_field="currency_id",
        groups=ACCOUNTING_GROUPS,
    )
    arcigy_amount_overdue = fields.Monetary(
        string="Po splatnosti",
        compute="_compute_arcigy_receivables",
        currency_field="currency_id",
        groups=ACCOUNTING_GROUPS,
    )
    arcigy_oldest_overdue_days = fields.Integer(
        string="Dni po splatnosti",
        compute="_compute_arcigy_receivables",
        groups=ACCOUNTING_GROUPS,
        help="Počet dní od splatnosti najstaršej neuhradenej faktúry.",
    )
    arcigy_next_due_date = fields.Date(
        string="Najbližšia splatnosť",
        compute="_compute_arcigy_receivables",
        groups=ACCOUNTING_GROUPS,
    )
    arcigy_last_payment_date = fields.Date(
        string="Posledná platba",
        compute="_compute_arcigy_receivables",
        groups=ACCOUNTING_GROUPS,
    )

    # App users of a client, kept as contacts of the client company (filled by the daily export).
    arcigy_user_key = fields.Char(string="Kľúč používateľa v aplikácii", index=True, copy=False, readonly=True)
    arcigy_app_role = fields.Selection(APP_ROLES, string="Rola v aplikácii", readonly=True, copy=False)
    arcigy_app_user_state = fields.Selection(APP_USER_STATES, string="Stav v aplikácii", readonly=True, groups=MANAGER_GROUP, copy=False)
    arcigy_user_monthly_ids = fields.One2many("arcigy.client.user.monthly", "contact_id", string="Výsledky po mesiacoch", groups=MANAGER_GROUP, copy=False)

    # Client summary, refreshed after every ingested batch.
    arcigy_metric_daily_ids = fields.One2many("arcigy.client.metric.daily", "partner_id", string="Denné metriky", copy=False)
    arcigy_metric_monthly_ids = fields.One2many("arcigy.client.metric.monthly", "partner_id", string="Mesačné metriky", copy=False)
    arcigy_data_as_of = fields.Date(string="Dáta k", readonly=True, copy=False)
    arcigy_users_total = fields.Integer(string="Používatelia", readonly=True, copy=False)
    arcigy_users_active_30d = fields.Integer(string="Aktívni (30 dní)", readonly=True, copy=False)
    arcigy_offers_generated_4w = fields.Integer(string="Vygenerované ponuky (4 týždne)", readonly=True, copy=False)
    arcigy_offers_sent_4w = fields.Integer(string="Odoslané ponuky (4 týždne)", readonly=True, copy=False)
    arcigy_offers_sent_prev_4w_avg = fields.Float(string="Odoslané ponuky, predchádzajúcich 8 týž. / 2", digits=(10, 1), readonly=True, copy=False)
    arcigy_offers_trend_pct = fields.Float(string="Trend odoslaných ponúk (%)", digits=(10, 1), readonly=True, copy=False)
    arcigy_ai_cost_per_project_usd = fields.Float(string="AI na projekt (USD, mesiac)", digits=(12, 4), readonly=True, copy=False)
    arcigy_ai_cost_p90_usd = fields.Float(string="AI na projekt p90 (USD, mesiac)", digits=(12, 4), readonly=True, copy=False)
    arcigy_pdf_corrected_pct = fields.Float(string="% opravených návrhov PDF (mesiac)", digits=(5, 1), readonly=True, copy=False)
    arcigy_margin_last_month = fields.Monetary(string="Príspevková marža (posledný mesiac)", currency_field="currency_id", readonly=True, copy=False)
    arcigy_app_user_count = fields.Integer(string="Používatelia aplikácie", compute="_compute_arcigy_app_user_count")

    _arcigy_user_key_unique = models.Constraint("UNIQUE(arcigy_user_key)", "Kľúč používateľa musí byť jedinečný.")

    _arcigy_client_key_unique = models.Constraint(
        "UNIQUE(arcigy_client_key)", "Kľúč klienta musí byť jedinečný."
    )

    @api.constrains("arcigy_client_key", "is_company")
    def _check_arcigy_client_key(self):
        for partner in self.filtered("arcigy_client_key"):
            if not partner.is_company:
                raise ValidationError("Kľúč klienta sa dá nastaviť iba na firme, nie na osobe.")
            if not CLIENT_KEY.match(partner.arcigy_client_key):
                raise ValidationError(
                    "Kľúč klienta musí byť organization_id z aplikácie: malé písmená, číslice, "
                    "„_“ alebo „-“, 2 až 64 znakov (napr. client_delfi)."
                )

    @api.depends_context("company")
    def _compute_arcigy_receivables(self):
        today = fields.Date.context_today(self)
        company = self.env.company
        commercial_ids = self.commercial_partner_id.ids
        due = defaultdict(float)
        overdue = defaultdict(float)
        oldest_overdue = defaultdict(int)
        next_due = {}
        last_payment = {}
        if commercial_ids:
            # Filter on payment_state, not only on the residual: since Odoo 18 a payment whose
            # method has no outstanding account creates no journal entry, so the invoice is
            # marked paid while its residual stays untouched.
            invoices = self.env["account.move"].search(
                [
                    ("commercial_partner_id", "in", commercial_ids),
                    ("company_id", "=", company.id),
                    ("move_type", "in", ("out_invoice", "out_refund")),
                    ("state", "=", "posted"),
                    ("payment_state", "in", ("not_paid", "partial")),
                    ("amount_residual", "!=", 0),
                ]
            )
            for move in invoices:
                key = move.commercial_partner_id.id
                due[key] += move.amount_residual_signed
                due_date = move.invoice_date_due
                if move.move_type != "out_invoice" or not due_date:
                    continue
                if due_date < today:
                    overdue[key] += move.amount_residual_signed
                    oldest_overdue[key] = max(oldest_overdue[key], (today - due_date).days)
                elif key not in next_due or due_date < next_due[key]:
                    next_due[key] = due_date
            # account.payment covers payments with and without a journal entry.
            payments = self.env["account.payment"].search(
                [
                    ("partner_id", "child_of", commercial_ids),
                    ("company_id", "=", company.id),
                    ("partner_type", "=", "customer"),
                    ("payment_type", "=", "inbound"),
                    ("state", "in", ("in_process", "paid")),
                ]
            )
            for payment in payments:
                key = payment.partner_id.commercial_partner_id.id
                if key not in last_payment or payment.date > last_payment[key]:
                    last_payment[key] = payment.date
        for partner in self:
            key = partner.commercial_partner_id.id
            partner.arcigy_amount_due = due[key]
            partner.arcigy_amount_overdue = overdue[key]
            partner.arcigy_oldest_overdue_days = oldest_overdue[key]
            partner.arcigy_next_due_date = next_due.get(key)
            partner.arcigy_last_payment_date = last_payment.get(key)

    def _compute_arcigy_app_user_count(self):
        counts = dict(self.env["res.partner"]._read_group(
            [("parent_id", "in", self.ids), ("arcigy_user_key", "!=", False)], ["parent_id"], ["__count"]))
        for partner in self:
            partner.arcigy_app_user_count = counts.get(partner, 0)

    def _arcigy_refresh_metric_summary(self):
        """Headline numbers for the client list and the signals, from the stored daily/monthly rows."""
        Daily = self.env["arcigy.client.metric.daily"]
        Monthly = self.env["arcigy.client.metric.monthly"]
        for partner in self:
            latest = Daily.search([("partner_id", "=", partner.id)], order="date desc", limit=1)
            if not latest:
                continue
            as_of = latest.date

            def total(name, days_back_from, days_back_to):
                rows = Daily.search([
                    ("partner_id", "=", partner.id),
                    ("date", ">", fields.Date.subtract(as_of, days=days_back_from)),
                    ("date", "<=", fields.Date.subtract(as_of, days=days_back_to)),
                ])
                return sum(rows.mapped(name))

            sent_4w = total("offers_sent", 28, 0)
            prev_avg = total("offers_sent", 84, 28) / 2.0
            month = Monthly.search([("partner_id", "=", partner.id), ("month", "<=", as_of)], order="month desc", limit=1)
            last_full = Monthly.search(
                [("partner_id", "=", partner.id), ("month", "<", fields.Date.start_of(as_of, "month"))], order="month desc", limit=1)
            partner.write({
                "arcigy_data_as_of": as_of,
                "arcigy_users_total": latest.users_total,
                "arcigy_users_active_30d": latest.users_active_30d,
                "arcigy_offers_generated_4w": total("offers_generated", 28, 0),
                "arcigy_offers_sent_4w": sent_4w,
                "arcigy_offers_sent_prev_4w_avg": prev_avg,
                "arcigy_offers_trend_pct": round(100.0 * (sent_4w - prev_avg) / prev_avg, 1) if prev_avg else 0.0,
                "arcigy_ai_cost_per_project_usd": month.ai_cost_per_project_avg_usd,
                "arcigy_ai_cost_p90_usd": month.ai_cost_per_project_p90_usd,
                "arcigy_pdf_corrected_pct": month.pdf_corrected_pct,
                "arcigy_margin_last_month": last_full.contribution_margin if last_full else month.contribution_margin,
            })

    def action_arcigy_open_metrics(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("arcigy_client_crm.action_arcigy_client_metric_daily")
        action["domain"] = [("partner_id", "=", self.id)]
        action["context"] = {"default_partner_id": self.id}
        return action

    def action_arcigy_open_app_users(self):
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id("arcigy_client_crm.action_arcigy_app_users")
        action["domain"] = [("parent_id", "=", self.id), ("arcigy_user_key", "!=", False)]
        return action
