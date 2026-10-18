{
    "name": "Arcigy navigácia späť a dopredu",
    "version": "19.0.1.0.0",
    "summary": "Back and forward buttons in the top bar, for Odoo opened without browser controls",
    "category": "Hidden",
    "license": "LGPL-3",
    "author": "Arcigy",
    "depends": ["web"],
    "assets": {
        "web.assets_backend": [
            "arcigy_web_navigation/static/src/navbar_history/*",
        ],
    },
    "installable": True,
}
