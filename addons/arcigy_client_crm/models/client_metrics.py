from odoo import api, fields, models

# Integer counters that the daily export sends per client and day (see arcigy_ingest).
DAILY_COUNTERS = (
    "projects_created",
    "projects_deleted",
    "offers_generated",
    "offers_sheet_generated",
    "offers_sent",
    "pdf_documents_processed",
    "pdf_items_proposed",
    "pdf_items_corrected",
    "pdf_items_rejected",
    "pdf_items_added_by_user",
    "ai_calls",
    "ai_calls_unpriced",
)
DAILY_SNAPSHOTS = ("users_total", "users_active_30d")
DAILY_COSTS = ("ai_cost_usd_pdf", "ai_cost_usd_assistant", "ai_cost_usd_other")
MONTHLY_FIELDS = (
    "projects_with_ai",
    "ai_cost_per_project_avg_usd",
    "ai_cost_per_project_p90_usd",
    "ai_cost_per_project_max_usd",
    "projects_over_target",
    "ai_cost_usd_without_project",
)
USER_MONTHLY_COUNTERS = (
    "projects_created",
    "offers_generated",
    "offers_sent",
    "pdf_documents_processed",
    "pdf_items_proposed",
    "pdf_items_corrected",
)


class ArcigyClientMetricDaily(models.Model):
    _name = "arcigy.client.metric.daily"
    _description = "Arcigy client metrics per day"
    _order = "date desc, partner_id"
    _rec_name = "date"

    partner_id = fields.Many2one("res.partner", string="Klient", required=True, index=True, ondelete="cascade")
    date = fields.Date(string="Deň", required=True, index=True)
    batch_generated_at = fields.Datetime(string="Dávka z", readonly=True)

    users_total = fields.Integer(string="Používatelia", aggregator="max")
    users_active_30d = fields.Integer(string="Aktívni (30 dní)", aggregator="max")
    projects_created = fields.Integer(string="Nové projekty")
    projects_deleted = fields.Integer(string="Zmazané projekty")
    offers_generated = fields.Integer(string="Vygenerované ponuky (PDF)")
    offers_sheet_generated = fields.Integer(string="Vygenerované kusovníky (XLSX)")
    offers_sent = fields.Integer(string="Odoslané ponuky")
    pdf_documents_processed = fields.Integer(string="Spracované PDF")
    pdf_items_proposed = fields.Integer(string="Návrhy z PDF")
    pdf_items_corrected = fields.Integer(string="Opravené návrhy")
    pdf_items_rejected = fields.Integer(string="Zamietnuté návrhy")
    pdf_items_added_by_user = fields.Integer(string="Doplnené používateľom")
    ai_cost_usd_pdf = fields.Float(string="AI PDF (USD)", digits=(12, 6))
    ai_cost_usd_assistant = fields.Float(string="AI asistent (USD)", digits=(12, 6))
    ai_cost_usd_other = fields.Float(string="AI iné (USD)", digits=(12, 6))
    ai_cost_usd_total = fields.Float(string="AI spolu (USD)", digits=(12, 6), compute="_compute_ai_cost_usd_total", store=True)
    ai_calls = fields.Integer(string="AI volania")
    ai_calls_unpriced = fields.Integer(string="AI volania bez ceny")

    _partner_date_unique = models.Constraint("UNIQUE(partner_id, date)", "Jeden deň klienta môže mať iba jeden riadok metrík.")

    @api.depends("ai_cost_usd_pdf", "ai_cost_usd_assistant", "ai_cost_usd_other")
    def _compute_ai_cost_usd_total(self):
        for row in self:
            row.ai_cost_usd_total = row.ai_cost_usd_pdf + row.ai_cost_usd_assistant + row.ai_cost_usd_other


class ArcigyClientMetricMonthly(models.Model):
    _name = "arcigy.client.metric.monthly"
    _description = "Arcigy client metrics per month"
    _order = "month desc, partner_id"
    _rec_name = "month"

    partner_id = fields.Many2one("res.partner", string="Klient", required=True, index=True, ondelete="cascade")
    month = fields.Date(string="Mesiac", required=True, index=True, help="Prvý deň mesiaca.")
    batch_generated_at = fields.Datetime(string="Dávka z", readonly=True)

    projects_with_ai = fields.Integer(string="Projekty s AI")
    ai_cost_per_project_avg_usd = fields.Float(string="AI na projekt, priemer (USD)", digits=(12, 6), aggregator="avg")
    ai_cost_per_project_p90_usd = fields.Float(string="AI na projekt, p90 (USD)", digits=(12, 6), aggregator="max")
    ai_cost_per_project_max_usd = fields.Float(string="AI na projekt, max (USD)", digits=(12, 6), aggregator="max")
    projects_over_target = fields.Integer(string="Projekty nad cieľom")
    ai_cost_usd_without_project = fields.Float(string="AI bez projektu (USD)", digits=(12, 6))

    # Filled at ingest from the daily rows and the client's contract.
    ai_cost_usd_total = fields.Float(string="AI spolu (USD)", digits=(12, 6), readonly=True)
    pdf_items_proposed = fields.Integer(string="Návrhy z PDF", readonly=True)
    pdf_items_corrected = fields.Integer(string="Opravené návrhy", readonly=True)
    pdf_corrected_pct = fields.Float(string="% opravených návrhov", digits=(5, 1), readonly=True, aggregator="avg")
    currency_id = fields.Many2one("res.currency", string="Mena", readonly=True)
    revenue_amount = fields.Monetary(string="Tržby (MRR)", currency_field="currency_id", readonly=True)
    ai_cost_amount = fields.Monetary(string="AI náklady", currency_field="currency_id", readonly=True)
    contribution_margin = fields.Monetary(string="Príspevková marža", currency_field="currency_id", readonly=True)

    _partner_month_unique = models.Constraint("UNIQUE(partner_id, month)", "Jeden mesiac klienta môže mať iba jeden riadok metrík.")

    def _refresh_from_daily(self):
        """Month totals from daily rows and the contract; past months keep the MRR they were computed with."""
        Daily = self.env["arcigy.client.metric.daily"]
        for row in self:
            month_end = fields.Date.end_of(row.month, "month")
            daily = Daily.search([("partner_id", "=", row.partner_id.id), ("date", ">=", row.month), ("date", "<=", month_end)])
            proposed = sum(daily.mapped("pdf_items_proposed"))
            corrected = sum(daily.mapped("pdf_items_corrected"))
            ai_usd = sum(daily.mapped("ai_cost_usd_total"))
            company = self.env.company
            currency = company.currency_id
            usd = self.env.ref("base.USD")
            partner = row.partner_id
            revenue = partner.arcigy_mrr_currency_id._convert(partner.arcigy_mrr, currency, company, month_end) if partner.arcigy_mrr else 0.0
            ai_cost = usd._convert(ai_usd, currency, company, month_end) if ai_usd else 0.0
            row.write({
                "ai_cost_usd_total": ai_usd,
                "pdf_items_proposed": proposed,
                "pdf_items_corrected": corrected,
                "pdf_corrected_pct": round(100.0 * corrected / proposed, 1) if proposed else 0.0,
                "currency_id": currency.id,
                "revenue_amount": revenue,
                "ai_cost_amount": ai_cost,
                "contribution_margin": revenue - ai_cost,
            })


class ArcigyClientUserMonthly(models.Model):
    _name = "arcigy.client.user.monthly"
    _description = "Arcigy app user results per month"
    _order = "month desc, contact_id"
    _rec_name = "month"

    contact_id = fields.Many2one("res.partner", string="Používateľ", required=True, index=True, ondelete="cascade")
    partner_id = fields.Many2one(related="contact_id.parent_id", string="Klient", store=True, index=True)
    month = fields.Date(string="Mesiac", required=True, index=True)
    batch_generated_at = fields.Datetime(string="Dávka z", readonly=True)
    projects_created = fields.Integer(string="Nové projekty")
    offers_generated = fields.Integer(string="Vygenerované ponuky")
    offers_sent = fields.Integer(string="Odoslané ponuky")
    pdf_documents_processed = fields.Integer(string="Spracované PDF")
    pdf_items_proposed = fields.Integer(string="Návrhy z PDF")
    pdf_items_corrected = fields.Integer(string="Opravené návrhy")

    _contact_month_unique = models.Constraint("UNIQUE(contact_id, month)", "Jeden mesiac používateľa môže mať iba jeden riadok.")

    @api.autovacuum
    def _gc_old_user_months(self):
        """Personal data minimisation: per-user summaries are kept for 24 months."""
        limit = fields.Date.subtract(fields.Date.context_today(self), months=24)
        self.sudo().search([("month", "<", limit)]).unlink()
