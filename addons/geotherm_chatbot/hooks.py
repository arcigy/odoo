import json

from odoo.tools import file_open


def post_init_hook(env):
    with file_open("geotherm_chatbot/data/geotherm-pricebook.json", "r") as source:
        payload = json.load(source)
    env["geotherm.pricebook.area"].sudo().import_catalog(
        payload,
        source_file="Predvolený Geotherm testovací cenník",
        mode="replace",
    )

