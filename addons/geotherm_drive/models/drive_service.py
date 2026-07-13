import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import secrets
from datetime import datetime, timezone

from odoo import _, api, models
from odoo.exceptions import UserError


DRIVE_FOLDER_MIME = "application/vnd.google-apps.folder"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


class GeothermDriveService(models.AbstractModel):
    _name = "geotherm.drive.service"
    _description = "Geotherm Google Drive service"

    def _param(self, env_name, key, default=""):
        return os.getenv(env_name) or self.env["ir.config_parameter"].sudo().get_param(key, default)

    def redirect_uri(self):
        return self._param("GOOGLE_DRIVE_REDIRECT_URI", "geotherm_drive.redirect_uri")

    def client_config(self):
        client_id = self._param("GOOGLE_CLIENT_ID", "geotherm_drive.client_id")
        client_secret = self._param("GOOGLE_CLIENT_SECRET", "geotherm_drive.client_secret")
        if not client_id or not client_secret:
            raise UserError(_("Google Drive OAuth credentials are not configured."))
        return {
            "web": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [self.redirect_uri()],
            }
        }

    def refresh_token(self):
        return self._param("GOOGLE_DRIVE_REFRESH_TOKEN", "geotherm_drive.refresh_token")

    def oauth_state_secret(self):
        return self._param("GOOGLE_OAUTH_STATE_SECRET", "geotherm_drive.oauth_state_secret") or self._param(
            "ODOO_MASTER_PASSWORD", "database.secret"
        )

    def _require_state_secret(self):
        secret = self.oauth_state_secret()
        if not secret:
            raise UserError(_("Google OAuth state secret is not configured."))
        return secret

    def _query_value(self, value):
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"

    def authorization_url(self, uid):
        from google_auth_oauthlib.flow import Flow

        if not self.redirect_uri():
            raise UserError(_("Google Drive redirect URI is not configured."))
        state_payload = json.dumps({"uid": uid, "nonce": secrets.token_urlsafe(24)}, separators=(",", ":"))
        signature = hmac.new(self._require_state_secret().encode(), state_payload.encode(), hashlib.sha256).hexdigest()
        state = base64.urlsafe_b64encode(state_payload.encode()).decode().rstrip("=") + "." + signature
        flow = Flow.from_client_config(self.client_config(), scopes=[DRIVE_SCOPE], state=state)
        flow.redirect_uri = self.redirect_uri()
        url, _ = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
        return url, state

    def exchange_code(self, state, code):
        from google_auth_oauthlib.flow import Flow

        try:
            encoded, signature = state.split(".", 1)
            payload = base64.urlsafe_b64decode(encoded + "=").decode()
        except (ValueError, UnicodeDecodeError, binascii.Error) as error:
            raise UserError(_("Invalid Google OAuth state.")) from error
        expected = hmac.new(self._require_state_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise UserError(_("Invalid Google OAuth state signature."))
        flow = Flow.from_client_config(self.client_config(), scopes=[DRIVE_SCOPE], state=state)
        flow.redirect_uri = self.redirect_uri()
        flow.fetch_token(code=code)
        token = flow.credentials.refresh_token
        if not token:
            raise UserError(_("Google did not return a refresh token. Re-authorize with consent."))
        uid = int(json.loads(payload)["uid"])
        user = self.env["res.users"].sudo().browse(uid).exists()
        if not user or not user.has_group("base.group_system"):
            raise UserError(_("OAuth user is not an Odoo administrator."))
        self.env["ir.config_parameter"].sudo().set_param("geotherm_drive.refresh_token", token)
        self.env["ir.config_parameter"].sudo().set_param("geotherm_drive.connected_at", datetime.now(timezone.utc).isoformat())
        return uid

    def service(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        refresh_token = self.refresh_token()
        if not refresh_token:
            raise UserError(_("Google Drive is not connected. Connect it in Geotherm settings."))
        credentials = Credentials(
            None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._param("GOOGLE_CLIENT_ID", "geotherm_drive.client_id"),
            client_secret=self._param("GOOGLE_CLIENT_SECRET", "geotherm_drive.client_secret"),
            scopes=[DRIVE_SCOPE],
        )
        if not credentials.valid or credentials.expired:
            credentials.refresh(Request())
        return build("drive", "v3", credentials=credentials, cache_discovery=False)

    def root_folder_id(self):
        configured = self._param("GOOGLE_DRIVE_ROOT_FOLDER_ID", "geotherm_drive.root_folder_id")
        if configured:
            return configured
        name = self._param("GOOGLE_DRIVE_ROOT_FOLDER_NAME", "geotherm_drive.root_folder_name", "FIRMA_STRUKTURA")
        result = self.service().files().list(
            q="name = %s and mimeType = %s and trashed = false and 'root' in parents" % (self._query_value(name), self._query_value(DRIVE_FOLDER_MIME)),
            spaces="drive",
            fields="files(id,name,webViewLink)",
            pageSize=10,
        ).execute()
        if not result.get("files"):
            raise UserError(_("Root Drive folder %s was not found." % name))
        return result["files"][0]["id"]

    def find_or_create_folder(self, name, parent_id):
        service = self.service()
        query = "name = %s and mimeType = %s and trashed = false and %s in parents" % (
            self._query_value(name),
            self._query_value(DRIVE_FOLDER_MIME),
            self._query_value(parent_id),
        )
        result = service.files().list(q=query, spaces="drive", fields="files(id,name,webViewLink)", pageSize=10).execute()
        if result.get("files"):
            return result["files"][0]
        return service.files().create(
            body={"name": name, "mimeType": DRIVE_FOLDER_MIME, "parents": [parent_id]},
            fields="id,name,webViewLink",
        ).execute()

    def ensure_path(self, names):
        parent_id = self.root_folder_id()
        current = None
        for name in names:
            current = self.find_or_create_folder(name, parent_id)
            parent_id = current["id"]
        return current

    def upload_attachment(self, attachment, parent_id):
        from googleapiclient.http import MediaIoBaseUpload

        content = base64.b64decode(attachment.datas or b"")
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=attachment.mimetype or "application/octet-stream", resumable=True)
        service = self.service()
        existing = self.env["geotherm.drive.file"].sudo().search([("attachment_id", "=", attachment.id)], limit=1)
        body = {"name": attachment.name, "parents": [parent_id], "description": "Odoo attachment %s" % attachment.id}
        if existing and existing.drive_file_id:
            result = service.files().update(fileId=existing.drive_file_id, body=body, media_body=media, fields="id,name,webViewLink,md5Checksum").execute()
        else:
            result = service.files().create(body=body, media_body=media, fields="id,name,webViewLink,md5Checksum").execute()
        checksum = hashlib.md5(content).hexdigest()
        values = {
            "attachment_id": attachment.id,
            "res_model": attachment.res_model,
            "res_id": attachment.res_id,
            "drive_file_id": result.get("id"),
            "drive_parent_id": parent_id,
            "drive_url": result.get("webViewLink") or "https://drive.google.com/open?id=%s" % result.get("id"),
            "checksum": checksum,
            "state": "synced",
            "last_error": False,
            "last_sync_at": datetime.now(timezone.utc).replace(tzinfo=None),
        }
        if existing:
            existing.write(values)
        else:
            self.env["geotherm.drive.file"].sudo().create(values)
        return result
