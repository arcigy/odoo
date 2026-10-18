{
    "name": "Arcigy klienti",
    "version": "19.0.1.1.0",
    "summary": "Client-focused CRM on res.partner: contract, MRR and receivables per client",
    "category": "Sales/CRM",
    "license": "LGPL-3",
    "author": "Arcigy",
    "depends": ["contacts", "account", "mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/crm_data.xml",
        "views/client_metric_views.xml",
        "views/res_partner_views.xml",
    ],
    "application": False,
    "installable": True,
}
