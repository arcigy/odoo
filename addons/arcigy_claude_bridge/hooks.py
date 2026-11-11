from .models.claude_ask import AGENT_LOGINS_PARAM, DEFAULT_AGENT_LOGINS


def post_init_hook(env):
    """Put the configured agent users into the agent group and create the questions channel."""
    params = env["ir.config_parameter"].sudo()
    logins_value = params.get_param(AGENT_LOGINS_PARAM)
    if not logins_value:
        logins_value = DEFAULT_AGENT_LOGINS
        params.set_param(AGENT_LOGINS_PARAM, logins_value)
    logins = [login.strip() for login in logins_value.split(",") if login.strip()]
    users = env["res.users"].sudo().with_context(active_test=False).search([("login", "in", logins)])
    group = env.ref("arcigy_claude_bridge.group_claude_agent")
    if users:
        group.sudo().write({"user_ids": [(4, user.id) for user in users]})
    env["arcigy.claude.ask"]._get_channel()
