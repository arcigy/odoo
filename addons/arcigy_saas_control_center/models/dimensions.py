from odoo import fields, models


class SaasEnvironment(models.Model):
    _name = "saas.environment"
    _description = "SaaS environment"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Selection(
        [("develop", "Develop"), ("main", "Main")],
        required=True,
        index=True,
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Environment code must be unique.")


class SaasDashboard(models.Model):
    _name = "saas.dashboard"
    _description = "SaaS dashboard"
    _order = "priority, sequence, id"

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, index=True)
    purpose = fields.Text(translate=True)
    priority = fields.Selection(
        [("p0", "P0"), ("p1", "P1"), ("p2", "P2"), ("optional", "Optional")],
        required=True,
        default="p2",
        index=True,
    )
    owner = fields.Char()
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Dashboard code must be unique.")


class SaasService(models.Model):
    _name = "saas.service"
    _description = "SaaS service"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    owner = fields.Char()
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Service code must be unique.")


class SaasRegion(models.Model):
    _name = "saas.region"
    _description = "SaaS region"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    provider = fields.Char(index=True)
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Region code must be unique.")


class SaasPlan(models.Model):
    _name = "saas.plan"
    _description = "SaaS plan"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Plan code must be unique.")


class SaasTenant(models.Model):
    _name = "saas.tenant"
    _description = "SaaS tenant"
    _order = "name"

    name = fields.Char(required=True)
    external_id = fields.Char(required=True, index=True)
    partner_id = fields.Many2one("res.partner", ondelete="restrict", index=True)
    plan_id = fields.Many2one("saas.plan", ondelete="restrict", index=True)
    country_id = fields.Many2one("res.country", ondelete="restrict", index=True)
    size_band = fields.Selection(
        [
            ("micro", "Micro"),
            ("small", "Small"),
            ("medium", "Medium"),
            ("large", "Large"),
            ("enterprise", "Enterprise"),
        ],
        index=True,
    )
    status = fields.Selection(
        [
            ("active", "Active"),
            ("suspended", "Suspended"),
            ("inactive", "Inactive"),
            ("deleted", "Deleted"),
        ],
        required=True,
        default="active",
        index=True,
    )
    owner_id = fields.Many2one("res.users", string="Customer success owner")
    active = fields.Boolean(default=True)

    _external_id_unique = models.Constraint(
        "UNIQUE(external_id)", "Tenant external ID must be unique."
    )


class SaasFeature(models.Model):
    _name = "saas.feature"
    _description = "SaaS feature"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    owner = fields.Char()
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Feature code must be unique.")


class SaasIntegration(models.Model):
    _name = "saas.integration"
    _description = "SaaS integration"
    _order = "name"

    name = fields.Char(required=True)
    code = fields.Char(required=True, index=True)
    provider = fields.Char(index=True)
    owner = fields.Char()
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("UNIQUE(code)", "Integration code must be unique.")


class SaasRelease(models.Model):
    _name = "saas.release"
    _description = "SaaS release"
    _order = "released_at desc, id desc"

    version = fields.Char(required=True, index=True)
    environment_id = fields.Many2one(
        "saas.environment", required=True, ondelete="restrict", index=True
    )
    commit_sha = fields.Char(index=True)
    released_at = fields.Datetime(index=True)
    status = fields.Selection(
        [
            ("healthy", "Healthy"),
            ("warning", "Warning"),
            ("critical", "Critical"),
            ("rolled_back", "Rolled back"),
            ("unknown", "Unknown"),
        ],
        required=True,
        default="unknown",
        index=True,
    )
    change_source = fields.Selection(
        [("human", "Human"), ("ai_assisted", "AI assisted"), ("mixed", "Mixed")],
        default="mixed",
    )

    _version_environment_unique = models.Constraint(
        "UNIQUE(version, environment_id)",
        "Release version must be unique inside one environment.",
    )
