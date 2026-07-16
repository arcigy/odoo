import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SECRET_ENV_NAME = /^ARCIGY_[A-Z0-9_]+$/;
const SAFE_KEY = /^[A-Za-z0-9._:-]{1,255}$/;
const SAFE_CODE = /^[A-Za-z0-9._:-]{1,120}$/;
const COMMON_FIELDS = new Set([
  "external_key",
  "period_start",
  "period_end",
  "status",
  "data_quality_status",
  "drilldown_url",
  "service_code",
  "tenant_external_id",
  "plan_code",
  "region_code",
  "feature_code",
  "integration_code",
  "currency_code",
]);

const integers = (...fields) => fields;
const numbers = (...fields) => fields;
const strings = (...fields) => fields;

const AGGREGATES = Object.freeze({
  "saas.tenant.daily": {
    cadence: "day",
    required: ["tenant_external_id"],
    integers: integers("active_users", "active_seats", "purchased_seats", "core_actions", "incident_count", "failed_jobs"),
    numbers: numbers("error_rate", "p95_seconds", "mrr", "operational_cost", "health_score"),
    selections: { health_status: ["healthy", "watch", "at_risk", "critical"] },
    strings: strings("health_reasons"),
  },
  "saas.endpoint.hourly": {
    cadence: "hour",
    required: ["method", "endpoint_group", "slo_class"],
    integers: integers("request_count", "success_count", "error_count", "timeout_count", "rate_limited_count"),
    numbers: numbers(
      "duration_seconds_sum",
      "p50_seconds",
      "p95_seconds",
      "p99_seconds",
      "request_bytes_p95",
      "response_bytes_p95",
    ),
    selections: { slo_class: ["critical", "high_fast", "high_slow", "low", "no_slo"] },
    strings: strings("method", "endpoint_group"),
  },
  "saas.database.hourly": {
    cadence: "hour",
    integers: integers(
      "open_connections",
      "active_connections",
      "waiting_connections",
      "max_connections",
      "pool_timeout_count",
      "slow_query_count",
      "lock_wait_count",
      "deadlock_count",
      "rollback_count",
      "storage_bytes",
      "storage_growth_bytes",
      "wal_lag_bytes",
    ),
    signed: ["storage_growth_bytes"],
    numbers: numbers("pool_utilization", "pool_wait_p95_seconds", "query_p95_seconds", "cache_hit_ratio", "replication_lag_seconds"),
  },
  "saas.cache.hourly": {
    cadence: "hour",
    required: ["namespace"],
    integers: integers(
      "request_count",
      "hit_count",
      "miss_count",
      "timeout_count",
      "error_count",
      "evicted_keys",
      "stale_served_count",
      "consistency_incident_count",
    ),
    numbers: numbers("hit_ratio", "get_p95_seconds", "set_p95_seconds", "invalidation_lag_seconds"),
    strings: strings("namespace"),
  },
  "saas.queue.hourly": {
    cadence: "hour",
    required: ["queue_name", "job_type"],
    integers: integers(
      "queue_depth",
      "started_count",
      "completed_count",
      "failed_count",
      "retry_count",
      "duplicate_suppressed_count",
      "idempotency_conflict_count",
      "dlq_size",
      "worker_count",
    ),
    numbers: numbers("oldest_age_seconds", "enqueue_rate", "processing_rate", "drain_time_seconds"),
    strings: strings("queue_name", "job_type"),
  },
  "saas.dependency.hourly": {
    cadence: "hour",
    required: ["integration_code", "currency_code"],
    integers: integers("request_count", "success_count", "timeout_count", "retry_count"),
    numbers: numbers("p50_seconds", "p95_seconds", "p99_seconds", "quota_utilization", "cost"),
  },
  "saas.cost.daily": {
    cadence: "day",
    required: ["provider", "category", "amount", "currency_code"],
    integers: integers("active_users", "active_tenants", "core_actions"),
    numbers: numbers("amount", "cost_per_active_user", "cost_per_active_tenant", "cost_per_core_action"),
    signed: ["amount"],
    selections: { category: ["compute", "database", "storage", "network", "observability", "email", "ai", "payment", "other"] },
    strings: strings("provider"),
  },
  "saas.product.daily": {
    cadence: "day",
    integers: integers("active_users", "active_tenants", "core_actions", "signup_count", "activated_tenants", "eligible_new_tenants"),
    numbers: numbers("activation_rate", "retention_rate", "feature_adoption_rate", "time_to_value_p50_seconds", "time_to_value_p90_seconds"),
  },
  "saas.security.daily": {
    cadence: "day",
    integers: integers(
      "login_attempts",
      "login_failures",
      "rate_limit_events",
      "suspicious_login_count",
      "cross_tenant_denied_count",
      "confirmed_cross_tenant_exposure_count",
      "privileged_action_count",
      "webhook_signature_failure_count",
      "critical_vulnerability_count",
      "high_vulnerability_count",
      "secret_finding_count",
      "audit_delivery_failure_count",
    ),
  },
  "saas.capacity.daily": {
    cadence: "day",
    integers: integers("current_concurrent_users", "tested_concurrent_users"),
    numbers: numbers(
      "peak_rps",
      "tested_safe_rps",
      "capacity_headroom",
      "db_connection_headroom",
      "cpu_headroom",
      "memory_headroom",
      "storage_days_to_full",
      "queue_drain_headroom",
    ),
    selections: { readiness: ["ready", "ready_with_risk", "not_ready", "test_stale"] },
  },
});

export function aggregateContractFields() {
  return Object.fromEntries(
    Object.entries(AGGREGATES).map(([model, schema]) => [
      model,
      [...new Set([
        ...(schema.integers || []),
        ...(schema.numbers || []),
        ...(schema.strings || []),
        ...Object.keys(schema.selections || {}),
      ])].sort(),
    ]),
  );
}

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object.`);
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function secureUrl(value, name) {
  const url = new URL(String(value || ""));
  if (url.username || url.password) throw new Error(`${name} must not contain credentials.`);
  const loopback = url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  if (url.protocol !== "https:" && !loopback) throw new Error(`${name} must use HTTPS except on loopback.`);
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

function isoDate(value, name) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) throw new Error(`${name} must be an ISO-8601 timestamp.`);
  return new Date(timestamp).toISOString();
}

function boundedString(value, name, maximum = 4000) {
  if (typeof value !== "string" || !value.trim() || value.length > maximum) {
    throw new Error(`${name} must be a non-empty string of at most ${maximum} characters.`);
  }
  if ([...value].some((character) => character.charCodeAt(0) < 32 && !["\n", "\t"].includes(character))) {
    throw new Error(`${name} contains unsupported control characters.`);
  }
  return value.trim();
}

function secret(env, name) {
  if (!SECRET_ENV_NAME.test(String(name || ""))) throw new Error(`Invalid secret environment variable name: ${name}.`);
  const value = env[name]?.trim();
  if (!value) throw new Error(`${name} is required.`);
  return value;
}

function validateCode(value, name) {
  const normalized = String(value || "").trim();
  if (!SAFE_CODE.test(normalized)) throw new Error(`${name} must be a safe identifier.`);
  return normalized;
}

export function validateAggregateConfig(raw) {
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
  return { odoo: { url: secureUrl(odoo.url, "config.odoo.url"), database, apiKeyEnv } };
}

export function validateAggregateEvidence(raw, now = Date.now()) {
  const evidence = plainObject(raw, "evidence");
  rejectUnknownKeys(evidence, new Set(["model", "environment", "source_updated_at", "items"]), "evidence");
  const model = String(evidence.model || "").trim();
  const schema = AGGREGATES[model];
  if (!schema) throw new Error(`Unsupported aggregate model: ${model || "<empty>"}.`);
  const environment = String(evidence.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("evidence.environment must be develop or main.");
  const sourceUpdatedAt = isoDate(evidence.source_updated_at, "evidence.source_updated_at");
  if (Date.parse(sourceUpdatedAt) > now + 5 * 60_000) throw new Error("evidence.source_updated_at is too far in the future.");
  if (!Array.isArray(evidence.items) || evidence.items.length < 1 || evidence.items.length > 500) {
    throw new Error("evidence.items must contain between 1 and 500 records.");
  }
  const modelFields = new Set([
    ...(schema.integers || []),
    ...(schema.numbers || []),
    ...(schema.strings || []),
    ...Object.keys(schema.selections || {}),
  ]);
  const allowed = new Set([...COMMON_FIELDS, ...modelFields]);
  const normalizedItems = evidence.items.map((rawItem, index) => {
    const name = `evidence.items[${index}]`;
    const item = plainObject(rawItem, name);
    rejectUnknownKeys(item, allowed, name);
    if (!Object.keys(item).some((field) => modelFields.has(field))) {
      throw new Error(`${name} must contain at least one ${model} aggregate field.`);
    }
    for (const required of ["external_key", "period_start", "period_end", ...(schema.required || [])]) {
      if (item[required] === undefined || item[required] === null || item[required] === "") {
        throw new Error(`${name}.${required} is required.`);
      }
    }
    const externalKey = String(item.external_key).trim();
    if (!SAFE_KEY.test(externalKey) || !externalKey.startsWith(`${environment}:`)) {
      throw new Error(`${name}.external_key must be safe and prefixed by ${environment}:`);
    }
    const periodStart = isoDate(item.period_start, `${name}.period_start`);
    const periodEnd = isoDate(item.period_end, `${name}.period_end`);
    const duration = (Date.parse(periodEnd) - Date.parse(periodStart)) / 1000;
    const expectedDuration = schema.cadence === "hour" ? 3600 : 86400;
    if (duration !== expectedDuration) throw new Error(`${name} must contain exactly one ${schema.cadence} UTC period.`);
    if (Date.parse(periodEnd) > Date.parse(sourceUpdatedAt) + 5 * 60_000) {
      throw new Error(`${name}.period_end is too far in the future.`);
    }
    const status = String(item.status || "unknown").trim().toLowerCase();
    const dataQualityStatus = String(item.data_quality_status || "valid").trim().toLowerCase();
    if (!new Set(["healthy", "warning", "critical", "unknown"]).has(status)) throw new Error(`${name}.status is invalid.`);
    if (!new Set(["valid", "warning", "invalid"]).has(dataQualityStatus)) {
      throw new Error(`${name}.data_quality_status is invalid.`);
    }
    const normalized = {
      external_key: externalKey,
      period_start: periodStart,
      period_end: periodEnd,
      status,
      data_quality_status: dataQualityStatus,
    };
    for (const dimension of [
      "service_code",
      "tenant_external_id",
      "plan_code",
      "region_code",
      "feature_code",
      "integration_code",
      "currency_code",
    ]) {
      if (item[dimension] !== undefined) normalized[dimension] = validateCode(item[dimension], `${name}.${dimension}`);
    }
    if (item.drilldown_url !== undefined) normalized.drilldown_url = secureUrl(item.drilldown_url, `${name}.drilldown_url`);
    for (const field of schema.integers || []) {
      if (item[field] === undefined) continue;
      if (typeof item[field] !== "number" || !Number.isInteger(item[field])) throw new Error(`${name}.${field} must be an integer.`);
      if (!(schema.signed || []).includes(field) && item[field] < 0) throw new Error(`${name}.${field} must not be negative.`);
      normalized[field] = item[field];
    }
    for (const field of schema.numbers || []) {
      if (item[field] === undefined) continue;
      if (typeof item[field] !== "number" || !Number.isFinite(item[field])) throw new Error(`${name}.${field} must be finite numeric data.`);
      if (!(schema.signed || []).includes(field) && item[field] < 0) throw new Error(`${name}.${field} must not be negative.`);
      normalized[field] = item[field];
    }
    for (const field of schema.strings || []) {
      if (item[field] !== undefined) normalized[field] = boundedString(item[field], `${name}.${field}`);
    }
    for (const [field, values] of Object.entries(schema.selections || {})) {
      if (item[field] === undefined) continue;
      if (!values.includes(item[field])) throw new Error(`${name}.${field} has an unsupported value.`);
      normalized[field] = item[field];
    }
    if (
      model === "saas.tenant.daily" &&
      (item.mrr !== undefined || item.operational_cost !== undefined) &&
      !item.currency_code
    ) {
      throw new Error(`${name}.currency_code is required when tenant monetary fields are present.`);
    }
    const boundedCounts = {
      "saas.endpoint.hourly": ["request_count", ["success_count", "error_count", "timeout_count", "rate_limited_count"]],
      "saas.cache.hourly": ["request_count", ["hit_count", "miss_count", "timeout_count", "error_count"]],
      "saas.dependency.hourly": ["request_count", ["success_count", "timeout_count"]],
      "saas.security.daily": ["login_attempts", ["login_failures", "suspicious_login_count"]],
      "saas.product.daily": ["eligible_new_tenants", ["activated_tenants"]],
    }[model];
    if (boundedCounts && normalized[boundedCounts[0]] !== undefined) {
      for (const field of boundedCounts[1]) {
        if (normalized[field] !== undefined && normalized[field] > normalized[boundedCounts[0]]) {
          throw new Error(`${name}.${field} cannot exceed ${boundedCounts[0]}.`);
        }
      }
    }
    return normalized;
  });
  return { model, environment, source_updated_at: sourceUpdatedAt, items: normalizedItems };
}

async function boundedJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    const target = new URL(url);
    if (Buffer.byteLength(text) > MAX_RESPONSE_BYTES) throw new Error(`Response from ${target.origin}${target.pathname} is too large.`);
    if (!response.ok) throw new Error(`${target.origin}${target.pathname} returned HTTP ${response.status}.`);
    return JSON.parse(text);
  } finally {
    clearTimeout(timeout);
  }
}

export async function runAggregateSync(
  rawConfig,
  rawEvidence,
  { env = process.env, dryRun = false, requestJson = boundedJson } = {},
) {
  const config = validateAggregateConfig(rawConfig);
  const evidence = validateAggregateEvidence(rawEvidence);
  if (dryRun) {
    return { ok: true, dryRun: true, model: evidence.model, environment: evidence.environment, records: evidence.items.length };
  }
  const headers = {
    Authorization: `Bearer ${secret(env, config.odoo.apiKeyEnv)}`,
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "Arcigy-SaaS-Aggregate-Sync/1.0",
  };
  if (config.odoo.database) headers["X-Odoo-Database"] = config.odoo.database;
  const odoo = await requestJson(`${config.odoo.url}/json/2/${evidence.model}/ingest_aggregate_batch`, {
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

async function readBoundedJson(path, name) {
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_INPUT_BYTES) {
    throw new Error(`${name} must be a JSON file no larger than ${MAX_INPUT_BYTES} bytes.`);
  }
  return JSON.parse(await readFile(path, "utf8"));
}

async function main() {
  const configArg = process.argv.find((argument) => argument.startsWith("--config="));
  const evidenceArg = process.argv.find((argument) => argument.startsWith("--evidence="));
  if (!configArg || !evidenceArg) {
    throw new Error("Usage: node saas_aggregate_sync.mjs --config=<path> --evidence=<path> [--dry-run]");
  }
  const config = await readBoundedJson(configArg.slice("--config=".length), "config");
  const evidence = await readBoundedJson(evidenceArg.slice("--evidence=".length), "evidence");
  const result = await runAggregateSync(config, evidence, { dryRun: process.argv.includes("--dry-run") });
  console.log(JSON.stringify({ ok: result.ok, dryRun: result.dryRun, model: result.model, environment: result.environment, records: result.records }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
