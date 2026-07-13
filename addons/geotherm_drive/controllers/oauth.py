from odoo import http
from odoo.http import request
from werkzeug.utils import redirect


class GeothermDriveOAuthController(http.Controller):
    @http.route("/geotherm/drive/oauth/start", type="http", auth="user", methods=["GET"], csrf=False)
    def oauth_start(self, **_kwargs):
        if not request.env.user.has_group("base.group_system"):
            return request.not_found()
        service = request.env["geotherm.drive.service"].sudo()
        url, _state = service.authorization_url(request.env.user.id)
        return redirect(url)

    @http.route("/geotherm/drive/oauth/callback", type="http", auth="none", methods=["GET"], csrf=False)
    def oauth_callback(self, code=None, state=None, error=None, **_kwargs):
        if error:
            return redirect("/web")
        if not code or not state:
            return request.make_response("Missing OAuth code or state.", status=400)
        try:
            uid = request.env["geotherm.drive.service"].sudo().exchange_code(state, code)
            user = request.env["res.users"].sudo().browse(uid).exists()
            if not user or not user.has_group("base.group_system"):
                return request.make_response("OAuth user is not an Odoo administrator.", status=403)
        except Exception as exc:
            return request.make_response("Google Drive authorization failed: %s" % exc, status=400)
        return redirect("/web")
