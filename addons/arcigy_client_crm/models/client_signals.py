from odoo import api, fields, models

PARAM = "arcigy_client_crm.signal_"
DEFAULTS = {
    "offers_drop_pct": 40.0,
    "offers_drop_min_prev_8w": 4.0,
    "no_offer_days": 14.0,
    "overdue_days": 7.0,
    "billing_notice_days": 7.0,
    "ai_p90_target_usd": 0.10,
    "pdf_correction_rise_pp": 10.0,
    "pdf_min_proposals": 20.0,
    "stale_days": 2.0,
    # Activity rules need history: with one client a holiday week looks like churn.
    "activity_min_days": 56.0,
}
ACTIVE_STAGES = ("pilot", "mvp", "paying")


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _arcigy_signal_param(self, name):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM + name)
        try:
            return float(value) if value not in (None, False, "") else DEFAULTS[name]
        except ValueError:
            return DEFAULTS[name]

    @api.model
    def _arcigy_run_signals(self):
        """Daily cron: turns client metrics into to-dos for the client owner instead of a dashboard."""
        created = []
        for partner in self.search([("arcigy_client_key", "!=", False), ("is_company", "=", True)]):
            for title, note in partner._arcigy_signal_findings():
                if partner._arcigy_schedule_signal(title, note):
                    created.append((partner.arcigy_client_key, title))
        return created

    def _arcigy_schedule_signal(self, title, note):
        self.ensure_one()
        summary = f"[Arcigy] {title}"
        open_activity = self.env["mail.activity"].sudo().search_count([
            ("res_model", "=", "res.partner"), ("res_id", "=", self.id), ("summary", "=", summary)], limit=1)
        if open_activity:
            return False
        owner = self.user_id or self.env.ref("base.user_admin")
        self.sudo().activity_schedule(
            "mail.mail_activity_data_todo",
            date_deadline=fields.Date.context_today(self),
            summary=summary,
            note=note,
            user_id=owner.id,
        )
        return True

    def _arcigy_signal_findings(self):
        """(title, explanation) for every rule that fires; extend in other modules for more sources."""
        self.ensure_one()
        today = fields.Date.context_today(self)
        param = self._arcigy_signal_param
        findings = []
        Daily = self.env["arcigy.client.metric.daily"]
        Monthly = self.env["arcigy.client.metric.monthly"]
        active_client = self.arcigy_client_stage in ACTIVE_STAGES

        if self.arcigy_data_as_of and self.arcigy_data_as_of < fields.Date.subtract(today, days=int(param("stale_days"))):
            findings.append(("Staré dáta z aplikácie", f"Posledné metriky sú k {self.arcigy_data_as_of}. Skontrolujte denný export, kým podľa nich rozhodnete."))

        first = Daily.search([("partner_id", "=", self.id)], order="date asc", limit=1)
        history_days = (self.arcigy_data_as_of - first.date).days + 1 if first and self.arcigy_data_as_of else 0
        if active_client and history_days >= param("activity_min_days"):
            prev_total = self.arcigy_offers_sent_prev_4w_avg * 2
            if prev_total >= param("offers_drop_min_prev_8w") and self.arcigy_offers_sent_4w <= self.arcigy_offers_sent_prev_4w_avg * (1 - param("offers_drop_pct") / 100):
                findings.append(("Pokles odoslaných ponúk", (
                    f"Za posledné 4 týždne {self.arcigy_offers_sent_4w} odoslaných ponúk, predtým priemerne "
                    f"{self.arcigy_offers_sent_prev_4w_avg:.1f} za 4 týždne. Zavolajte klientovi, či ich niečo neblokuje.")))
            window = int(param("no_offer_days"))
            recent = Daily.search([("partner_id", "=", self.id), ("date", ">", fields.Date.subtract(self.arcigy_data_as_of, days=window))])
            if not sum(recent.mapped("offers_sent")):
                findings.append((f"{window} dní bez odoslanej ponuky", "Klient neoznačil žiadnu ponuku ako odoslanú. Overte, či aplikáciu používa a či tlačidlo pozná."))
            if self.arcigy_users_active_30d == 0:
                findings.append(("Nikto z klienta aplikáciu nepoužíva", "Za 30 dní žiadny projekt, ponuka ani rozhodnutie v PDF. Ozvite sa kľúčovému človeku."))

        if self.arcigy_oldest_overdue_days > param("overdue_days"):
            findings.append(("Faktúra po splatnosti", f"Najstaršia neuhradená faktúra je {self.arcigy_oldest_overdue_days} dní po splatnosti ({self.arcigy_amount_overdue:.2f} {self.currency_id.name})."))

        if self.arcigy_billing_start_date and self.arcigy_billing_start_date <= fields.Date.add(today, days=int(param("billing_notice_days"))):
            invoiced = self.env["account.move"].search_count([
                ("commercial_partner_id", "=", self.commercial_partner_id.id), ("move_type", "=", "out_invoice"),
                ("state", "=", "posted"), ("invoice_date", ">=", self.arcigy_billing_start_date)], limit=1)
            if not invoiced:
                findings.append(("Vystaviť prvú faktúru po MVP", f"Platba začína {self.arcigy_billing_start_date} a od tohto dňa nie je vystavená žiadna faktúra."))

        month = Monthly.search([("partner_id", "=", self.id)], order="month desc", limit=2)
        if month and month[0].ai_cost_per_project_p90_usd > param("ai_p90_target_usd"):
            findings.append(("AI náklady nad cieľom", (
                f"V mesiaci {month[0].month:%m/%Y} stojí 90 % projektov do {month[0].ai_cost_per_project_p90_usd:.3f} USD, "
                f"cieľ je {param('ai_p90_target_usd'):.2f} USD. Pozrite, ktorá časť (PDF alebo asistent) míňa.")))
        if len(month) == 2 and month[0].pdf_items_proposed >= param("pdf_min_proposals") \
                and month[0].pdf_corrected_pct - month[1].pdf_corrected_pct >= param("pdf_correction_rise_pp"):
            findings.append(("Zhoršenie PDF intake", (
                f"Opravených návrhov {month[0].pdf_corrected_pct:.1f} % oproti {month[1].pdf_corrected_pct:.1f} % "
                "v predchádzajúcom mesiaci. Skontrolujte posledné nasadenie PDF intake.")))
        return findings
