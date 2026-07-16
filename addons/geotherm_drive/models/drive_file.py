from odoo import fields, models


class GeothermDriveFile(models.Model):
    _name = "geotherm.drive.file"
    _description = "Geotherm Drive file mapping"
    _rec_name = "drive_file_id"

    attachment_id = fields.Many2one("ir.attachment", required=True, ondelete="cascade", index=True)
    res_model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True, index=True)
    drive_file_id = fields.Char(required=True, index=True)
    drive_parent_id = fields.Char(required=True)
    drive_url = fields.Char(readonly=True)
    checksum = fields.Char(readonly=True)
    state = fields.Selection([("synced", "Synced"), ("error", "Error")], default="synced", required=True)
    last_error = fields.Text()
    last_sync_at = fields.Datetime(default=fields.Datetime.now)

    _attachment_unique = models.Constraint(
        "UNIQUE(attachment_id)",
        "An attachment can have only one Drive mapping.",
    )
