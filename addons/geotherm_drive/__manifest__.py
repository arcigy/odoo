{
    "name": "Geotherm Google Drive",
    "version": "19.0.1.0.0",
    "summary": "Controlled Google Drive storage for CRM lead projects",
    "category": "Sales/CRM",
    "license": "LGPL-3",
    "author": "Arcigy",
    "depends": ["geotherm_chatbot", "crm", "mail"],
    "external_dependencies": {
        "python": ["googleapiclient", "google_auth_oauthlib", "google.oauth2"],
    },
    "data": [
        "security/ir.model.access.csv",
        "views/crm_lead_views.xml",
        "views/settings_views.xml",
    ],
    "application": False,
    "installable": True,
}
