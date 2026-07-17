import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

import {
  collectBackupRunEvidence,
  validateBackupRunEvidence,
} from "./saas_odoo_backup_rollup.mjs";
import { validateOperationalEvidence } from "./saas_operational_sync.mjs";

const options = {
  environment: "main",
  expectedAppService: "srv-captain--geotherm-odoo",
  expectedDbService: "srv-captain--geotherm-odoo-db",
  now: Date.parse("2026-07-17T12:00:00Z"),
};

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function rawEvidence(directory, backupId, artifact) {
  return {
    schema_version: 1,
    backup_id: backupId,
    started_at_utc: "2026-07-17T10:56:57.142Z",
    completed_at_utc: "2026-07-17T10:58:10.507Z",
    source_app_service: "srv-captain--geotherm-odoo",
    source_db_service: "srv-captain--geotherm-odoo-db",
    source_archive_sha256: "a".repeat(64),
    source_archive_size_bytes: 6807747,
    database_size_bytes: 4823476,
    filestore_size_bytes: 4025158,
    encryption: "CMS EnvelopedData AES-256-CBC",
    certificate_thumbprint: "A".repeat(40),
    encrypted_archive_sha256: sha256(artifact),
    encrypted_archive_size_bytes: artifact.length,
    off_host_path: join(directory, `${backupId}.tar.gz.p7m`),
    transfer_checksum_verified: true,
    structural_validation_passed: true,
    encryption_roundtrip_verified: true,
    remote_plaintext_removed: true,
    odoo_metric_write_performed: false,
    status: "success",
  };
}

async function fixture() {
  const directory = await mkdtemp(join(tmpdir(), "odoo-backup-rollup-"));
  const backupId = "geotherm-odoo-20260717T105657Z-bcc475";
  const artifact = Buffer.from("test-only-encrypted-archive");
  const evidence = rawEvidence(directory, backupId, artifact);
  await writeFile(join(directory, `${backupId}.tar.gz.p7m`), artifact);
  await writeFile(join(directory, `${backupId}.evidence.json`), JSON.stringify(evidence));
  return { directory, backupId, artifact, evidence };
}

test("compiles verified encrypted artifacts into privacy-safe incomplete Odoo evidence", async () => {
  const source = await fixture();
  const result = await collectBackupRunEvidence(source.directory, options);
  const normalized = validateOperationalEvidence(result, options.now);

  assert.equal(normalized.model, "saas.backup.run");
  assert.equal(normalized.environment, "main");
  assert.equal(normalized.items.length, 1);
  assert.equal(normalized.items[0].external_key, `main:backup:${source.backupId}`);
  assert.equal(normalized.items[0].backup_contract_complete, false);
  assert.equal(normalized.items[0].snapshot_count, 1);
  assert.equal(normalized.items[0].pitr_enabled, false);
  assert.equal(normalized.items[0].wal_archive_status, "not_applicable");
  assert.equal(normalized.items[0].secondary_copy_status, "healthy");
  assert.equal("certificate_thumbprint" in normalized.items[0], false);
  assert.equal("off_host_path" in normalized.items[0], false);
  assert.equal("storage_cost_monthly_eur" in normalized.items[0], false);
  assert.equal("failure_count_24h" in normalized.items[0], false);
});

test("rejects unknown fields, source drift and false verification claims", async () => {
  const source = await fixture();
  const unknown = structuredClone(source.evidence);
  unknown.raw_log = "must never enter Odoo";
  assert.throws(() => validateBackupRunEvidence(unknown, options), /unsupported fields: raw_log/);

  const wrongService = structuredClone(source.evidence);
  wrongService.source_db_service = "srv-captain--other-db";
  assert.throws(() => validateBackupRunEvidence(wrongService, options), /does not match the approved service/);

  const unverified = structuredClone(source.evidence);
  unverified.encryption_roundtrip_verified = false;
  assert.throws(() => validateBackupRunEvidence(unverified, options), /must be true/);
});

test("rejects missing, tampered, orphan and plaintext backup artifacts", async () => {
  const missing = await fixture();
  missing.evidence.off_host_path = join(missing.directory, "missing.tar.gz.p7m");
  assert.throws(() => validateBackupRunEvidence(missing.evidence, options), /does not match backup_id/);

  const tampered = await fixture();
  await writeFile(join(tampered.directory, `${tampered.backupId}.tar.gz.p7m`), "tampered");
  await assert.rejects(
    () => collectBackupRunEvidence(tampered.directory, options),
    /size does not match evidence/,
  );

  const orphan = await fixture();
  await writeFile(join(orphan.directory, "orphan.tar.gz.p7m"), "orphan");
  await assert.rejects(
    () => collectBackupRunEvidence(orphan.directory, options),
    /artifacts without validated evidence/,
  );

  const plaintext = await fixture();
  await writeFile(join(plaintext.directory, "retained.raw.tar.gz"), "plaintext");
  await assert.rejects(
    () => collectBackupRunEvidence(plaintext.directory, options),
    /retained plaintext archives/,
  );
});

test("is deterministic and keeps the full contract false when cost and attempt coverage are unknown", async () => {
  const source = await fixture();
  const first = await collectBackupRunEvidence(source.directory, options);
  const second = await collectBackupRunEvidence(source.directory, options);
  assert.deepEqual(first, second);
  assert.equal(first.items[0].backup_contract_complete, false);
  assert.equal("failure_count_24h" in first.items[0], false);
  assert.equal("storage_cost_monthly_eur" in first.items[0], false);

  const persisted = JSON.parse(
    await readFile(join(source.directory, `${source.backupId}.evidence.json`), "utf8"),
  );
  assert.equal(persisted.odoo_metric_write_performed, false);
});
