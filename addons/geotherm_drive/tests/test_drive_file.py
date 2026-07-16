from odoo.exceptions import ValidationError
from odoo.tests.common import TransactionCase


class TestGeothermDriveFile(TransactionCase):
    def test_one_attachment_cannot_have_two_drive_mappings(self):
        attachment = self.env["ir.attachment"].create({"name": "constraint-test.txt"})
        values = {
            "attachment_id": attachment.id,
            "res_model": "crm.lead",
            "res_id": 0,
            "drive_file_id": "test-drive-file-1",
            "drive_parent_id": "test-drive-parent",
        }
        self.env["geotherm.drive.file"].create(values)

        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env["geotherm.drive.file"].create({**values, "drive_file_id": "test-drive-file-2"})
