from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    geotherm_drive_client_id = fields.Char(config_parameter="geotherm_drive.client_id")
    geotherm_drive_root_folder_name = fields.Char(default="FIRMA_STRUKTURA", config_parameter="geotherm_drive.root_folder_name")
    geotherm_drive_root_folder_id = fields.Char(config_parameter="geotherm_drive.root_folder_id")
    geotherm_drive_redirect_uri = fields.Char(config_parameter="geotherm_drive.redirect_uri")
    geotherm_drive_connected_at = fields.Char(readonly=True, config_parameter="geotherm_drive.connected_at")
