import { createHash } from "node:crypto";
import { createReadStream } from "node:fs";
import { lstat, readFile, readdir, rename, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const MAX_EVIDENCE_BYTES = 1024 * 1024;
const MAX_EVIDENCE_FILES = 400;
const MAX_ENCRYPTED_ARCHIVE_BYTES = 10 * 1024 * 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SAFE_SERVICE = /^srv-captain--[a-z0-9][a-z0-9-]{0,100}$/;
const SAFE_BACKUP_ID = /^[A-Za-z0-9][A-Za-z0-9.-]{0,127}$/;
const SHA256 = /^[0-9a-f]{64}$/;
const CERTIFICATE_THUMBPRINT = /^[0-9A-F]{40}$/;

const INPUT_FIELDS = new Set([
  "schema_version",
  "backup_id",
  "started_at_utc",
  "completed_at_utc",
  "source_app_service",
  "source_db_service",
  "source_archive_sha256",
  "source_archive_size_bytes",
  "database_size_bytes",
  "filestore_size_bytes",
  "encryption",
  "certificate_thumbprint",
  "encrypted_archive_sha256",
  "encrypted_archive_size_bytes",
  "off_host_path",
  "transfer_checksum_verified",
  "structural_validation_passed",
  "encryption_roundtrip_verified",
  "remote_plaintext_removed",
  "odoo_metric_write_performed",
  "status",
]);

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${name} must be an object.`);
  }
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function requiredString(value, name, pattern) {
  if (typeof value !== "string" || !value.trim() || (pattern && !pattern.test(value))) {
    throw new Error(`${name} is invalid.`);
  }
  return value;
}

function positiveInteger(value, name) {
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${name} must be a positive safe integer.`);
  return value;
}

function requiredBoolean(value, expected, name) {
  if (value !== expected) throw new Error(`${name} must be ${expected}.`);
  return value;
}

function validDate(value, name) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) throw new Error(`${name} must be an ISO-8601 timestamp.`);
  return new Date(timestamp).toISOString();
}

function normalizeOptions(options = {}) {
  const environment = String(options.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("environment must be develop or main.");
  const expectedAppService = requiredString(options.expectedAppService, "expectedAppService", SAFE_SERVICE);
  const expectedDbService = requiredString(options.expectedDbService, "expectedDbService", SAFE_SERVICE);
  const now = options.now === undefined ? Date.now() : Number(options.now);
  if (!Number.isFinite(now)) throw new Error("now must be a finite timestamp.");
  return { environment, expectedAppService, expectedDbService, now };
}

export function validateBackupRunEvidence(raw, options = {}) {
  const normalizedOptions = normalizeOptions(options);
  const evidence = plainObject(raw, "backup evidence");
  rejectUnknownKeys(evidence, INPUT_FIELDS, "backup evidence");
  if (evidence.schema_version !== 1) throw new Error("backup evidence schema_version must be 1.");

  const backupId = requiredString(evidence.backup_id, "backup evidence backup_id", SAFE_BACKUP_ID);
  const startedAt = validDate(evidence.started_at_utc, "backup evidence started_at_utc");
  const completedAt = validDate(evidence.completed_at_utc, "backup evidence completed_at_utc");
  if (Date.parse(completedAt) <= Date.parse(startedAt)) {
    throw new Error("backup evidence completed_at_utc must be after started_at_utc.");
  }
  if (Date.parse(completedAt) > normalizedOptions.now + 5 * 60_000) {
    throw new Error("backup evidence completed_at_utc is too far in the future.");
  }
  if (evidence.source_app_service !== normalizedOptions.expectedAppService) {
    throw new Error("backup evidence source_app_service does not match the approved service.");
  }
  if (evidence.source_db_service !== normalizedOptions.expectedDbService) {
    throw new Error("backup evidence source_db_service does not match the approved service.");
  }
  if (evidence.status !== "success") throw new Error("backup evidence status must be success.");
  if (evidence.encryption !== "CMS EnvelopedData AES-256-CBC") {
    throw new Error("backup evidence encryption must be CMS EnvelopedData AES-256-CBC.");
  }

  const sourceArchiveSha256 = requiredString(
    evidence.source_archive_sha256,
    "backup evidence source_archive_sha256",
    SHA256,
  );
  const encryptedArchiveSha256 = requiredString(
    evidence.encrypted_archive_sha256,
    "backup evidence encrypted_archive_sha256",
    SHA256,
  );
  requiredString(
    evidence.certificate_thumbprint,
    "backup evidence certificate_thumbprint",
    CERTIFICATE_THUMBPRINT,
  );
  const offHostPath = requiredString(evidence.off_host_path, "backup evidence off_host_path");
  const expectedArtifactName = `${backupId}.tar.gz.p7m`;
  if (basename(offHostPath) !== expectedArtifactName) {
    throw new Error("backup evidence off_host_path does not match backup_id.");
  }

  requiredBoolean(evidence.transfer_checksum_verified, true, "backup evidence transfer_checksum_verified");
  requiredBoolean(evidence.structural_validation_passed, true, "backup evidence structural_validation_passed");
  requiredBoolean(evidence.encryption_roundtrip_verified, true, "backup evidence encryption_roundtrip_verified");
  requiredBoolean(evidence.remote_plaintext_removed, true, "backup evidence remote_plaintext_removed");
  requiredBoolean(evidence.odoo_metric_write_performed, false, "backup evidence odoo_metric_write_performed");

  return {
    backupId,
    startedAt,
    completedAt,
    sourceArchiveSha256,
    sourceArchiveSizeBytes: positiveInteger(
      evidence.source_archive_size_bytes,
      "backup evidence source_archive_size_bytes",
    ),
    databaseSizeBytes: positiveInteger(evidence.database_size_bytes, "backup evidence database_size_bytes"),
    filestoreSizeBytes: positiveInteger(evidence.filestore_size_bytes, "backup evidence filestore_size_bytes"),
    encryptedArchiveSha256,
    encryptedArchiveSizeBytes: positiveInteger(
      evidence.encrypted_archive_size_bytes,
      "backup evidence encrypted_archive_size_bytes",
    ),
    offHostPath: resolve(offHostPath),
    expectedArtifactName,
    environment: normalizedOptions.environment,
  };
}

async function readBoundedJson(path) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) throw new Error(`Evidence must be a regular file: ${basename(path)}.`);
  if (info.size <= 0 || info.size > MAX_EVIDENCE_BYTES) {
    throw new Error(`Evidence file is empty or exceeds ${MAX_EVIDENCE_BYTES} bytes: ${basename(path)}.`);
  }
  return JSON.parse(await readFile(path, "utf8"));
}

async function sha256File(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

function compileItems(runs, environment, snapshotCount) {
  return runs
    .slice()
    .sort((left, right) => Date.parse(left.startedAt) - Date.parse(right.startedAt))
    .map((run) => ({
      external_key: `${environment}:backup:${run.backupId}`,
      name: "Encrypted off-host Odoo backup",
      started_at: run.startedAt,
      finished_at: run.completedAt,
      status: "success",
      backup_type: "full",
      size_bytes: run.sourceArchiveSizeBytes,
      checksum: `sha256:${run.sourceArchiveSha256}`,
      encrypted: true,
      off_host: true,
      backup_contract_complete: false,
      snapshot_count: snapshotCount,
      pitr_enabled: false,
      pitr_window_seconds: 0,
      wal_archive_status: "not_applicable",
      secondary_copy_status: "healthy",
    }));
}

export async function collectBackupRunEvidence(inputDirectory, options = {}) {
  const normalizedOptions = normalizeOptions(options);
  const directory = resolve(requiredString(inputDirectory, "inputDirectory"));
  const directoryInfo = await lstat(directory);
  if (!directoryInfo.isDirectory() || directoryInfo.isSymbolicLink()) {
    throw new Error("inputDirectory must be a regular directory.");
  }

  const entries = await readdir(directory, { withFileTypes: true });
  const plaintext = entries.filter(
    (entry) => entry.isFile() && (/\.raw\.tar\.gz$/i.test(entry.name) || /\.tar\.gz$/i.test(entry.name)),
  );
  if (plaintext.length) throw new Error("Backup directory contains retained plaintext archives.");

  const evidenceEntries = entries
    .filter((entry) => entry.isFile() && entry.name.endsWith(".evidence.json"))
    .sort((left, right) => left.name.localeCompare(right.name));
  if (!evidenceEntries.length) throw new Error("Backup directory contains no evidence files.");
  if (evidenceEntries.length > MAX_EVIDENCE_FILES) {
    throw new Error(`Backup directory exceeds ${MAX_EVIDENCE_FILES} evidence files.`);
  }

  const artifactEntries = entries.filter((entry) => entry.isFile() && entry.name.endsWith(".tar.gz.p7m"));
  const artifactNames = new Set(artifactEntries.map((entry) => entry.name));
  if (artifactNames.size !== artifactEntries.length) throw new Error("Backup directory contains duplicate artifact names.");

  const runs = [];
  const seenIds = new Set();
  for (const entry of evidenceEntries) {
    const evidencePath = resolve(directory, entry.name);
    const run = validateBackupRunEvidence(await readBoundedJson(evidencePath), normalizedOptions);
    if (seenIds.has(run.backupId)) throw new Error(`Duplicate backup evidence ID: ${run.backupId}.`);
    seenIds.add(run.backupId);
    if (dirname(run.offHostPath) !== directory) {
      throw new Error(`Backup artifact path leaves the approved directory: ${run.backupId}.`);
    }
    if (!artifactNames.has(run.expectedArtifactName)) {
      throw new Error(`Backup artifact is missing: ${run.backupId}.`);
    }
    const artifactPath = resolve(directory, run.expectedArtifactName);
    const artifactInfo = await lstat(artifactPath);
    if (!artifactInfo.isFile() || artifactInfo.isSymbolicLink()) {
      throw new Error(`Backup artifact must be a regular file: ${run.backupId}.`);
    }
    if (artifactInfo.size <= 0 || artifactInfo.size > MAX_ENCRYPTED_ARCHIVE_BYTES) {
      throw new Error(`Backup artifact size is outside the approved bound: ${run.backupId}.`);
    }
    if (artifactInfo.size !== run.encryptedArchiveSizeBytes) {
      throw new Error(`Backup artifact size does not match evidence: ${run.backupId}.`);
    }
    if ((await sha256File(artifactPath)) !== run.encryptedArchiveSha256) {
      throw new Error(`Backup artifact SHA-256 does not match evidence: ${run.backupId}.`);
    }
    runs.push(run);
  }

  const expectedArtifacts = new Set(runs.map((run) => run.expectedArtifactName));
  const orphanArtifacts = [...artifactNames].filter((name) => !expectedArtifacts.has(name));
  if (orphanArtifacts.length) throw new Error("Backup directory contains artifacts without validated evidence.");

  const sourceUpdatedAt = runs
    .map((run) => run.completedAt)
    .sort((left, right) => Date.parse(right) - Date.parse(left))[0];
  return {
    model: "saas.backup.run",
    environment: normalizedOptions.environment,
    source_updated_at: sourceUpdatedAt,
    items: compileItems(runs, normalizedOptions.environment, expectedArtifacts.size),
  };
}

function parseCli(argv) {
  const allowed = new Set(["input-dir", "environment", "app-service", "db-service", "output"]);
  const parsed = {};
  for (const argument of argv) {
    const match = /^--([a-z-]+)=(.+)$/.exec(argument);
    if (!match || !allowed.has(match[1])) throw new Error(`Unsupported argument: ${argument}.`);
    if (parsed[match[1]] !== undefined) throw new Error(`Duplicate argument: --${match[1]}.`);
    parsed[match[1]] = match[2];
  }
  for (const name of ["input-dir", "environment", "app-service", "db-service"]) {
    if (!parsed[name]) throw new Error(`--${name}=... is required.`);
  }
  return parsed;
}

async function writeAtomic(path, content) {
  const outputPath = resolve(path);
  const temporaryPath = `${outputPath}.${Date.now()}.tmp`;
  try {
    await writeFile(temporaryPath, content, { encoding: "utf8", flag: "wx", mode: 0o600 });
    await rename(temporaryPath, outputPath);
  } catch (error) {
    await unlink(temporaryPath).catch(() => {});
    throw error;
  }
  return outputPath;
}

async function main() {
  const args = parseCli(process.argv.slice(2));
  const evidence = await collectBackupRunEvidence(args["input-dir"], {
    environment: args.environment,
    expectedAppService: args["app-service"],
    expectedDbService: args["db-service"],
  });
  const serialized = `${JSON.stringify(evidence, null, 2)}\n`;
  if (args.output) {
    const outputPath = await writeAtomic(args.output, serialized);
    console.log(JSON.stringify({ ok: true, output: outputPath, records: evidence.items.length }));
  } else {
    process.stdout.write(serialized);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
