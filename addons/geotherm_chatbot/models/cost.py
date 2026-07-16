from odoo import fields, models


class GeothermMonthlyCost(models.Model):
    _name = "geotherm.monthly.cost"
    _description = "Geotherm mesačný náklad"
    _order = "name"

    name = fields.Char(string="Názov", required=True)
    amount = fields.Float(string="Mesačná suma", required=True, digits=(16, 2))
    currency_id = fields.Many2one(
        "res.currency",
        string="Mena",
        required=True,
        default=lambda self: self.env.company.currency_id,
    )
    active = fields.Boolean(string="Aktívne", default=True)
