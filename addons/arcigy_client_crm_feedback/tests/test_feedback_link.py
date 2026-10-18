from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestFeedbackLink(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.project = cls.env["project.project"].create({"name": "Arcigy podpora (test)"})
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("arcigy_feedback.project_id", str(cls.project.id))
        # Treat every test task as historical so the migration does not wait for attachments.
        params.set_param("arcigy_feedback.legacy_baseline", str(10**9))
        cls.Task = cls.env["project.task"]

    def report(self, **values):
        return self.Task.create({
            "name": "Hlásenie", "project_id": self.project.id, "arcigy_report": True, "arcigy_status": "new",
            "arcigy_environment": "unknown", "arcigy_tenant_id": "client_delfi", "arcigy_kind": "price_error", **values,
        })

    def test_linking_a_company_links_its_past_and_future_reports(self):
        before = self.report()
        develop = self.report(arcigy_environment="develop")
        client = self.env["res.partner"].create({"name": "DELFI", "is_company": True, "arcigy_client_key": "client_delfi"})
        after = self.report(arcigy_kind="feature_request", arcigy_status="implemented")
        self.assertEqual(before.arcigy_partner_id, client)
        self.assertEqual(after.arcigy_partner_id, client)
        self.assertFalse(develop.arcigy_partner_id, "Reports from develop are tests, not the client's.")
        self.assertEqual(after.arcigy_kind_group, "wish")
        self.assertEqual((client.arcigy_report_count, client.arcigy_open_report_count, client.arcigy_open_price_error_count), (2, 1, 1))

        client.arcigy_client_key = "client_other"
        self.assertFalse(before.arcigy_partner_id)

    def test_migration_applies_the_new_report_types(self):
        task = self.Task.create({
            "name": "[Arcigy price_error] Nesedí cena dvierok",
            "project_id": self.project.id,
            "description": "Arcigy feedback report\nsubmission=s-1\ntype=price_error\n\nCena dvierok je o 20 % nižšia.",
        })
        self.Task._arcigy_migrate_legacy([task.id])
        self.assertTrue(task.arcigy_report)
        self.assertEqual(task.arcigy_kind, "price_error")
        self.assertEqual(task.arcigy_kind_group, "price_error")

    def test_open_price_error_signal(self):
        client = self.env["res.partner"].create({"name": "DELFI", "is_company": True, "arcigy_client_key": "client_delfi"})
        task = self.report()
        self.env.cr.execute("UPDATE project_task SET create_date = now() - interval '5 days' WHERE id = %s", [task.id])
        self.env.invalidate_all()
        titles = [title for title, _note in client._arcigy_signal_findings()]
        self.assertIn("Otvorená chyba v cene", titles)
        task.arcigy_status = "implemented"
        self.assertNotIn("Otvorená chyba v cene", [title for title, _note in client._arcigy_signal_findings()])
