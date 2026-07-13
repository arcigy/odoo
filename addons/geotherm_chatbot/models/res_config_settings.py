from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    geotherm_api_key = fields.Char(
        string="Geotherm API kľúč",
        config_parameter="geotherm_chatbot.api_key",
    )
    geotherm_webhook_secret = fields.Char(
        string="Geotherm webhook secret",
        config_parameter="geotherm_chatbot.webhook_secret",
    )
    geotherm_salesperson_id = fields.Many2one(
        "res.users",
        string="Predvolený obchodník pre chatbot leady",
        config_parameter="geotherm_chatbot.salesperson_id",
        domain="[('share', '=', False)]",
    )

