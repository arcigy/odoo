import re
from datetime import date

from odoo import _, fields, models
from odoo.exceptions import UserError


class CrmLead(models.Model):
    _inherit = "crm.lead"

    geotherm_drive_folder_id = fields.Char(copy=False, readonly=True)
    geotherm_drive_folder_url = fields.Char(copy=False, readonly=True)
    geotherm_drive_state = fields.Selection(
        [("not_synced", "Not synced"), ("synced", "Synced"), ("error", "Error")],
        default="not_synced",
        copy=False,
    )
    geotherm_drive_error = fields.Text(copy=False, readonly=True)

    def _drive_folder_name(self):
        self.ensure_one()
        label = self.partner_name or self.contact_name or self.name or "Lead"
        label = re.sub(r"[^\w .-]+", "", label, flags=re.UNICODE).strip() or "Lead"
        return "%s_%s_%s" % (date.today().isoformat(), label[:80], self.id)

    def action_geotherm_drive_sync(self):
        service = self.env["geotherm.drive.service"]
        for lead in self:
            try:
                base = service.ensure_path(["40_OPERATIONS_AND_CLIENTS", "02_Client_Projects"])
                project = service.find_or_create_folder(lead._drive_folder_name(), base["id"])
                subfolders = {}
                for name in ["01_Discovery", "02_Execution", "03_Delivery", "04_Archive"]:
                    subfolders[name] = service.find_or_create_folder(name, project["id"])
                attachments = self.env["ir.attachment"].sudo().search([("res_model", "=", "crm.lead"), ("res_id", "=", lead.id)])
                for attachment in attachments:
                    service.upload_attachment(attachment, subfolders["01_Discovery"]["id"])
                lead.write({
                    "geotherm_drive_folder_id": project["id"],
                    "geotherm_drive_folder_url": project.get("webViewLink") or "https://drive.google.com/drive/folders/%s" % project["id"],
                    "geotherm_drive_state": "synced",
                    "geotherm_drive_error": False,
                })
            except Exception as error:
                lead.write({"geotherm_drive_state": "error", "geotherm_drive_error": str(error)})
                raise UserError(_("Google Drive sync failed: %s") % error) from error
        return True
