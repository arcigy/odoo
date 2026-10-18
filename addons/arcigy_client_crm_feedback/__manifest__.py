{
    "name": "Arcigy klienti: hlásenia",
    "version": "19.0.1.0.0",
    "summary": "Links Arcigy feedback reports to client companies and adds the client success report types",
    "category": "Sales/CRM",
    "license": "LGPL-3",
    "author": "Arcigy",
    # arcigy_feedback ships from arcigy/kitchen_app (host-mounted on arcigy-odoo), so CI here cannot
    # install this bridge; it installs automatically next to both modules.
    "depends": ["arcigy_client_crm", "arcigy_feedback"],
    "data": ["views/feedback_views.xml"],
    "auto_install": True,
    "installable": True,
}
