{
    "name": "Arcigy – otázky z Claude chatov",
    "version": "19.0.1.0.0",
    "summary": "Questions from the owner's Claude Code chats, answered on the phone",
    "category": "Productivity/Discuss",
    "license": "LGPL-3",
    "author": "Arcigy",
    "depends": ["mail"],
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "views/ask_views.xml",
        "views/ask_templates.xml",
    ],
    "post_init_hook": "post_init_hook",
    "application": False,
    "installable": True,
}
