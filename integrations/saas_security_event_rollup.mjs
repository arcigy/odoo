import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

import { normalizeProductEventExport } from "./saas_product_event_rollup.mjs";

const MAX_INPUT_BYTES = 32 * 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SERVER_SOURCE_CODES = new Set(["server", "backend", "worker", "integration"]);
const SAFE_CODE = /^[A-Za-z0-9._:-]{1,120}$/;
const PRIVILEGED_EVENTS = new Set([
  "admin_action_performed",
  "permission_changed",
  "api_key_created",
  "api_key_revoked",
  "sensitive_export_requested",
  "sensitive_export_completed",
]);
const SIGNATURE_FAILURE_REASONS = new Set(["signature_verification_failed", "invalid_signature", "signature_missing"]);

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object.`);
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function codeList(value, name) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 4) {
    throw new Error(`${name} must contain between 1 and 4 approved server sources.`);
  }
  const normalized = value.map((item, index) => {
    const source = String(item || "").trim();
    if (!SAFE_CODE.test(source) || !SERVER_SOURCE_CODES.has(source)) {
      throw new Error(`${name}[${index}] must be an approved server source.`);
    }
    return source;
  });
  if (new Set(normalized).size !== normalized.length) throw new Error(`${name} must not contain duplicates.`);
  return normalized;
}

export function validateSecurityEventConfig(raw) {
  const config = plainObject(raw, "config");
  rejectUnknownKeys(config, new Set(["environment", "server_sources", "security_stream_complete"]), "config");
  const environment = String(config.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("config.environment must be develop or main.");
  if (config.security_stream_complete !== true) {
    throw new Error("config.security_stream_complete must be explicitly true before zero counts can be emitted.");
  }
  return { environment, serverSources: codeList(config.server_sources, "config.server_sources") };
}

function dayBounds(timestamp) {
  const day = timestamp.slice(0, 10);
  const start = `${day}T00:00:00.000Z`;
  const end = new Date(Date.parse(start) + 86_400_000).toISOString();
  return { day, start, end };
}

function emptyBucket(period) {
  return {
    ...period,
    login_attempts: 0,
    login_failures: 0,
    rate_limit_events: 0,
    suspicious_login_count: 0,
    cross_tenant_denied_count: 0,
    confirmed_cross_tenant_exposure_count: 0,
    privileged_action_count: 0,
    webhook_signature_failure_count: 0,
    audit_delivery_failure_count: 0,
  };
}

export function rollupSecurityEvents(rawConfig, rawExport) {
  const securityConfig = validateSecurityEventConfig(rawConfig);
  const normalized = normalizeProductEventExport(
    {
      environment: securityConfig.environment,
      server_sources: securityConfig.serverSources,
      meaningful_events: ["core_action_succeeded"],
    },
    rawExport,
  );
  const days = new Map();
  for (const event of normalized.events) {
    const period = dayBounds(event.occurred_at_utc);
    if (Date.parse(period.end) > Date.parse(normalized.sourceUpdatedAt)) {
      throw new Error(`Event day ${period.day} is not closed at the export watermark.`);
    }
    const bucket = days.get(period.day) || emptyBucket(period);
    if (event.event_name === "login_succeeded" || event.event_name === "login_failed") bucket.login_attempts += 1;
    if (event.event_name === "login_failed") bucket.login_failures += 1;
    if (event.event_name === "rate_limit_triggered") bucket.rate_limit_events += 1;
    if (event.event_name === "suspicious_login_detected") bucket.suspicious_login_count += 1;
    if (event.event_name === "cross_tenant_access_denied") bucket.cross_tenant_denied_count += 1;
    if (event.event_name === "cross_tenant_exposure_confirmed") bucket.confirmed_cross_tenant_exposure_count += 1;
    if (PRIVILEGED_EVENTS.has(event.event_name)) bucket.privileged_action_count += 1;
    if (
      event.event_name === "webhook_signature_failed"
      || (event.event_name === "webhook_rejected" && SIGNATURE_FAILURE_REASONS.has(event.failure_reason))
    ) bucket.webhook_signature_failure_count += 1;
    if (event.event_name === "audit_log_delivery_failed") bucket.audit_delivery_failure_count += 1;
    days.set(period.day, bucket);
  }
  const items = [...days.values()].sort((left, right) => left.day.localeCompare(right.day)).map((bucket) => {
    const { day, start, end, ...counts } = bucket;
    return {
      external_key: `${securityConfig.environment}:security:event-rollup-v1:${day}`,
      period_start: start,
      period_end: end,
      status: "unknown",
      data_quality_status: "valid",
      ...counts,
    };
  });
  if (items.length > 500) throw new Error("Security event rollup cannot emit more than 500 daily rows per artifact.");
  return {
    evidence: {
      model: "saas.security.daily",
      environment: securityConfig.environment,
      source_updated_at: normalized.sourceUpdatedAt,
      items,
    },
    stats: { ...normalized.stats, dailyRows: items.length },
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
    throw new Error("Usage: node saas_security_event_rollup.mjs --config=<path> --events=<path>");
  }
  const config = await readBoundedJson(configArg.slice("--config=".length), "config");
  const events = await readBoundedJson(eventsArg.slice("--events=".length), "events");
  const result = rollupSecurityEvents(config, events);
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
