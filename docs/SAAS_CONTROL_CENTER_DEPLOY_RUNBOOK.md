# Arcigy SaaS Control Center deployment runbook

Status: preparation only. No command in this document authorizes a live change.

## Preservation gate

Before deployment, confirm all of the following:

- the canonical Odoo source contains `geotherm_chatbot`, `geotherm_drive` and `arcigy_saas_control_center`;
- existing Geotherm CRM, pricebook, analytics, monthly costs, Drive OAuth, Drive mappings and attachments remain present;
- the current CapRover app definition and env are exported to a mode-0600 backup without printing secrets;
- PostgreSQL and `/var/lib/odoo` backups exist, have SHA-256 checksums and pass structural verification;
- host disk free space is at least 5 GB after a separately approved image-retention cleanup;
- rollback owner and verification window are agreed.

There are two separate approval gates:

- An additive Odoo-only module upgrade may proceed with explicit Odoo deploy approval and a fresh verified backup while the parallel `kitchen_app` task is active. It must not create a metrics API key, enable a sync schedule, change an Arcigy URL/credential, ingest source rows or touch the `kitchen_app` repository, service, database or storage.
- Arcigy source cutover may proceed only after the parallel `kitchen_app` task has finished, an exact tested commit is named, Develop/Main isolation is proven and both no-write scrapes reconcile. The Odoo-only approval does not authorize this cutover.

Current read-only evidence from 2026-07-16:

- host root filesystem: 38 GB total, 16 GB used and 21 GB free (44% used);
- Docker images: 24 total, 13 active, 9.248 GB, of which Docker reports 7.916 GB reclaimable;
- dangling images: 0;
- Odoo database: 35 MB;
- PostgreSQL data volume: 105.7 MB;
- Odoo filestore volume: 2.6 MB;
- live volumes: `captain--geotherm-odoo-db-data` and `captain--geotherm-odoo-data`.

The earlier 97%-full condition has been remediated outside this Odoo change. No image cleanup is required for the current deployment. Docker's reclaimable estimate is not an approval to delete images: current and previous working images for every live service remain protected rollback evidence, and shared layers or aliases must be resolved by image ID before any future retention decision.

Do not run `docker system prune -a`. Image deletion requires an explicit reviewed list that preserves the current and previous working image for every live service.

## Backup commands

Run through the CapRover SSH path only after approval. The commands read credentials inside the database container and do not print them.

```bash
set -euo pipefail
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir="/root/arcigy-backups/geotherm-odoo-$stamp"
install -d -m 700 "$backup_dir"

db=$(docker ps --filter name=srv-captain--geotherm-odoo-db. -q | head -1)
odoo=$(docker ps --filter name=srv-captain--geotherm-odoo. -q | head -1)
test -n "$db" && test -n "$odoo"

docker exec "$db" sh -lc \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$backup_dir/database.dump"
docker exec "$odoo" tar -C /var/lib/odoo -czf - . \
  > "$backup_dir/filestore.tar.gz"

test -s "$backup_dir/database.dump"
test -s "$backup_dir/filestore.tar.gz"
docker exec -i "$db" pg_restore -l < "$backup_dir/database.dump" > /dev/null
tar -tzf "$backup_dir/filestore.tar.gz" > /dev/null
sha256sum "$backup_dir/database.dump" "$backup_dir/filestore.tar.gz" \
  > "$backup_dir/SHA256SUMS"
chmod 600 "$backup_dir"/*
```

Copy the verified backup off-host before the live deployment. A backup left only on the application host is not sufficient even while disk headroom is healthy.

## Controlled deployment order

1. Deploy the tested `kitchen_app` commit to `arcigy-kitchen-develop` only.
2. Add a unique `ARCIGY_METRICS_TOKEN` to Develop while preserving every existing env key.
3. Verify `/health`, `/ready` and authenticated `/metrics`; unauthenticated `/metrics` must remain hidden.
4. Perform two no-write metric scrapes and compare counters/latency with the source.
5. Back up Odoo database and filestore using the commands above.
6. Deploy the reconciled canonical Odoo package to `geotherm-odoo` with `ODOO_INIT=1` and `ODOO_INIT_MODULES=arcigy_saas_control_center` for the installation restart only.
7. Confirm registry load, set `ODOO_INIT=0`, and verify CRM, pricebook, analytics, monthly costs, Drive and Control Center menus.
8. Create the dedicated `SaaS Integration Bot` user and API key inside Odoo; keep the key only in the approved secret store.
9. Run the Develop-to-Odoo sync twice, confirm idempotency and inspect the Develop column while Main remains unchanged.
10. Only after the Develop and Odoo proof, repeat the runtime/token steps for Main and verify that Main updates never overwrite Develop.

Use the fail-closed selector during cutover so a proof run cannot scrape or write the other environment:

```bash
node integrations/saas_prometheus_sync.mjs --config=<path> --dry-run --environment=develop
node integrations/saas_prometheus_sync.mjs --config=<path> --environment=develop
node integrations/saas_prometheus_sync.mjs --config=<path> --dry-run --environment=main
node integrations/saas_prometheus_sync.mjs --config=<path> --environment=main
```

Omit `--dry-run` only after both no-write scrapes for that environment have been reconciled. Use `--environment=all` only after the independent Develop and Main cutovers are complete.

## Smoke checks

- Odoo `/web/login` and `/geotherm/api/v1/health` return expected responses.
- `arcigy_saas_control_center` is `installed` in the Odoo registry.
- All 24 dashboards load and every row contains both Develop and Main.
- Browser console has no addon errors or warnings.
- Existing Geotherm lead, pricebook, analytics, monthly cost and Drive views open.
- Drive OAuth callback and an existing lead attachment remain readable; do not create/delete Drive data during smoke.
- Metric sync rejects HTML, stale, malformed and unknown telemetry.
- Two consecutive ingests are idempotent and preserve environment isolation.

## Rollback

1. Stop the sync schedule first.
2. Restore the previous CapRover image/app definition while preserving env and volumes.
3. If only additive Control Center tables were created and existing workflows are healthy, leave them unused rather than deleting them.
4. Restore PostgreSQL and filestore only when the deployment mutated existing Geotherm data or rollback verification fails; this is a separate destructive decision.
5. Re-run CRM, pricebook, analytics, monthly costs, Drive and login smoke checks.
6. Record the failed release, evidence, root cause and follow-up action.

Never uninstall the addon, drop its tables, prune rollback images or delete backup files as part of an automatic rollback.
