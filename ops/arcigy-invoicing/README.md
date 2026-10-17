# Arcigy invoicing setup

`setup_invoicing.py` prepares the Odoo company for issuing invoices to Arcigy clients and links
each client company to the app (`arcigy_client_crm`). It is idempotent: it only fills in what is
missing or different, and `"commit": false` (the default in the example) rolls everything back.

What it sets up:

- company identity printed on invoices (IČO, DIČ, IČ DPH, trade registry, IBAN);
- the Slovak chart of accounts, taxes and fiscal positions (`l10n_sk`), only on a company without
  journal entries;
- customer payments posting to the outstanding receipts account, so partial payments reconcile;
- the legal note on the fiscal position "Obchody s EU" (B2B services to other EU countries,
  reverse charge);
- the sales journal code, which is the invoice number prefix (only before the first posted invoice);
- the subscription product and each client company with its contract fields and payment term.

The tax treatment (Arcigy registered under § 7a, reverse charge for EU B2B services, the legal
note text) must be confirmed by the accountant before the first real invoice.

## Run

1. Install the modules once: `odoo -d <db> -i l10n_sk,arcigy_client_crm --stop-after-init`
   (or `ODOO_INIT=1` with `ODOO_INIT_MODULES`, see `docs/ARCIGY_ODOO_DEPLOYMENT.md`).
2. Copy `config.example.json` outside the repository, fill in real data, keep `"commit": false`.
3. Dry run, then set `"commit": true` and run again:

```sh
docker cp invoicing.json <odoo-container>:/tmp/invoicing.json
docker exec -i -e ARCIGY_INVOICING_CONFIG=/tmp/invoicing.json <odoo-container> \
  /entrypoint.sh odoo shell -c /tmp/geotherm-odoo.conf -d <db> --no-http < ops/arcigy-invoicing/setup_invoicing.py
docker exec <odoo-container> rm /tmp/invoicing.json
```

The config holds company bank details and client data: never commit it.
