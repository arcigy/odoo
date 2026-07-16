import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 32 * 1024 * 1024;
const MAX_EVENTS = 100_000;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SERVER_SOURCE_CODES = new Set(["server", "backend", "worker", "integration"]);
const SAFE_CODE = /^[A-Za-z0-9._:-]{1,120}$/;
const SAFE_IDENTIFIER = /^[A-Za-z0-9._:@-]{1,255}$/;
const HASH = /^(?:sha256:)?[a-f0-9]{64}$/i;
const UTC_TIMESTAMP = /(?:Z|\+00:00)$/i;
const REQUIRED_EVENT_FIELDS = new Set([
  "event_id",
  "event_name",
  "event_version",
  "occurred_at_utc",
  "received_at_utc",
  "environment",
  "release_version",
  "tenant_id",
  "user_id_hash",
  "session_id",
  "feature",
  "object_type",
  "object_id_hash",
  "outcome",
  "failure_reason",
  "request_id",
  "trace_id",
  "source",
  "plan",
  "properties",
]);
const DEFAULT_MEANINGFUL_EVENTS = [
  "core_action_succeeded",
  "feature_used",
  "record_created",
  "record_updated",
  "export_completed",
  "import_completed",
  "file_processed",
];
const SUCCESS_OUTCOMES = new Set(["success", "succeeded", "completed", "accepted"]);
const BILLING_EVENTS = new Set([
  "trial_started",
  "trial_expired",
  "subscription_started",
  "subscription_upgraded",
  "subscription_downgraded",
  "subscription_cancel_requested",
  "subscription_cancelled",
  "subscription_reactivated",
  "invoice_created",
  "payment_succeeded",
  "payment_failed",
  "payment_recovered",
  "refund_created",
  "dispute_created",
  "usage_recorded",
  "usage_invoiced",
]);
const SERVER_ONLY_EVENTS = new Set([
  "account_signed_up",
  "email_verification_sent",
  "email_verified",
  "login_succeeded",
  "login_failed",
  "logout_completed",
  "password_reset_requested",
  "password_reset_completed",
  "mfa_enabled",
  "mfa_disabled",
  "session_revoked",
  "api_key_created",
  "api_key_revoked",
  "admin_action_performed",
  "permission_changed",
  "sensitive_export_requested",
  "sensitive_export_completed",
  "data_deletion_requested",
  "data_deletion_completed",
  "rate_limit_triggered",
  "suspicious_login_detected",
  "cross_tenant_access_denied",
  "cross_tenant_exposure_confirmed",
  "webhook_received",
  "webhook_processed",
  "webhook_rejected",
  "webhook_signature_failed",
  "audit_log_delivery_failed",
]);
const PII_PROPERTY = /email|e_mail|fullname|full_name|firstname|first_name|lastname|last_name|phone|address|password|secret|token|cookie|authorization/i;

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

function code(value, name, { nullable = false } = {}) {
  if (value === null && nullable) return null;
  const normalized = String(value || "").trim();
  if (!SAFE_CODE.test(normalized)) throw new Error(`${name} must be a safe identifier.`);
  return normalized;
}

function identifier(value, name, { nullable = false } = {}) {
  if (value === null && nullable) return null;
  const normalized = String(value || "").trim();
  if (!SAFE_IDENTIFIER.test(normalized)) throw new Error(`${name} must be an opaque safe identifier.`);
  return normalized;
}

function hash(value, name) {
  if (value === null) return null;
  const normalized = String(value || "").trim().toLowerCase();
  if (!HASH.test(normalized)) throw new Error(`${name} must be a SHA-256 hash, not a raw identifier.`);
  return normalized.startsWith("sha256:") ? normalized : `sha256:${normalized}`;
}

function utcTimestamp(value, name) {
  const raw = String(value || "").trim();
  if (!UTC_TIMESTAMP.test(raw)) throw new Error(`${name} must explicitly use UTC.`);
  const timestamp = Date.parse(raw);
  if (!Number.isFinite(timestamp)) throw new Error(`${name} must be an ISO-8601 timestamp.`);
  return new Date(timestamp).toISOString();
}

function stringList(value, name, fallback) {
  const candidate = value === undefined ? fallback : value;
  if (!Array.isArray(candidate) || candidate.length < 1 || candidate.length > 50) {
    throw new Error(`${name} must contain between 1 and 50 event names.`);
  }
  const normalized = candidate.map((item, index) => code(item, `${name}[${index}]`));
  if (new Set(normalized).size !== normalized.length) throw new Error(`${name} must not contain duplicates.`);
  return normalized;
}

export function validateProductEventConfig(raw) {
  const config = plainObject(raw, "config");
  rejectUnknownKeys(config, new Set(["environment", "server_sources", "meaningful_events"]), "config");
  const environment = String(config.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("config.environment must be develop or main.");
  const serverSources = stringList(config.server_sources, "config.server_sources", ["server", "worker"]);
  const invalidServerSources = serverSources.filter((source) => !SERVER_SOURCE_CODES.has(source));
  if (invalidServerSources.length) {
    throw new Error(`config.server_sources contains non-server sources: ${invalidServerSources.join(", ")}.`);
  }
  return {
    environment,
    serverSources: new Set(serverSources),
    meaningfulEvents: new Set(stringList(config.meaningful_events, "config.meaningful_events", DEFAULT_MEANINGFUL_EVENTS)),
  };
}

function validateProperties(value, name) {
  const properties = plainObject(value, name);
  const keys = Object.keys(properties);
  if (keys.length > 32) throw new Error(`${name} must contain no more than 32 properties.`);
  const normalized = {};
  for (const key of keys.sort()) {
    if (!SAFE_CODE.test(key)) throw new Error(`${name}.${key} has an unsafe property name.`);
    if (PII_PROPERTY.test(key)) throw new Error(`${name}.${key} is a forbidden PII or secret property.`);
    const item = properties[key];
    if (item === null || typeof item === "boolean") {
      normalized[key] = item;
    } else if (typeof item === "number" && Number.isFinite(item)) {
      normalized[key] = item;
    } else if (typeof item === "string" && item.length <= 256 && !/[\u0000-\u001f]/.test(item)) {
      normalized[key] = item;
    } else {
      throw new Error(`${name}.${key} must be a bounded scalar value.`);
    }
  }
  return normalized;
}

function validateEvent(raw, index, config, sourceUpdatedAt) {
  const name = `export.events[${index}]`;
  const event = plainObject(raw, name);
  rejectUnknownKeys(event, REQUIRED_EVENT_FIELDS, name);
  const missing = [...REQUIRED_EVENT_FIELDS].filter((field) => !Object.hasOwn(event, field));
  if (missing.length) throw new Error(`${name} is missing required fields: ${missing.join(", ")}.`);
  if (!Number.isInteger(event.event_version) || event.event_version < 1 || event.event_version > 1000) {
    throw new Error(`${name}.event_version must be an integer between 1 and 1000.`);
  }
  const environment = String(event.environment || "").trim().toLowerCase();
  if (environment !== config.environment) throw new Error(`${name}.environment must match ${config.environment}.`);
  const occurredAt = utcTimestamp(event.occurred_at_utc, `${name}.occurred_at_utc`);
  const receivedAt = utcTimestamp(event.received_at_utc, `${name}.received_at_utc`);
  if (Date.parse(receivedAt) < Date.parse(occurredAt)) throw new Error(`${name}.received_at_utc must not precede occurrence.`);
  if (Date.parse(receivedAt) > Date.parse(sourceUpdatedAt) + 5 * 60_000) {
    throw new Error(`${name}.received_at_utc is newer than the export watermark.`);
  }
  const eventName = code(event.event_name, `${name}.event_name`);
  const source = code(event.source, `${name}.source`);
  if ((BILLING_EVENTS.has(eventName) || SERVER_ONLY_EVENTS.has(eventName)) && !config.serverSources.has(source)) {
    throw new Error(`${name}.${eventName} must originate from an approved server source.`);
  }
  const outcome = code(event.outcome, `${name}.outcome`).toLowerCase();
  const failureReason = event.failure_reason === null ? null : code(event.failure_reason, `${name}.failure_reason`);
  if (["failed", "failure", "rejected"].includes(outcome) && !failureReason) {
    throw new Error(`${name}.failure_reason is required for failed outcomes.`);
  }
  return {
    event_id: identifier(event.event_id, `${name}.event_id`),
    event_name: eventName,
    event_version: event.event_version,
    occurred_at_utc: occurredAt,
    received_at_utc: receivedAt,
    environment,
    release_version: code(event.release_version, `${name}.release_version`),
    tenant_id: identifier(event.tenant_id, `${name}.tenant_id`, { nullable: true }),
    user_id_hash: hash(event.user_id_hash, `${name}.user_id_hash`),
    session_id: identifier(event.session_id, `${name}.session_id`, { nullable: true }),
    feature: event.feature === null ? null : code(event.feature, `${name}.feature`),
    object_type: event.object_type === null ? null : code(event.object_type, `${name}.object_type`),
    object_id_hash: hash(event.object_id_hash, `${name}.object_id_hash`),
    outcome,
    failure_reason: failureReason,
    request_id: identifier(event.request_id, `${name}.request_id`, { nullable: true }),
    trace_id: identifier(event.trace_id, `${name}.trace_id`, { nullable: true }),
    source,
    plan: event.plan === null ? null : code(event.plan, `${name}.plan`),
    properties: validateProperties(event.properties, `${name}.properties`),
  };
}

function dayBounds(timestamp) {
  const day = timestamp.slice(0, 10);
  const start = `${day}T00:00:00.000Z`;
  const end = new Date(Date.parse(start) + 86_400_000).toISOString();
  return { day, start, end };
}

export function normalizeProductEventExport(rawConfig, rawExport) {
  const config = validateProductEventConfig(rawConfig);
  const exported = plainObject(rawExport, "export");
  rejectUnknownKeys(exported, new Set(["source_updated_at", "events"]), "export");
  const sourceUpdatedAt = utcTimestamp(exported.source_updated_at, "export.source_updated_at");
  if (!Array.isArray(exported.events) || exported.events.length < 1 || exported.events.length > MAX_EVENTS) {
    throw new Error(`export.events must contain between 1 and ${MAX_EVENTS} events.`);
  }
  const unique = new Map();
  let duplicatesSuppressed = 0;
  exported.events.forEach((rawEvent, index) => {
    const event = validateEvent(rawEvent, index, config, sourceUpdatedAt);
    const previous = unique.get(event.event_id);
    if (previous) {
      if (JSON.stringify(previous) !== JSON.stringify(event)) {
        throw new Error(`event_id ${event.event_id} has conflicting duplicate payloads.`);
      }
      duplicatesSuppressed += 1;
      return;
    }
    unique.set(event.event_id, event);
  });
  return {
    config,
    sourceUpdatedAt,
    events: [...unique.values()],
    stats: {
      eventsRead: exported.events.length,
      uniqueEvents: unique.size,
      duplicatesSuppressed,
    },
  };
}

export function rollupProductEvents(rawConfig, rawExport) {
  const normalized = normalizeProductEventExport(rawConfig, rawExport);
  const { config, sourceUpdatedAt } = normalized;

  const days = new Map();
  for (const event of normalized.events) {
    const period = dayBounds(event.occurred_at_utc);
    if (Date.parse(period.end) > Date.parse(sourceUpdatedAt)) {
      throw new Error(`Event day ${period.day} is not closed at the export watermark.`);
    }
    const bucket = days.get(period.day) || {
      ...period,
      activeUsers: new Set(),
      activeTenants: new Set(),
      coreActions: 0,
      signupCount: 0,
    };
    const successful = SUCCESS_OUTCOMES.has(event.outcome);
    if (successful && config.meaningfulEvents.has(event.event_name)) {
      if (event.user_id_hash) bucket.activeUsers.add(event.user_id_hash);
      if (event.tenant_id) bucket.activeTenants.add(event.tenant_id);
    }
    if (successful && event.event_name === "core_action_succeeded") bucket.coreActions += 1;
    if (successful && event.event_name === "account_signed_up") bucket.signupCount += 1;
    days.set(period.day, bucket);
  }
  const items = [...days.values()].sort((left, right) => left.day.localeCompare(right.day)).map((bucket) => ({
    external_key: `${config.environment}:product:event-rollup-v1:${bucket.day}`,
    period_start: bucket.start,
    period_end: bucket.end,
    status: "unknown",
    data_quality_status: "valid",
    active_users: bucket.activeUsers.size,
    active_tenants: bucket.activeTenants.size,
    core_actions: bucket.coreActions,
    signup_count: bucket.signupCount,
  }));
  if (items.length > 500) throw new Error("Product event rollup cannot emit more than 500 daily rows per artifact.");
  return {
    evidence: {
      model: "saas.product.daily",
      environment: config.environment,
      source_updated_at: sourceUpdatedAt,
      items,
    },
    stats: {
      ...normalized.stats,
      dailyRows: items.length,
    },
  };
}

async function readBoundedJson(path, name) {
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_INPUT_BYTES) {
    throw new Error(`${name} must be a JSON file no larger than ${MAX_INPUT_BYTES} bytes.`);
  }
  return JSON.parse(await readFile(path, "utf8"));
}

async function main() {
  const configArg = process.argv.find((arg) => arg.startsWith("--config="));
  const eventsArg = process.argv.find((arg) => arg.startsWith("--events="));
  if (!configArg || !eventsArg) {
    throw new Error("Usage: node saas_product_event_rollup.mjs --config=<path> --events=<path>");
  }
  const config = await readBoundedJson(configArg.slice("--config=".length), "config");
  const events = await readBoundedJson(eventsArg.slice("--events=".length), "events");
  const result = rollupProductEvents(config, events);
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
