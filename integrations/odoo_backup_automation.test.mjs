import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const remoteScript = await readFile(new URL("../ops/backup/create-odoo-backup.sh", import.meta.url), "utf8");
const runner = await readFile(new URL("../ops/backup/odoo-backup-runner.ps1", import.meta.url), "utf8");
const installer = await readFile(new URL("../ops/backup/install-odoo-backup-task.ps1", import.meta.url), "utf8");
const decryptor = await readFile(new URL("../ops/backup/decrypt-odoo-backup.ps1", import.meta.url), "utf8");
const evidenceRunner = await readFile(new URL("../ops/backup/odoo-backup-evidence-runner.ps1", import.meta.url), "utf8");
const evidenceInstaller = await readFile(new URL("../ops/backup/install-odoo-backup-evidence-task.ps1", import.meta.url), "utf8");
const credentialSetter = await readFile(new URL("../ops/backup/set-odoo-ingest-credential.ps1", import.meta.url), "utf8");
const ingestRunner = await readFile(new URL("../ops/backup/odoo-backup-ingest-runner.ps1", import.meta.url), "utf8");
const ingestInstaller = await readFile(new URL("../ops/backup/install-odoo-backup-ingest-task.ps1", import.meta.url), "utf8");

test("remote Odoo backup is fail-closed, structurally verified and bounded to exact services", () => {
  assert.match(remoteScript, /set -Eeuo pipefail/);
  assert.match(remoteScript, /umask 077/);
  assert.match(remoteScript, /backup_id="\$\{1:-\}"/);
  assert.match(remoteScript, /mode="cleanup"/);
  assert.match(remoteScript, /result_path/);
  assert.match(remoteScript, /lock_dir/);
  assert.match(remoteScript, /srv-captain--geotherm-odoo/);
  assert.match(remoteScript, /srv-captain--geotherm-odoo-db/);
  assert.match(remoteScript, /pg_dump/);
  assert.match(remoteScript, /pg_restore -l/);
  assert.match(remoteScript, /\/var\/lib\/odoo/);
  assert.match(remoteScript, /service-definitions\.json/);
  assert.match(remoteScript, /sha256sum/);
  assert.doesNotMatch(remoteScript, /docker system prune|docker volume prune|DROP DATABASE|rm -rf \/root\/arcigy-backups/);
});

test("off-host runner uses strict SSH, AES-256 CMS and deletes plaintext only after verification", () => {
  assert.match(runner, /StrictHostKeyChecking=yes/);
  assert.match(runner, /"-q"/);
  assert.match(runner, /UserKnownHostsFile=/);
  assert.match(runner, /Protect-CmsMessage/);
  assert.match(runner, /Unprotect-CmsMessage/);
  assert.match(runner, /Confirm-Aes256CmsCipher/);
  assert.match(runner, /Encrypted backup is not CMS AES-256-CBC/);
  assert.match(runner, /Confirm-EncryptedRoundtrip/);
  assert.match(runner, /Get-FileHash.+SHA256/s);
  assert.match(runner, /Remote archive path is outside the approved transfer directory/);
  assert.match(runner, /Remote backup returned a duplicate result key/);
  assert.match(runner, /Remote backup ID does not match the requested run/);
  assert.match(runner, /-Attempts 3/);
  assert.match(runner, /--cleanup \$requestedBackupId/);
  assert.match(runner, /remote_plaintext_removed = \$true/);
  assert.match(runner, /odoo_metric_write_performed = \$false/);
  assert.doesNotMatch(runner, /kitchen_app|ARCIGY_ODOO_API_KEY|saas\.backup\.run|Invoke-RestMethod/);
  assert.doesNotMatch(runner, /Remove-Item.+-Recurse|docker system prune|docker volume prune/);
});

test("installer creates a separate non-exportable daily Odoo task without modifying Arcigy tasks", () => {
  assert.match(installer, /Geotherm Odoo Encrypted Off-host Backup/);
  assert.match(installer, /New-SelfSignedCertificate/);
  assert.match(installer, /KeyExportPolicy\s*=\s*"NonExportable"/);
  assert.match(installer, /New-ScheduledTaskTrigger -Daily/);
  assert.match(installer, /StartWhenAvailable/);
  assert.match(installer, /MultipleInstances\s*=\s*"IgnoreNew"/);
  assert.doesNotMatch(installer, /Arcigy Production Encrypted Backup|Arcigy Weekly Isolated Restore Verification|Unregister-ScheduledTask/);
});

test("restore decryptor requires explicit plaintext approval and verifies both checksums", () => {
  assert.match(decryptor, /AllowPlaintextOutput/);
  assert.match(decryptor, /Encrypted archive checksum mismatch/);
  assert.match(decryptor, /Decrypted archive checksum mismatch/);
  assert.match(decryptor, /Unprotect-CmsMessage/);
  assert.match(decryptor, /tar\.exe -tzf/);
  assert.match(decryptor, /inheritance:r/);
  assert.doesNotMatch(decryptor, /Remove-Item.+-Recurse|Unregister-ScheduledTask|kitchen_app/);
});

test("evidence task compiles and validates backups without a secret or live Odoo write", () => {
  assert.match(evidenceRunner, /saas_odoo_backup_rollup\.mjs/);
  assert.match(evidenceRunner, /saas_operational_sync\.mjs/);
  assert.match(evidenceRunner, /"--dry-run"/);
  assert.match(evidenceRunner, /mode=dry-run/);
  assert.match(evidenceRunner, /output must stay inside the approved backup directory/);
  assert.match(evidenceRunner, /icacls\.exe/);
  assert.doesNotMatch(evidenceRunner, /ARCIGY_ODOO_API_KEY|Authorization|Bearer|Invoke-RestMethod|kitchen_app/);

  assert.match(evidenceInstaller, /Geotherm Odoo Backup Evidence Compile/);
  assert.match(evidenceInstaller, /New-ScheduledTaskTrigger -Daily/);
  assert.match(evidenceInstaller, /StartWhenAvailable/);
  assert.match(evidenceInstaller, /MultipleInstances IgnoreNew/);
  assert.match(evidenceInstaller, /Does not write Odoo or alter Arcigy tasks/);
  assert.doesNotMatch(
    evidenceInstaller,
    /ARCIGY_ODOO_API_KEY|Arcigy Production Encrypted Backup|Arcigy Weekly Isolated Restore Verification|Unregister-ScheduledTask/,
  );
});

test("live backup ingest keeps the Odoo key in Windows Credential Manager only", () => {
  assert.match(credentialSetter, /\[Security\.SecureString\]\$ApiKey/);
  assert.match(credentialSetter, /CredWriteW/);
  assert.match(credentialSetter, /SecureStringToCoTaskMemUnicode/);
  assert.match(credentialSetter, /ZeroFreeCoTaskMemUnicode/);
  assert.match(credentialSetter, /Credential already exists; use -Force only during an approved rotation/);
  assert.doesNotMatch(credentialSetter, /cmdkey|ConvertFrom-SecureString|Set-Content|WriteAllText/);

  assert.match(ingestRunner, /CredReadW/);
  assert.match(ingestRunner, /saas_integration_bot/);
  assert.match(ingestRunner, /odoo-backup-evidence-runner\.ps1/);
  assert.match(ingestRunner, /freshly regenerated before live ingest/);
  assert.match(ingestRunner, /\$env:ARCIGY_ODOO_API_KEY = \$apiKey/);
  assert.match(ingestRunner, /Remove-Item Env:ARCIGY_ODOO_API_KEY/);
  assert.match(ingestRunner, /saas\.backup\.run/);
  assert.match(ingestRunner, /mode=live/);
  assert.doesNotMatch(ingestRunner, /Write-Output.+apiKey|Write-Host.+apiKey|kitchen_app/);
});

test("live backup ingest is a separate delayed task with secret-free configuration", () => {
  assert.match(ingestInstaller, /Geotherm Odoo Backup Evidence Ingest/);
  assert.match(ingestInstaller, /AddHours\(4\)\.AddMinutes\(40\)/);
  assert.match(ingestInstaller, /New-ScheduledTaskTrigger -Daily/);
  assert.match(ingestInstaller, /StartWhenAvailable/);
  assert.match(ingestInstaller, /MultipleInstances IgnoreNew/);
  assert.match(ingestInstaller, /RunLevel Limited/);
  assert.match(ingestInstaller, /configuration must stay outside the repository/);
  assert.match(ingestInstaller, /ODOO_INGEST_CREDENTIAL_TARGET/);
  assert.doesNotMatch(
    ingestInstaller,
    /ARCIGY_ODOO_API_KEY=|Arcigy Production Encrypted Backup|Arcigy Weekly Isolated Restore Verification|Unregister-ScheduledTask|kitchen_app/,
  );
});
