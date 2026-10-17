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
