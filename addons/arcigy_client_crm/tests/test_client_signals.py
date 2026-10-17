from datetime import timedelta

from odoo import fields
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestClientSignals(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.context_today(cls.env.user)
        cls.client = cls.env["res.partner"].create({
            "name": "Klient", "is_company": True, "arcigy_client_key": "client_signals", "arcigy_client_stage": "paying",
        })
        cls.Daily = cls.env["arcigy.client.metric.daily"]

    def add_days(self, days, offers_sent, active_users=2, last_day=None):
        last_day = last_day or self.today - timedelta(days=1)
        for offset in range(days):
            day = last_day - timedelta(days=offset)
            self.Daily.create({
                "partner_id": self.client.id, "date": day,
                "offers_sent": offers_sent(offset), "users_total": 3, "users_active_30d": active_users,
            })
        self.client._arcigy_refresh_metric_summary()

    def titles(self):
        return [title for title, _note in self.client._arcigy_signal_findings()]

    def test_offer_drop_needs_history_and_fires_once(self):
        # 12 weeks of history: 1 sent offer per day before, none in the last 4 weeks.
        self.add_days(84, lambda offset: 0 if offset < 28 else 1)
        self.assertEqual(self.client.arcigy_offers_sent_4w, 0)
        self.assertEqual(self.client.arcigy_offers_sent_prev_4w_avg, 28)
        self.assertIn("Pokles odoslaných ponúk", self.titles())
        self.assertIn("14 dní bez odoslanej ponuky", self.titles())

        created = self.env["res.partner"]._arcigy_run_signals()
        self.assertIn(("client_signals", "Pokles odoslaných ponúk"), created)
        self.assertFalse([item for item in self.env["res.partner"]._arcigy_run_signals() if item[0] == "client_signals"],
                         "An open to-do must not be duplicated.")
        activity = self.client.activity_ids.filtered(lambda act: act.summary == "[Arcigy] Pokles odoslaných ponúk")
        self.assertEqual(activity.user_id, self.env.ref("base.user_admin"))

    def test_activity_rules_wait_for_eight_weeks_of_data(self):
        self.add_days(20, lambda offset: 0, active_users=0)
        self.assertEqual(self.titles(), [])

    def test_backfilled_days_before_offer_tracking_do_not_look_like_churn(self):
        # 70 days of history from before offer tracking existed, nobody active recently.
        self.add_days(70, lambda offset: 0, active_users=0)
        titles = self.titles()
        self.assertNotIn("Pokles odoslaných ponúk", titles)
        self.assertNotIn("14 dní bez odoslanej ponuky", titles)
        self.assertIn("Nikto z klienta aplikáciu nepoužíva", titles)

    def test_stale_data(self):
        self.add_days(3, lambda offset: 1, last_day=self.today - timedelta(days=5))
        self.assertIn("Staré dáta z aplikácie", self.titles())

    def test_ai_cost_over_target_and_pdf_quality_drop(self):
        self.add_days(1, lambda offset: 1)
        Monthly = self.env["arcigy.client.metric.monthly"]
        this_month = fields.Date.start_of(self.today, "month")
        Monthly.create({"partner_id": self.client.id, "month": fields.Date.subtract(this_month, months=1), "pdf_items_proposed": 100, "pdf_items_corrected": 10, "pdf_corrected_pct": 10.0})
        Monthly.create({"partner_id": self.client.id, "month": this_month, "ai_cost_per_project_p90_usd": 0.14, "pdf_items_proposed": 50, "pdf_items_corrected": 15, "pdf_corrected_pct": 30.0})
        titles = self.titles()
        self.assertIn("AI náklady nad cieľom", titles)
        self.assertIn("Zhoršenie PDF intake", titles)

    def test_first_invoice_reminder_before_billing_starts(self):
        self.client.arcigy_billing_start_date = self.today + timedelta(days=3)
        self.assertIn("Vystaviť prvú faktúru po MVP", self.titles())
        self.client.arcigy_billing_start_date = self.today + timedelta(days=30)
        self.assertNotIn("Vystaviť prvú faktúru po MVP", self.titles())

    def test_thresholds_are_parameters(self):
        self.add_days(1, lambda offset: 1)
        self.env["arcigy.client.metric.monthly"].create({
            "partner_id": self.client.id, "month": fields.Date.start_of(self.today, "month"), "ai_cost_per_project_p90_usd": 0.14})
        self.env["ir.config_parameter"].sudo().set_param("arcigy_client_crm.signal_ai_p90_target_usd", "0.2")
        self.assertNotIn("AI náklady nad cieľom", self.titles())
