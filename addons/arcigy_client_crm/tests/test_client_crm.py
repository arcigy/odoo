from datetime import timedelta

from psycopg2 import IntegrityError

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install")
class TestClientCrm(AccountTestInvoicingCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.today = fields.Date.context_today(cls.env.user)
        cls.client = cls.env["res.partner"].create(
            {"name": "DELFI test", "is_company": True, "arcigy_client_key": "client_delfi_test"}
        )
        cls.client_contact = cls.env["res.partner"].create(
            {"name": "DELFI fakturácia", "parent_id": cls.client.id, "type": "invoice"}
        )

    def _invoice(self, amount, days_until_due, partner=None, move_type="out_invoice"):
        return self._create_invoice_one_line(
            move_type=move_type,
            price_unit=amount,
            tax_ids=[],
            partner_id=partner or self.client,
            invoice_date=self.today - timedelta(days=30),
            invoice_date_due=self.today + timedelta(days=days_until_due),
            invoice_payment_term_id=False,
            post=True,
        )

    def test_client_key_rules(self):
        with self.assertRaises(ValidationError):
            self.env["res.partner"].create({"name": "Osoba", "arcigy_client_key": "client_person"})
        with self.assertRaises(ValidationError):
            self.env["res.partner"].create(
                {"name": "Zlý kľúč", "is_company": True, "arcigy_client_key": "Client DELFI"}
            )
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError), self.cr.savepoint():
            self.env["res.partner"].create(
                {"name": "Duplikát", "is_company": True, "arcigy_client_key": "client_delfi_test"}
            )
        duplicate = self.client.copy()
        self.assertFalse(duplicate.arcigy_client_key, "A copied company must not take over the client key.")

    def test_contract_changes_are_tracked(self):
        # The test base disables tracking (DISABLED_MAIL_CONTEXT); values are written at precommit.
        client = self.client.with_context(tracking_disable=False, mail_notrack=False)
        # Odoo skips tracking for records created in the current transaction; the client was
        # created in setUpClass without a commit, so close that creation phase first.
        self.env.cr.precommit.run()
        client.write({"arcigy_mrr": 150.0, "arcigy_client_stage": "paying"})
        self.env.cr.precommit.run()
        client.write({"arcigy_mrr": 200.0})
        self.env.cr.precommit.run()
        # Tracking values are readable only by system users.
        tracked = self.env["mail.tracking.value"].sudo().search(
            [
                ("mail_message_id.model", "=", "res.partner"),
                ("mail_message_id.res_id", "=", self.client.id),
                ("field_id.name", "=", "arcigy_mrr"),
            ]
        )
        self.assertEqual(sorted(tracked.mapped("new_value_float")), [150.0, 200.0])

    def test_receivables_follow_invoices_and_payments(self):
        overdue_invoice = self._invoice(100.0, days_until_due=-10)
        self._invoice(50.0, days_until_due=5, partner=self.client_contact)
        self._invoice(20.0, days_until_due=12)
        self._invoice(15.0, days_until_due=0, move_type="out_refund")
        self._register_payment(overdue_invoice, amount=30.0, payment_date=self.today - timedelta(days=2))

        self.client.invalidate_recordset()
        self.assertEqual(overdue_invoice.payment_state, "partial")
        # 70 left on the overdue invoice + 50 (invoiced to a child contact) + 20 - 15 refund.
        self.assertAlmostEqual(self.client.arcigy_amount_due, 125.0)
        self.assertAlmostEqual(self.client.arcigy_amount_overdue, 70.0)
        self.assertEqual(self.client.arcigy_oldest_overdue_days, 10)
        self.assertEqual(self.client.arcigy_next_due_date, self.today + timedelta(days=5))
        self.assertEqual(self.client.arcigy_last_payment_date, self.today - timedelta(days=2))
        # A contact shows the figures of its company.
        self.assertAlmostEqual(self.client_contact.arcigy_amount_due, 125.0)

    def test_fully_paid_invoice_without_journal_entry_is_not_due(self):
        # Odoo 18+ creates no journal entry when the payment method has no outstanding account;
        # the invoice is paid but its residual is unchanged.
        self.inbound_payment_method_line.payment_account_id = False
        invoice = self._invoice(80.0, days_until_due=-3)
        self._register_payment(invoice, payment_date=self.today)

        self.client.invalidate_recordset()
        self.assertEqual(invoice.payment_state, "paid")
        self.assertAlmostEqual(self.client.arcigy_amount_due, 0.0)
        self.assertAlmostEqual(self.client.arcigy_amount_overdue, 0.0)
        self.assertEqual(self.client.arcigy_oldest_overdue_days, 0)
        self.assertEqual(self.client.arcigy_last_payment_date, self.today)

    def test_client_without_invoices(self):
        other = self.env["res.partner"].create({"name": "Nový klient", "is_company": True})
        self.assertEqual(other.arcigy_amount_due, 0.0)
        self.assertFalse(other.arcigy_next_due_date)
        self.assertFalse(other.arcigy_last_payment_date)
