"""Creates the technical Odoo user that receives the app's daily client metrics, and its API key.

Run inside `odoo shell` after installing arcigy_client_crm:

    ARCIGY_CRM_ENVIRONMENT=prod ARCIGY_CRM_KEY_OUT=/tmp/arcigy-crm-ingest.key \
      /entrypoint.sh odoo shell -c /tmp/geotherm-odoo.conf -d <db> --no-http < setup_ingest_user.py

The key is written once to ARCIGY_CRM_KEY_OUT (mode 600) and never printed. Copy it into the app's
ARCIGY_CRM_ODOO_API_KEY and delete the file. Re-running revokes the previous key of this user.
"""
import os
from datetime import timedelta

from odoo import Command, fields

LOGIN = "arcigy-crm-ingest"
KEY_NAME = "arcigy-crm-export"
KEY_DAYS = 365

environment = os.environ.get("ARCIGY_CRM_ENVIRONMENT", "prod")
key_out = os.environ["ARCIGY_CRM_KEY_OUT"]
if os.path.exists(key_out):
    raise SystemExit(f"{key_out} already exists; move it away first.")

groups = [env.ref("base.group_portal").id, env.ref("arcigy_client_crm.group_crm_ingest").id]
user = env["res.users"].with_context(active_test=False).search([("login", "=", LOGIN)], limit=1)
if not user:
    user = env["res.users"].with_context(no_reset_password=True).create({
        "name": "Arcigy CRM · príjem metrík", "login": LOGIN, "group_ids": [Command.set(groups)],
    })
    print(f"[crm-setup] created user {LOGIN}")
else:
    user.write({"active": True, "group_ids": [Command.set(groups)]})
    print(f"[crm-setup] updated user {LOGIN}")

old_keys = env["res.users.apikeys"].sudo().search([("user_id", "=", user.id), ("name", "=", KEY_NAME)])
if old_keys:
    old_keys._remove()
    print(f"[crm-setup] revoked {len(old_keys)} previous key(s)")
key = env["res.users.apikeys"].with_user(user).sudo()._generate(None, KEY_NAME, fields.Datetime.now() + timedelta(days=KEY_DAYS))
fd = os.open(key_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(key)
print(f"[crm-setup] API key written to {key_out}, valid {KEY_DAYS} days")

env["ir.config_parameter"].sudo().set_param("arcigy_client_crm.accepted_environment", environment)
print(f"[crm-setup] this Odoo accepts batches from environment {environment!r}")
env.cr.commit()
