# Arcigy client CRM: metrics from the app

`arcigy_client_crm` receives a daily batch of client metrics from the Arcigy app
(`arcigy/kitchen_app`, `docs/CRM_CLIENT_METRICS.md`) through
`arcigy.client.export.batch.arcigy_ingest`. `arcigy_client_crm_feedback` links feedback reports
(`arcigy_feedback`) to client companies; it installs automatically when both modules are present.

## Rollout

1. Take and verify a backup (`CapRover/ops/odoo-backup/backup.sh`).
2. Install the modules once (or upgrade `arcigy_client_crm` if it is installed):
   `odoo -d <db> -i arcigy_client_crm --stop-after-init` (the feedback bridge follows automatically).
3. Create the ingest user and its key with `setup_ingest_user.py` (see the docstring), put the key
   into the app's `ARCIGY_CRM_ODOO_API_KEY`, delete the key file.
4. Set `Kľúč klienta v aplikácii` on each client company (`organization_id`, e.g. `client_delfi`).
   Their past feedback reports are linked immediately.
5. Enable the export in the app (`ARCIGY_CRM_EXPORT_ENABLED=true`, `ARCIGY_CRM_EXPORT_CLIENT_KEYS`),
   run `npm run crm:export -- --dry-run`, then a backfill.
6. Check *Kontakty > Konfigurácia > Dávky exportu*: every batch is `Zapísaná`; unmatched keys mean a
   company is not linked yet.

## Signals

The daily cron *Arcigy klienti: signály* creates one to-do per open finding for the client owner
(`Obchodník`, or the administrator). Thresholds are system parameters `arcigy_client_crm.signal_*`
(defaults in `models/client_signals.py`). Rules about activity (offers, active users) start only
after 56 days of data, because with few clients a holiday week looks like churn.

Per-user monthly results are visible only to the group *Správca klientov* and are deleted after
24 months.
