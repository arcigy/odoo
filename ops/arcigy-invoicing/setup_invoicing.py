"""Idempotent invoicing setup for the Arcigy Odoo company and its clients.

Run inside `odoo shell` after `l10n_sk` and `arcigy_client_crm` are installed:

    ARCIGY_INVOICING_CONFIG=/tmp/invoicing.json /entrypoint.sh odoo shell -d <db> --no-http < setup_invoicing.py

The config file holds company and client data (see config.example.json) and is never
committed. Every step only fills in what is missing or different, so re-running is safe.
Set "commit": false in the config for a dry run that rolls everything back.
"""
import json
import os

from odoo import Command, fields

config = json.load(open(os.environ["ARCIGY_INVOICING_CONFIG"], encoding="utf-8"))
report = []


def log(message):
    report.append(message)
    print("[invoicing-setup]", message)


def country(code):
    return env["res.country"].search([("code", "=", code.upper())], limit=1)


def ensure_values(record, values, label):
    changed = {
        name: value
        for name, value in values.items()
        if value is not None and record[name] != (value if not hasattr(record[name], "id") else record[name].browse(value))
    }
    if changed:
        record.write(changed)
        log(f"{label}: updated {', '.join(sorted(changed))}")
    return record


def payment_term(days):
    terms = env["account.payment.term"].search([("company_id", "in", (False, company.id))])
    for term in terms:
        lines = term.line_ids
        if len(lines) == 1 and lines.value == "percent" and lines.value_amount == 100 and lines.nb_days == days:
            return term
    log(f"payment term: created {days} days")
    return env["account.payment.term"].create(
        {
            "name": f"{days} dní",
            "company_id": company.id,
            "line_ids": [Command.create({"value": "percent", "value_amount": 100, "nb_days": days})],
        }
    )


modules = env["ir.module.module"].search([("name", "in", ("l10n_sk", "arcigy_client_crm"))])
missing = {"l10n_sk", "arcigy_client_crm"} - set(modules.filtered(lambda m: m.state == "installed").mapped("name"))
if missing:
    raise SystemExit(f"Install these modules first: {', '.join(sorted(missing))}")

# 1. Company identity (printed on every invoice).
company = env.company
company_cfg = config["company"]
ensure_values(
    company,
    {
        "name": company_cfg["name"],
        "street": company_cfg.get("street"),
        "zip": company_cfg.get("zip"),
        "city": company_cfg.get("city"),
        "country_id": country(company_cfg.get("country", "SK")).id,
        "company_registry": company_cfg.get("company_registry"),
        "vat": company_cfg.get("vat"),
        "email": company_cfg.get("email"),
        "phone": company_cfg.get("phone"),
        "website": company_cfg.get("website"),
    },
    "company",
)
ensure_values(
    company,
    {"income_tax_id": company_cfg.get("income_tax_id"), "trade_registry": company_cfg.get("trade_registry")},
    "company (SK)",
)

# 2. Slovak chart of accounts, taxes and fiscal positions.
if company.chart_template != "sk":
    if env["account.move"].search_count([("company_id", "=", company.id)], limit=1):
        raise SystemExit("The company already has journal entries; load the chart of accounts manually.")
    env["account.chart.template"].try_loading("sk", company=company, install_demo=False)
    log("chart of accounts: loaded l10n_sk")
template = env["account.chart.template"].with_company(company)

# 3. Bank account on the company and the bank journal.
bank_journal = env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", company.id)], limit=1)
iban = company_cfg.get("iban", "").replace(" ", "")
if iban:
    bank_account = env["res.partner.bank"].search(
        [("acc_number", "=", iban), ("partner_id", "=", company.partner_id.id)], limit=1
    ) or env["res.partner.bank"].create({"acc_number": iban, "partner_id": company.partner_id.id})
    if bank_journal.bank_account_id != bank_account:
        bank_journal.bank_account_id = bank_account
        log("bank journal: IBAN set")
    # Odoo drops an untrusted company bank account from posted invoices, and only a person
    # (not OdooBot) may trust it, so this stays a deliberate manual step.
    if not bank_account.allow_out_payment:
        log("ACTION NEEDED: trust the company bank account in Odoo (bank account > Send Money), "
            "otherwise invoices are posted without the IBAN")

# 4. Customer payments create journal entries (needed for partial payments and for the accountant).
inbound_line = bank_journal.inbound_payment_method_line_ids[:1]
# Since Odoo 18 the outstanding accounts are chart records, not company fields.
outstanding = template.ref("account_journal_payment_debit_account_id", raise_if_not_found=False)
if inbound_line and outstanding and inbound_line.payment_account_id != outstanding:
    inbound_line.payment_account_id = outstanding
    log("bank journal: inbound payments post to the outstanding receipts account")

# 5. Legal note for B2B services to other EU countries (reverse charge).
eu_position = template.ref("fiscal_position_template_2", raise_if_not_found=False)
eu_note = config.get("eu_reverse_charge_note")
if eu_position and eu_note and (eu_position.note or "").strip() != f"<p>{eu_note}</p>":
    eu_position.note = f"<p>{eu_note}</p>"
    log("fiscal position 'Obchody s EU': legal note set")

# 6. Invoice numbering: the sales journal code is the prefix (e.g. FA/2026/00001).
sales_journal = env["account.journal"].search([("type", "=", "sale"), ("company_id", "=", company.id)], limit=1)
code = config.get("sales_journal_code")
if code and sales_journal.code != code:
    if env["account.move"].search_count([("journal_id", "=", sales_journal.id), ("state", "=", "posted")], limit=1):
        log(f"sales journal: NOT renamed to {code}, it already has posted invoices")
    else:
        sales_journal.code = code
        log(f"sales journal: code set to {code}")

# 7. The subscription product.
product_cfg = config["product"]
service_tax = template.ref(product_cfg.get("tax_xmlid", "vy_tuz_0_s"), raise_if_not_found=False)
product = env["product.product"].search([("default_code", "=", product_cfg["default_code"])], limit=1)
if not product:
    product = env["product.product"].create(
        {"name": product_cfg["name"], "default_code": product_cfg["default_code"], "type": "service"}
    )
    log(f"product: created {product_cfg['default_code']}")
ensure_values(product, {"name": product_cfg["name"], "list_price": product_cfg.get("list_price")}, "product")
if service_tax and product.taxes_id != service_tax:
    product.taxes_id = [Command.set(service_tax.ids)]
    log(f"product: sales tax set to {service_tax.name}")

# 8. Clients.
owner = env["res.users"].search([("login", "=", config.get("owner_login", "admin"))], limit=1)
for client_cfg in config.get("clients", []):
    partner = env["res.partner"].search([("arcigy_client_key", "=", client_cfg["client_key"])], limit=1)
    if not partner and client_cfg.get("vat"):
        partner = env["res.partner"].search([("vat", "=", client_cfg["vat"]), ("is_company", "=", True)], limit=1)
    if not partner:
        partner = env["res.partner"].create({"name": client_cfg["name"], "is_company": True})
        log(f"client {client_cfg['client_key']}: created")
    label = f"client {client_cfg['client_key']}"
    ensure_values(
        partner,
        {
            "name": client_cfg["name"],
            "is_company": True,
            "street": client_cfg.get("street"),
            "zip": client_cfg.get("zip"),
            "city": client_cfg.get("city"),
            "country_id": country(client_cfg["country"]).id,
            "vat": client_cfg.get("vat"),
            "company_registry": client_cfg.get("company_registry"),
            "email": client_cfg.get("email"),
            "lang": client_cfg.get("lang"),
            "user_id": owner.id or None,
        },
        label,
    )
    ensure_values(
        partner,
        {
            "arcigy_client_key": client_cfg["client_key"],
            "arcigy_client_stage": client_cfg.get("stage"),
            "arcigy_mvp_start_date": client_cfg.get("mvp_start_date") and fields.Date.to_date(client_cfg["mvp_start_date"]),
            "arcigy_billing_start_date": client_cfg.get("billing_start_date")
            and fields.Date.to_date(client_cfg["billing_start_date"]),
            "arcigy_mrr": client_cfg.get("mrr"),
            "arcigy_mrr_currency_id": env.ref(f"base.{client_cfg.get('mrr_currency', 'EUR')}").id,
        },
        label,
    )
    if client_cfg.get("payment_term_days"):
        term = payment_term(int(client_cfg["payment_term_days"]))
        if partner.with_company(company).property_payment_term_id != term:
            partner.with_company(company).property_payment_term_id = term
            log(f"{label}: payment term {term.name}")

if config.get("commit", False):
    env.cr.commit()
    log("committed")
else:
    env.cr.rollback()
    log("dry run: rolled back (set \"commit\": true to apply)")
