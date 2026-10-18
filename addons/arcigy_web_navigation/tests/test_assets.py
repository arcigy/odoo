from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestNavbarHistoryAssets(TransactionCase):
    def test_backend_bundle_ships_the_buttons(self):
        bundle = self.env["ir.qweb"]._get_asset_bundle("web.assets_backend", debug_assets=True)
        urls = [file["url"] for file in bundle.files]
        for name in ("navbar_history.js", "navbar_history.xml", "navbar_history.scss"):
            self.assertTrue(any(url.endswith(f"/arcigy_web_navigation/static/src/navbar_history/{name}") for url in urls), name)

    def test_template_extends_the_navbar_after_the_apps_menu(self):
        bundle = self.env["ir.qweb"]._get_asset_bundle("web.assets_backend", debug_assets=True)
        templates = bundle.generate_xml_bundle()
        self.assertIn('t-inherit="web.NavBar"', templates)
        self.assertIn("o_arcigy_history_back", templates)
        self.assertIn("o_arcigy_history_forward", templates)
