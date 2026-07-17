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

## Automated encrypted off-host backup

The approved Odoo control-plane automation uses three repository-owned scripts:

- `ops/backup/create-odoo-backup.sh` performs a fail-closed PostgreSQL, filestore and service-definition capture on the CapRover host;
- `ops/backup/odoo-backup-runner.ps1` transfers the structurally verified archive through strict-host-key SSH, verifies SHA-256, encrypts it with CMS/AES-256-CBC and proves a decrypt/checksum roundtrip before removing both plaintext copies;
- `ops/backup/decrypt-odoo-backup.ps1` requires explicit `-AllowPlaintextOutput`, evidence and both encrypted/source checksum checks before producing a mode-restricted restore archive.

The separate Windows task `Geotherm Odoo Encrypted Off-host Backup` runs daily at 04:15 Europe/Bratislava, starts when the workstation becomes available, ignores overlapping starts and fails below 5 GiB free space. Its certificate private key is non-exportable in the current-user Windows certificate store. Config, certificate reference, encrypted artifacts and evidence remain outside Git. It does not use or alter the `Arcigy Production Encrypted Backup` or `Arcigy Weekly Isolated Restore Verification` tasks and never writes backup claims into live Odoo.

Two approved 2026-07-17 runs succeeded, including one launched through the actual Scheduled Task. Backups `geotherm-odoo-20260717T105657Z-bcc475` and `geotherm-odoo-20260717T105825Z-b0cead` each passed source structure, transfer SHA-256, CMS AES-256 cipher inspection and decryption roundtrip. Each source archive was 6,807,746-6,807,747 bytes and each encrypted artifact was 12,421,810 bytes. The destination contained two encrypted artifacts and two evidence files, zero raw archives; server transfer/work directories were empty; the task result was `0` with the next run scheduled for 2026-07-18 04:15. Live app/database stayed image 36 at 1/1.

The independent `integrations/saas_odoo_backup_rollup.mjs` compiler now converts those artifacts into bounded `saas.backup.run` evidence only after re-reading and hashing every physical encrypted file. The approved real run reconciled two evidence files to two `.p7m` files, emitted two Main records, removed local paths and certificate metadata, and passed `saas_operational_sync.mjs --dry-run` twice. It deliberately emitted `backup_contract_complete=false`: the current success-only files do not prove a complete 24-hour attempt population and no storage-cost allocation has been approved. No API key or Odoo row was created; live `saas.backup.run` and `res.users.apikeys` both remained zero after the secret-handling boundary stopped automated temporary-key transfer.

The backup runner additionally writes one bounded `.attempt.json` record for each
completed attempt. It records only the generated backup ID, timestamps, exact
Odoo service identities, final `success`/`failed` status and a symbolic
failure class. It never records command output, a path, certificate metadata,
credentials or an Odoo key. The compiler accepts a failed attempt as an
incomplete `saas.backup.run` record so it can open the normal Odoo operational
path, but it refuses to invent a 24-hour failure count or a complete backup
contract until an independently complete attempt population and an approved
storage-cost allocation exist.

`ops/backup/odoo-backup-evidence-runner.ps1` and `install-odoo-backup-evidence-task.ps1` provide a separate daily 04:30 compile/dry-run task. Its config has no credential, its output must remain a `.local.json` file inside the approved backup directory, and the runner cannot omit `--dry-run`. Keep this task independent from both the encrypted backup task and every Arcigy task.

The Windows task `Geotherm Odoo Backup Evidence Compile` was installed on 2026-07-17 and then exercised through Task Scheduler. It finished with result `0`, validated two Main `saas.backup.run` records, and scheduled its next run for 2026-07-18 04:30 Europe/Bratislava. The config and generated evidence ACL each grant only the current Windows user full control. The encrypted Odoo backup task and both inspected Arcigy backup/restore tasks retained their original executable, arguments, trigger and principal.

### Credential-backed backup evidence ingest

The live stage is deliberately separate. Create a dedicated `saas_integration_bot` Odoo user with only `arcigy_saas_control_center.group_saas_integration_bot`, generate an `rpc` API key with a maximum 30-day lifetime and store it under the approved `Arcigy/GeothermOdoo/...` Windows Credential Manager namespace with `ops/backup/set-odoo-ingest-credential.ps1`. Never place the key in a command argument, file, task definition, JSON evidence, log, Git, clipboard transcript or chat.

Install `Geotherm Odoo Backup Evidence Ingest` with `ops/backup/install-odoo-backup-ingest-task.ps1`. It runs at 04:40 Europe/Bratislava using a limited interactive principal. Its external `.env` contains only `ODOO_INGEST_EVIDENCE_CONFIG_PATH` and `ODOO_INGEST_CREDENTIAL_TARGET`. Every run invokes the 04:30 evidence runner again, requires a freshly regenerated `.local.json`, reads the key from Credential Manager into process memory, executes the allowlisted idempotent `saas.backup.run` ingest, checks the exact Main record count and removes `ARCIGY_ODOO_API_KEY` in `finally`.

Fail closed if the credential is absent, expired, belongs to another user, has the wrong shape, the dry-run fails, evidence is stale, the environment is not Main or the returned model/count differs. Prove retry safety by running the task twice and confirming the same external keys update the same rows. Rotation must first create a new bounded key, replace the exact Credential Manager target with explicit `-Force`, run and verify one ingest, then revoke the previous named key. Do not delete backup artifacts or enable retention as part of rotation or failure recovery.

This proves an automated off-host copy for Odoo, not the Arcigy Develop/Main SaaS backup producer. The task uses an interactive Windows principal with `StartWhenAvailable`; a powered-off or logged-out workstation delays the copy. Automatic retention deletion remains disabled until an explicit retention and certificate-recovery policy is approved. At the current observed size, storage grows by about 12.4 MB per successful daily artifact.

## Isolated restore drill

Restore proof must never attach production database or Odoo volumes. Use unique temporary resource names, a Docker `--internal` network, random temporary database credentials, no published ports and the exact tested application/database images. Disable cron in the restored Odoo process and verify login plus the authenticated health endpoint from inside the isolated application container.

Record all of the following without emitting secrets or row contents:

- source archive and component checksum verification;
- successful PostgreSQL restore, filestore extraction and required-addon registry state;
- exact hashes for stable critical tables such as pricing, CRM, Drive, users, attachments, environments and metric definitions;
- counts rather than false bitwise equality for `saas.metric.current` and `saas.metric.timeseries`, which may legitimately change after the backup snapshot;
- restored filestore count, application smoke, internal network, unpublished ports and absence of production mounts;
- RTO from restore start to healthy application smoke, and conservative RPO as the backup age at successful recovery.

Cleanup must remove containers with `docker rm -fv` because the PostgreSQL and Odoo base images declare anonymous volumes. Then remove only the explicitly named temporary filestore volume and network, and prove zero matching container, network and volume residue. Never use a broad prune.

The approved 2026-07-17 drill restored post-deploy backup `geotherm-odoo-20260717T082027Z` with image 36. It passed ten exact stable-table hashes, `current=2`, `history=30`, 26 filestore files, all three required addons, application smoke and tenant isolation. Measured RTO was 33 seconds and conservative RPO was 1,198 seconds. Production remained 1/1 and no Arcigy source, credential or schedule was changed.

## Isolated image rollback drill

Application rollback proof must reuse an independently verified restored database and filestore on a unique internal network. Start the current image first, confirm login and authorized health, remove only that isolated application container with its anonymous volumes, then start the previous protected image against the same isolated restore. Verify required module registry state and preserved business/metric counts before returning to the current image. Keep cron disabled, publish no ports and never attach production mounts.

The approved 2026-07-17 drill used backup `geotherm-odoo-20260717T082027Z` and proved image 36 -> image 35 -> image 36. Current-image login and authorized health returned 200 in 4 seconds; rollback image 35 returned both in 5 seconds; the return to image 36 returned both in 4 seconds. Five monthly costs, one pricebook catalog, 88 products, 12 add-ons, two environments, 24 dashboards, 376 metric definitions, two current rows, 30 history rows and all three required addons remained exact. The test used random credentials, an internal network and zero published ports. Cleanup left zero matching containers, networks or volumes; live application and database services remained image 36 at 1/1.

## Isolated DR failover drill

DR proof must use two fully separate restored stacks: unique primary and standby PostgreSQL volumes, unique primary and standby Odoo filestore volumes, and separate internal networks. Prepare and validate the standby before declaring the simulated primary outage. Measure failover RTO from primary shutdown to standby application health. Report conservative RPO as the age of the restored backup at successful failover. Do not publish ports, attach production mounts, modify DNS or write synthetic evidence into live Odoo.

The approved 2026-07-17 drill prepared both stacks from backup `geotherm-odoo-20260717T082027Z`, verified the primary login/health and exact source counts, stopped the primary application and database, then started image 36 against the separate standby. Standby login and authorized health returned 200 in 6 seconds. Five monthly costs, one catalog, 88 products, 12 add-ons, two environments, 24 dashboards, 376 definitions, two current rows, 30 history rows and all three required addons remained exact. Conservative backup-age RPO was 7,612 seconds. Cleanup left zero matching containers, networks or volumes; live application and database services remained image 36 at 1/1.

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

### Signed-in browser evidence

On 2026-07-17 an authenticated Chrome session loaded the live Control Center at
`/odoo/action-246`. The smoke selected all 23 required dashboards plus the
optional AI/LLM dashboard. For each of the 24 options it waited for the exact
dashboard heading and confirmed exactly one Develop and one Main table column;
all 24 passed. The earlier read-only `Obnoviť` action also passed, and the final
browser console contained zero errors or warnings. Operations, Metrics,
Aggregates and Configuration navigation was visible to the signed-in
administrator. The tab was returned to Founder/CEO. No form, record, credential
or source value was created, edited or deleted.

### Documentation-only image 37 deployment

PR `#13` merged commit `f2abc6194c4a84c5a193725b8e2c3e18b4bdad19` after the
required pull-request validation. Main run `29586725340` repeated addon-source,
127-contract, isolated PostgreSQL/Odoo 19 and immutable-archive checks before the
automatic CapRover deployment. The deployed archive contained documentation
changes only. Image 37 reached 1/1 alongside the unchanged database service;
`/web/login` returned 200 and the protected health endpoint returned the expected
unauthenticated 401. Image tags 36 and 35 remain available as rollback evidence,
and the host still has 19 GB free. No Arcigy credential, schedule, source write,
database, storage or `kitchen_app` change was made.

### Live Odoo backup-evidence ingest

On 2026-07-17, the dedicated Odoo service user `saas_integration_bot` was
created with only the Control Center Integration Bot role. Its short-lived RPC
key expires on 2026-08-16 and is stored only in Windows Credential Manager at
`Arcigy/GeothermOdoo/SaaSIntegrationBot`; it is not present in this repository,
a task definition, command output or configuration file.

The dedicated `Geotherm Odoo Backup Evidence Ingest` task runs daily at 04:40
under the interactive user context. It has a user-only, secret-free
configuration file and leaves the existing Arcigy and Odoo backup tasks
unchanged. Two direct runs and one Task Scheduler run (`LastTaskResult=0`)
validated the encrypted off-host backup evidence again before the allowlisted,
idempotent Main-only Odoo write. The live Backups list contains exactly four
successful encrypted off-host Odoo backup records; retries did not duplicate
them. A subsequent Odoo-native refresh derived three truthful backup metrics
from that evidence. Restore, DR, load, retention, 24-hour failure coverage and
storage-cost evidence remain separate incomplete requirements.

The first ledger-enabled normal backup completed successfully on 2026-07-17.
Its 420-byte attempt record contained the generated backup ID, timestamps,
approved services, `success`, JSON `null` failure class and no metric-write
claim. The compiler dry-run validated four Main records; the following existing
ingest task completed with result `0` and updated the same bounded set without
creating a fifth duplicate record.

## Rollback

1. Stop the sync schedule first.
2. Restore the previous CapRover image/app definition while preserving env and volumes.
3. If only additive Control Center tables were created and existing workflows are healthy, leave them unused rather than deleting them.
4. Restore PostgreSQL and filestore only when the deployment mutated existing Geotherm data or rollback verification fails; this is a separate destructive decision.
5. Re-run CRM, pricebook, analytics, monthly costs, Drive and login smoke checks.
6. Record the failed release, evidence, root cause and follow-up action.

Never uninstall the addon, drop its tables, prune rollback images or delete backup files as part of an automatic rollback.
