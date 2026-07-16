import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_RESPONSE_BYTES = 1024 * 1024;
const MAX_INPUT_BYTES = 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SAFE_EXTERNAL_KEY = /^[A-Za-z0-9._:-]{1,255}$/;
const SECRET_ENV_NAME = /^ARCIGY_[A-Z0-9_]+$/;

const stringField = (required = false, maximum = 1024) => ({ type: "string", required, maximum });
const dateField = (required = false) => ({ type: "datetime", required });
const numberField = (integer = false, minimum = 0) => ({
  type: integer ? "integer" : "number",
  minimum,
});
const booleanField = () => ({ type: "boolean" });
const urlField = () => ({ type: "url" });
const selectionField = (values, required = false) => ({ type: "selection", values, required });

const OPERATIONAL_MODELS = Object.freeze({
  "saas.backup.run": {
    fields: {
      name: stringField(true),
      started_at: dateField(true),
      finished_at: dateField(),
      status: selectionField(["running", "success", "failed"], true),
      backup_type: selectionField(["full", "incremental", "pitr"], true),
      size_bytes: numberField(true),
      checksum: stringField(),
      encrypted: booleanField(),
      off_host: booleanField(),
      drilldown_url: urlField(),
    },
  },
  "saas.restore.test": {
    fields: {
      name: stringField(true),
      started_at: dateField(true),
      finished_at: dateField(),
      status: selectionField(["running", "success", "failed"], true),
      actual_rpo_seconds: numberField(true),
      actual_rto_seconds: numberField(true),
      rpo_measured: booleanField(),
      rto_measured: booleanField(),
      checksum_valid: booleanField(),
      application_smoke_passed: booleanField(),
      tenant_isolation_passed: booleanField(),
      evidence_url: urlField(),
    },
  },
  "saas.load.test": {
    fields: {
      name: stringField(true),
      started_at: dateField(true),
      finished_at: dateField(),
      test_type: selectionField(
        ["baseline", "ramp", "hold", "spike", "stress", "soak", "failure"],
        true,
      ),
      status: selectionField(["ready", "ready_with_risk", "not_ready", "test_stale"], true),
      concurrent_users: numberField(true),
      requests_per_second: numberField(),
      p95_seconds: numberField(),
      p99_seconds: numberField(),
      error_rate: numberField(),
      recovery_seconds: numberField(),
      representative: booleanField(),
      architecture_version: stringField(false, 128),
      evidence_url: urlField(),
    },
  },
  "saas.data.quality.run": {
    fields: {
      name: stringField(true),
      started_at: dateField(true),
      finished_at: dateField(),
      status: selectionField(["valid", "warning", "invalid"], true),
      events_sent: numberField(true),
      events_received: numberField(true),
      events_processed: numberField(true),
      events_rejected: numberField(true),
      event_stream_complete: booleanField(),
      retry_adjustment_count: numberField(true),
      duplicate_count: numberField(true),
      schema_failure_count: numberField(true),
      missing_field_count: numberField(true),
      late_event_count: numberField(true),
      unknown_tenant_count: numberField(true),
      reconciliation_difference: numberField(false, Number.NEGATIVE_INFINITY),
      oldest_unsynced_at: dateField(),
      drilldown_url: urlField(),
    },
  },
  "saas.sync.run": {
    method: "ingest_sync_run_batch",
    fields: {
      name: stringField(true),
      started_at: dateField(true),
      finished_at: dateField(),
      status: selectionField(["running", "success", "partial", "failed"], true),
      sync_contract_complete: booleanField(),
      records_read: numberField(true),
      records_created: numberField(true),
      records_updated: numberField(true),
      records_skipped: numberField(true),
      records_rejected: numberField(true),
      duplicate_upsert_count: numberField(true),
      api_error_count: numberField(true),
      authentication_error_count: numberField(true),
      permission_error_count: numberField(true),
      rate_limit_error_count: numberField(true),
      retry_count: numberField(true),
      backlog_count: numberField(true),
      oldest_unsynced_at: dateField(),
      error_code: stringField(false, 64),
      drilldown_url: urlField(),
    },
  },
});

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

function normalizedUrl(value, name) {
  const url = new URL(String(value || ""));
  if (url.username || url.password) throw new Error(`${name} must not contain credentials.`);
  const loopback = url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  if (url.protocol !== "https:" && !loopback) throw new Error(`${name} must use HTTPS except on loopback.`);
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

function validDate(value, name) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) throw new Error(`${name} must be an ISO-8601 timestamp.`);
  return new Date(timestamp).toISOString();
}

function validateField(value, descriptor, name) {
  if (value === undefined || value === null || value === "") {
    if (descriptor.required) throw new Error(`${name} is required.`);
    return undefined;
  }
  if (descriptor.type === "string") {
    if (typeof value !== "string" || !value.trim() || value.length > descriptor.maximum) {
      throw new Error(`${name} must be a non-empty string of at most ${descriptor.maximum} characters.`);
    }
    return value.trim();
  }
  if (descriptor.type === "datetime") return validDate(value, name);
  if (descriptor.type === "boolean") {
    if (typeof value !== "boolean") throw new Error(`${name} must be boolean.`);
    return value;
  }
  if (descriptor.type === "url") return normalizedUrl(value, name);
  if (descriptor.type === "selection") {
    if (!descriptor.values.includes(value)) throw new Error(`${name} has an unsupported value.`);
    return value;
  }
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${name} must be finite numeric data.`);
  if (descriptor.type === "integer" && !Number.isInteger(value)) throw new Error(`${name} must be an integer.`);
  if (descriptor.minimum !== undefined && value < descriptor.minimum) {
    throw new Error(`${name} must be at least ${descriptor.minimum}.`);
  }
  return value;
}

function secret(env, name) {
  if (!SECRET_ENV_NAME.test(String(name || ""))) throw new Error(`Invalid secret environment variable name: ${name}.`);
  const value = env[name]?.trim();
  if (!value) throw new Error(`${name} is required.`);
  return value;
}

export function validateOperationalConfig(raw) {
  const config = plainObject(raw, "config");
  rejectUnknownKeys(config, new Set(["odoo"]), "config");
  const odoo = plainObject(config.odoo, "config.odoo");
  rejectUnknownKeys(odoo, new Set(["url", "database", "apiKeyEnv"]), "config.odoo");
  const database = odoo.database === undefined ? undefined : String(odoo.database).trim();
  if (database !== undefined && !/^[A-Za-z0-9_.-]{1,128}$/.test(database)) {
    throw new Error("config.odoo.database contains unsupported characters.");
  }
  const apiKeyEnv = String(odoo.apiKeyEnv || "ARCIGY_ODOO_API_KEY");
  if (!SECRET_ENV_NAME.test(apiKeyEnv)) throw new Error("config.odoo.apiKeyEnv must name an ARCIGY_ environment variable.");
  return { odoo: { url: normalizedUrl(odoo.url, "config.odoo.url"), database, apiKeyEnv } };
}

export function validateOperationalEvidence(raw, now = Date.now()) {
  const evidence = plainObject(raw, "evidence");
  rejectUnknownKeys(evidence, new Set(["model", "environment", "source_updated_at", "items"]), "evidence");
  const model = String(evidence.model || "").trim();
  const contract = OPERATIONAL_MODELS[model];
  if (!contract) throw new Error(`Unsupported operational model: ${model || "<empty>"}.`);
  const environment = String(evidence.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("evidence.environment must be develop or main.");
  const sourceUpdatedAt = validDate(evidence.source_updated_at, "evidence.source_updated_at");
  if (Date.parse(sourceUpdatedAt) > now + 5 * 60_000) throw new Error("evidence.source_updated_at is too far in the future.");
  if (!Array.isArray(evidence.items) || evidence.items.length < 1 || evidence.items.length > 200) {
    throw new Error("evidence.items must contain between 1 and 200 records.");
  }
  const allowed = new Set(["external_key", "release_version", ...Object.keys(contract.fields)]);
  const items = evidence.items.map((rawItem, index) => {
    const item = plainObject(rawItem, `evidence.items[${index}]`);
    rejectUnknownKeys(item, allowed, `evidence.items[${index}]`);
    const externalKey = String(item.external_key || "").trim();
    if (!SAFE_EXTERNAL_KEY.test(externalKey) || !externalKey.startsWith(`${environment}:`)) {
      throw new Error(`evidence.items[${index}].external_key must be safe and prefixed by ${environment}:`);
    }
    const normalized = { external_key: externalKey };
    if (item.release_version !== undefined) {
      const releaseVersion = String(item.release_version).trim();
      if (!releaseVersion || releaseVersion.length > 128) throw new Error(`evidence.items[${index}].release_version is invalid.`);
      normalized.release_version = releaseVersion;
    }
    for (const [fieldName, descriptor] of Object.entries(contract.fields)) {
      const value = validateField(item[fieldName], descriptor, `evidence.items[${index}].${fieldName}`);
      if (value !== undefined) normalized[fieldName] = value;
    }
    if (normalized.finished_at && Date.parse(normalized.finished_at) <= Date.parse(normalized.started_at)) {
      throw new Error(`evidence.items[${index}].finished_at must be after started_at.`);
    }
    for (const fieldName of ["started_at", "finished_at", "oldest_unsynced_at"]) {
      if (normalized[fieldName] && Date.parse(normalized[fieldName]) > Date.parse(sourceUpdatedAt) + 5 * 60_000) {
        throw new Error(`evidence.items[${index}].${fieldName} is too far in the future.`);
      }
    }
    if (model === "saas.restore.test") {
      for (const [marker, measurement] of [
        ["rpo_measured", "actual_rpo_seconds"],
        ["rto_measured", "actual_rto_seconds"],
      ]) {
        if (normalized[measurement] !== undefined && normalized[marker] !== true) {
          throw new Error(`evidence.items[${index}].${measurement} requires ${marker}=true.`);
        }
        if (normalized[marker] === true && normalized[measurement] === undefined) {
          throw new Error(`evidence.items[${index}].${marker}=true requires ${measurement}.`);
        }
      }
      if (["success", "failed"].includes(normalized.status) && !normalized.finished_at) {
        throw new Error(`evidence.items[${index}] completed restore evidence requires finished_at.`);
      }
      if (normalized.status === "success") {
        for (const fieldName of [
          "checksum_valid",
          "application_smoke_passed",
          "tenant_isolation_passed",
          "rpo_measured",
          "rto_measured",
        ]) {
          if (normalized[fieldName] !== true) {
            throw new Error(`evidence.items[${index}] successful restore requires ${fieldName}=true.`);
          }
        }
      }
    }
    if (model === "saas.load.test") {
      if (!normalized.finished_at) {
        throw new Error(`evidence.items[${index}] completed load-test evidence requires finished_at.`);
      }
      if (
        normalized.p95_seconds !== undefined
        && normalized.p99_seconds !== undefined
        && normalized.p99_seconds < normalized.p95_seconds
      ) {
        throw new Error(`evidence.items[${index}].p99_seconds cannot be lower than p95_seconds.`);
      }
      if (normalized.error_rate !== undefined && normalized.error_rate > 100) {
        throw new Error(`evidence.items[${index}].error_rate cannot exceed 100.`);
      }
      if (normalized.representative === true) {
        if (!normalized.architecture_version || !(normalized.concurrent_users > 0)) {
          throw new Error(
            `evidence.items[${index}] representative load evidence requires architecture_version and positive concurrent_users.`,
          );
        }
      }
    }
    if (model === "saas.data.quality.run" && normalized.event_stream_complete === true) {
      if (!normalized.finished_at) {
        throw new Error(`evidence.items[${index}] complete event-stream evidence requires finished_at.`);
      }
      const completeCountFields = [
        "events_sent",
        "events_received",
        "events_processed",
        "events_rejected",
        "retry_adjustment_count",
        "duplicate_count",
        "schema_failure_count",
        "missing_field_count",
        "late_event_count",
        "unknown_tenant_count",
      ];
      for (const fieldName of completeCountFields) {
        if (normalized[fieldName] === undefined) {
          throw new Error(
            `evidence.items[${index}] complete event-stream evidence requires ${fieldName}.`,
          );
        }
      }
      if (normalized.events_processed + normalized.events_rejected > normalized.events_received) {
        throw new Error(
          `evidence.items[${index}] processed and rejected events cannot exceed received events.`,
        );
      }
      for (const fieldName of [
        "duplicate_count",
        "schema_failure_count",
        "missing_field_count",
        "late_event_count",
        "unknown_tenant_count",
      ]) {
        if (normalized[fieldName] > normalized.events_received) {
          throw new Error(
            `evidence.items[${index}].${fieldName} cannot exceed events_received.`,
          );
        }
      }
      const maximumRetryAdjustment = Math.max(
        normalized.events_sent - normalized.events_received,
        0,
      );
      if (normalized.retry_adjustment_count > maximumRetryAdjustment) {
        throw new Error(
          `evidence.items[${index}].retry_adjustment_count exceeds the sent/received difference.`,
        );
      }
    }
    if (model === "saas.sync.run") {
      if (normalized.sync_contract_complete !== true) {
        throw new Error(`evidence.items[${index}] external sync evidence requires sync_contract_complete=true.`);
      }
      if (!normalized.finished_at || normalized.status === "running") {
        throw new Error(`evidence.items[${index}] complete sync evidence requires a completed attempt.`);
      }
      const completeCountFields = [
        "records_read",
        "records_created",
        "records_updated",
        "records_skipped",
        "records_rejected",
        "duplicate_upsert_count",
        "api_error_count",
        "authentication_error_count",
        "permission_error_count",
        "rate_limit_error_count",
        "retry_count",
        "backlog_count",
      ];
      for (const fieldName of completeCountFields) {
        if (normalized[fieldName] === undefined) {
          throw new Error(`evidence.items[${index}] complete sync evidence requires ${fieldName}.`);
        }
      }
      const categorized = normalized.records_created
        + normalized.records_updated
        + normalized.records_skipped
        + normalized.records_rejected;
      if (categorized !== normalized.records_read) {
        throw new Error(`evidence.items[${index}] categorized records must equal records_read.`);
      }
      if (normalized.duplicate_upsert_count > normalized.records_read) {
        throw new Error(`evidence.items[${index}].duplicate_upsert_count cannot exceed records_read.`);
      }
      const classifiedApiErrors = normalized.authentication_error_count
        + normalized.permission_error_count
        + normalized.rate_limit_error_count;
      if (classifiedApiErrors > normalized.api_error_count) {
        throw new Error(`evidence.items[${index}] classified API errors cannot exceed api_error_count.`);
      }
      if (normalized.backlog_count > 0 && !normalized.oldest_unsynced_at) {
        throw new Error(`evidence.items[${index}] positive backlog requires oldest_unsynced_at.`);
      }
      if (normalized.backlog_count === 0 && normalized.oldest_unsynced_at) {
        throw new Error(`evidence.items[${index}] empty backlog cannot have oldest_unsynced_at.`);
      }
      if (
        normalized.oldest_unsynced_at
        && Date.parse(normalized.oldest_unsynced_at) > Date.parse(normalized.finished_at)
      ) {
        throw new Error(`evidence.items[${index}].oldest_unsynced_at cannot be newer than finished_at.`);
      }
      if (
        normalized.status === "success"
        && (
          normalized.records_rejected
          || normalized.api_error_count
          || normalized.authentication_error_count
          || normalized.permission_error_count
          || normalized.rate_limit_error_count
        )
      ) {
        throw new Error(`evidence.items[${index}] successful sync evidence cannot contain errors.`);
      }
      if (normalized.error_code && !/^[A-Z0-9_.:-]{1,64}$/.test(normalized.error_code)) {
        throw new Error(`evidence.items[${index}].error_code must be a bounded symbolic code.`);
      }
    }
    return normalized;
  });
  return { model, environment, source_updated_at: sourceUpdatedAt, items };
}

async function boundedJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    if (Buffer.byteLength(text) > MAX_RESPONSE_BYTES) throw new Error(`Response from ${new URL(url).origin} is too large.`);
    if (!response.ok) throw new Error(`${new URL(url).origin} returned HTTP ${response.status}.`);
    return JSON.parse(text);
  } finally {
    clearTimeout(timeout);
  }
}

export async function runOperationalSync(
  rawConfig,
  rawEvidence,
  { env = process.env, dryRun = false, requestJson = boundedJson } = {},
) {
  const config = validateOperationalConfig(rawConfig);
  const evidence = validateOperationalEvidence(rawEvidence);
  if (dryRun) {
    return { ok: true, dryRun: true, model: evidence.model, environment: evidence.environment, records: evidence.items.length };
  }
  const headers = {
    Authorization: `Bearer ${secret(env, config.odoo.apiKeyEnv)}`,
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "Arcigy-SaaS-Operational-Sync/1.0",
  };
  if (config.odoo.database) headers["X-Odoo-Database"] = config.odoo.database;
  const method = OPERATIONAL_MODELS[evidence.model].method || "ingest_operational_batch";
  const url = `${config.odoo.url}/json/2/${evidence.model}/${method}`;
  const odoo = await requestJson(url, {
    method: "POST",
    headers,
    body: JSON.stringify({
      payload: {
        environment: evidence.environment,
        source_updated_at: evidence.source_updated_at,
        items: evidence.items,
      },
    }),
  });
  return { ok: true, dryRun: false, model: evidence.model, environment: evidence.environment, records: evidence.items.length, odoo };
}

async function main() {
  const configArg = process.argv.find((arg) => arg.startsWith("--config="));
  const evidenceArg = process.argv.find((arg) => arg.startsWith("--evidence="));
  if (!configArg || !evidenceArg) {
    throw new Error("Usage: node saas_operational_sync.mjs --config=<path> --evidence=<path> [--dry-run]");
  }
  const readBoundedJson = async (path, name) => {
    const metadata = await stat(path);
    if (!metadata.isFile() || metadata.size > MAX_INPUT_BYTES) {
      throw new Error(`${name} must be a JSON file no larger than ${MAX_INPUT_BYTES} bytes.`);
    }
    return JSON.parse(await readFile(path, "utf8"));
  };
  const config = await readBoundedJson(configArg.slice("--config=".length), "config");
  const evidence = await readBoundedJson(evidenceArg.slice("--evidence=".length), "evidence");
  const result = await runOperationalSync(config, evidence, { dryRun: process.argv.includes("--dry-run") });
  console.log(JSON.stringify({ ok: result.ok, dryRun: result.dryRun, model: result.model, environment: result.environment, records: result.records }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
