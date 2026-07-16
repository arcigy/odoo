import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const MAX_RESPONSE_BYTES = 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const STATUS = new Set(["healthy", "warning", "critical", "unknown"]);
const TENANT_SIZE_BANDS = new Set(["micro", "small", "medium", "large", "enterprise"]);
const INCIDENT_SEVERITIES = new Set(["p0", "p1", "p2", "p3"]);
const SAFE_CODE = /^[A-Za-z0-9._:-]{1,120}$/;
const SAFE_EXTERNAL_KEY = /^[A-Za-z0-9._:-]{1,255}$/;
const SECRET_ENV_NAME = /^ARCIGY_[A-Z0-9_]+$/;
const UTC_TIMESTAMP = /(?:Z|\+00:00)$/i;
const ITEM_FIELDS = new Set([
  "code", "value", "numerator", "denominator", "sample_count", "status",
  "measured_at", "freshness_seconds", "scope_key", "service_code",
  "tenant_external_id", "plan_code", "region_code", "feature_code",
  "integration_code", "country_code", "currency_code", "tenant_size_band",
  "acquisition_source", "incident_severity", "drilldown_url", "external_key",
  "period_start", "period_end", "granularity",
]);
const DIMENSION_FIELDS = [
  "service_code", "tenant_external_id", "plan_code", "region_code", "feature_code",
  "integration_code", "country_code", "currency_code", "tenant_size_band",
  "acquisition_source", "incident_severity",
];

const BUSINESS_METRIC_CODES = new Set([
  "core_action_success_rate", "active_tenants", "activation_rate", "mau",
  "retention_30d", "mrr", "arr", "nrr", "payment_success_rate",
  "total_operational_cost", "cost_per_active_tenant", "gross_margin", "mrr_at_risk",
  "account_signups", "email_verification_rate", "tenant_creation_rate",
  "onboarding_completion_rate", "first_core_action_rate", "trial_to_paid_rate",
  "time_to_value_p50_seconds", "time_to_value_p90_seconds",
  "funnel_technical_failure_rate", "dau", "wau", "dau_mau_ratio",
  "core_actions_per_active_user", "feature_adoption_rate", "retention_d7",
  "dormant_tenants", "tenant_reactivation_rate", "active_seats", "seat_utilization",
  "tenant_health_score", "at_risk_tenants", "tenant_usage_drop_30d",
  "tenant_error_rate", "tenant_p95_seconds", "tenants_with_failed_invoice",
  "open_support_tickets", "net_new_mrr", "grr", "failed_payment_value",
  "billing_reconciliation_difference", "support_backlog",
  "support_first_response_p95_seconds", "support_resolution_p95_seconds",
  "support_sla_attainment", "csat", "support_reopen_rate", "cost_per_active_user",
  "cost_per_request", "cost_per_core_action", "budget_utilization",
  "forecast_month_end_cost", "cost_anomaly_index", "waste_cost",
  "escaped_defect_count", "cdn_hit_ratio", "cdn_egress_bytes", "search_query_count",
  "search_zero_result_rate", "website_visitors", "landing_page_conversion",
  "cost_per_lead", "cost_per_signup", "cost_per_activated_tenant",
  "customer_acquisition_cost", "return_on_ad_spend", "unknown_acquisition_source_rate",
  "crm_win_rate", "weighted_pipeline_value", "median_sales_cycle_days",
  "pql_to_paid_rate", "email_delivery_rate", "email_complaint_rate",
  "tracking_consent_rate", "ai_request_count", "ai_input_token_count",
  "ai_output_token_count", "ai_cost",
]);

export function businessMetricCodes() {
  return [...BUSINESS_METRIC_CODES].sort();
}

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object.`);
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function safeCode(value, name) {
  const normalized = String(value || "").trim();
  if (!SAFE_CODE.test(normalized)) throw new Error(`${name} must be a safe identifier.`);
  return normalized;
}

function utcTimestamp(value, name) {
  const raw = String(value || "").trim();
  if (!UTC_TIMESTAMP.test(raw)) throw new Error(`${name} must explicitly use UTC.`);
  const timestamp = Date.parse(raw);
  if (!Number.isFinite(timestamp)) throw new Error(`${name} must be an ISO-8601 timestamp.`);
  return new Date(timestamp).toISOString();
}

function finite(value, name, { optional = false, integer = false, minimum = Number.NEGATIVE_INFINITY } = {}) {
  if ((value === undefined || value === null) && optional) return undefined;
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${name} must be finite numeric data.`);
  if (integer && !Number.isInteger(value)) throw new Error(`${name} must be an integer.`);
  if (value < minimum) throw new Error(`${name} must be at least ${minimum}.`);
  return value;
}

function secureUrl(value, name) {
  if (value === undefined || value === null || value === "") return undefined;
  const url = new URL(String(value));
  if (url.username || url.password) throw new Error(`${name} must not contain credentials.`);
  const loopback = url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  if (url.protocol !== "https:" && !loopback) throw new Error(`${name} must use HTTPS except on loopback.`);
  const sensitiveQuery = [...url.searchParams.keys()].find((key) => /token|secret|password|authorization|api.?key|email/i.test(key));
  if (sensitiveQuery) throw new Error(`${name} contains a sensitive query parameter.`);
  url.hash = "";
  return url.toString();
}

function validatePeriod(start, end, granularity, name) {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (endMs <= startMs) throw new Error(`${name}.period_end must be after period_start.`);
  const startDate = new Date(startMs);
  if (startDate.getUTCHours() !== 0 || startDate.getUTCMinutes() !== 0 || startDate.getUTCSeconds() !== 0 || startDate.getUTCMilliseconds() !== 0) {
    throw new Error(`${name}.period_start must be aligned to UTC midnight.`);
  }
  if (granularity === "day") {
    if (endMs - startMs !== 86_400_000) throw new Error(`${name} must cover exactly one UTC day.`);
    return;
  }
  if (granularity === "month") {
    if (startDate.getUTCDate() !== 1) throw new Error(`${name}.period_start must be the first UTC day of a month.`);
    const expected = Date.UTC(startDate.getUTCFullYear(), startDate.getUTCMonth() + 1, 1);
    if (endMs !== expected) throw new Error(`${name} must cover exactly one UTC calendar month.`);
    return;
  }
  throw new Error(`${name}.granularity must be day or month.`);
}

export function validateBusinessConfig(raw) {
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
  const url = secureUrl(odoo.url, "config.odoo.url");
  if (!url) throw new Error("config.odoo.url is required.");
  return { odoo: { url: url.replace(/\/$/, ""), database, apiKeyEnv } };
}

export function validateBusinessEvidence(raw, now = Date.now()) {
  const evidence = plainObject(raw, "evidence");
  rejectUnknownKeys(
    evidence,
    new Set(["environment", "source_code", "source_updated_at", "release_version", "commit_sha", "metrics"]),
    "evidence",
  );
  const environment = String(evidence.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("evidence.environment must be develop or main.");
  const sourceCode = safeCode(evidence.source_code, "evidence.source_code");
  const sourceUpdatedAt = utcTimestamp(evidence.source_updated_at, "evidence.source_updated_at");
  if (Date.parse(sourceUpdatedAt) > now + 5 * 60_000) throw new Error("evidence.source_updated_at is too far in the future.");
  const releaseVersion = evidence.release_version === undefined ? undefined : safeCode(evidence.release_version, "evidence.release_version");
  const commitSha = evidence.commit_sha === undefined ? undefined : String(evidence.commit_sha).trim().toLowerCase();
  if (commitSha !== undefined && !/^[a-f0-9]{7,64}$/.test(commitSha)) throw new Error("evidence.commit_sha must be a hexadecimal commit identifier.");
  if (commitSha && !releaseVersion) throw new Error("evidence.release_version is required when commit_sha is supplied.");
  if (!Array.isArray(evidence.metrics) || evidence.metrics.length < 1 || evidence.metrics.length > 200) {
    throw new Error("evidence.metrics must contain between 1 and 200 records.");
  }
  const metrics = evidence.metrics.map((rawItem, index) => {
    const name = `evidence.metrics[${index}]`;
    const item = plainObject(rawItem, name);
    rejectUnknownKeys(item, ITEM_FIELDS, name);
    const metricCode = safeCode(item.code, `${name}.code`);
    if (!BUSINESS_METRIC_CODES.has(metricCode)) throw new Error(`${name}.code is not an approved business metric.`);
    const periodStart = utcTimestamp(item.period_start, `${name}.period_start`);
    const periodEnd = utcTimestamp(item.period_end, `${name}.period_end`);
    const granularity = String(item.granularity || "").trim();
    validatePeriod(periodStart, periodEnd, granularity, name);
    if (Date.parse(periodEnd) > Date.parse(sourceUpdatedAt) + 5 * 60_000) {
      throw new Error(`${name}.period_end is newer than the source watermark.`);
    }
    const externalKey = String(item.external_key || "").trim();
    const expectedPrefix = `${environment}:business:${sourceCode}:`;
    if (!SAFE_EXTERNAL_KEY.test(externalKey) || !externalKey.startsWith(expectedPrefix)) {
      throw new Error(`${name}.external_key must be safe and prefixed by ${expectedPrefix}`);
    }
    const dimensions = {};
    for (const field of DIMENSION_FIELDS) {
      if (item[field] === undefined || item[field] === null || item[field] === "") continue;
      const value = safeCode(item[field], `${name}.${field}`);
      if (field === "tenant_size_band" && !TENANT_SIZE_BANDS.has(value)) throw new Error(`${name}.tenant_size_band is invalid.`);
      if (field === "incident_severity" && !INCIDENT_SEVERITIES.has(value)) throw new Error(`${name}.incident_severity is invalid.`);
      dimensions[field] = value;
    }
    const scopeKey = String(item.scope_key || "global").trim();
    if (!SAFE_CODE.test(scopeKey)) throw new Error(`${name}.scope_key must be a safe identifier.`);
    if (Object.keys(dimensions).length && scopeKey === "global") {
      throw new Error(`${name}.scope_key must be non-global when dimensions are supplied.`);
    }
    if (!Object.keys(dimensions).length && scopeKey !== "global") {
      throw new Error(`${name}.scope_key must be global when no dimensions are supplied.`);
    }
    const numerator = finite(item.numerator, `${name}.numerator`, { optional: true });
    const denominator = finite(item.denominator, `${name}.denominator`, { optional: true });
    if ((numerator === undefined) !== (denominator === undefined)) throw new Error(`${name}.numerator and denominator must be supplied together.`);
    if (numerator !== undefined && numerator > denominator) throw new Error(`${name}.numerator cannot exceed denominator.`);
    const status = String(item.status || "unknown").trim().toLowerCase();
    if (!STATUS.has(status)) throw new Error(`${name}.status is invalid.`);
    const measuredAt = item.measured_at === undefined ? periodEnd : utcTimestamp(item.measured_at, `${name}.measured_at`);
    if (Date.parse(measuredAt) > Date.parse(sourceUpdatedAt) + 5 * 60_000) throw new Error(`${name}.measured_at is newer than the source watermark.`);
    const sampleCount = finite(item.sample_count, `${name}.sample_count`, { optional: true, integer: true, minimum: 0 });
    const freshnessSeconds = finite(item.freshness_seconds, `${name}.freshness_seconds`, { optional: true, integer: true, minimum: 1 });
    if (freshnessSeconds !== undefined && freshnessSeconds > 604800) throw new Error(`${name}.freshness_seconds must not exceed 604800.`);
    const drilldownUrl = secureUrl(item.drilldown_url, `${name}.drilldown_url`);
    return {
      code: metricCode,
      value: finite(item.value, `${name}.value`),
      scope_key: scopeKey,
      status,
      measured_at: measuredAt,
      external_key: externalKey,
      period_start: periodStart,
      period_end: periodEnd,
      granularity,
      ...(numerator === undefined ? {} : { numerator, denominator }),
      ...(sampleCount === undefined ? {} : { sample_count: sampleCount }),
      ...(freshnessSeconds === undefined ? {} : { freshness_seconds: freshnessSeconds }),
      ...(drilldownUrl ? { drilldown_url: drilldownUrl } : {}),
      ...dimensions,
    };
  });
  return {
    environment,
    sourceCode,
    source_updated_at: sourceUpdatedAt,
    ...(releaseVersion ? { release_version: releaseVersion } : {}),
    ...(commitSha ? { commit_sha: commitSha } : {}),
    metrics,
  };
}

function secret(env, name) {
  const value = env[name]?.trim();
  if (!value) throw new Error(`${name} is required.`);
  return value;
}

async function boundedJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const body = await response.text();
    if (Buffer.byteLength(body) > MAX_RESPONSE_BYTES) throw new Error(`Response from ${new URL(url).origin} is too large.`);
    if (!response.ok) throw new Error(`${new URL(url).origin} returned HTTP ${response.status}.`);
    return JSON.parse(body);
  } finally {
    clearTimeout(timeout);
  }
}

export async function runBusinessSync(rawConfig, rawEvidence, { env = process.env, dryRun = false, requestJson = boundedJson } = {}) {
  const config = validateBusinessConfig(rawConfig);
  const evidence = validateBusinessEvidence(rawEvidence);
  if (dryRun) return { ok: true, dryRun: true, environment: evidence.environment, sourceCode: evidence.sourceCode, records: evidence.metrics.length };
  const headers = {
    Authorization: `Bearer ${secret(env, config.odoo.apiKeyEnv)}`,
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "Arcigy-SaaS-Business-Sync/1.0",
  };
  if (config.odoo.database) headers["X-Odoo-Database"] = config.odoo.database;
  const payload = { ...evidence };
  delete payload.sourceCode;
  const odoo = await requestJson(`${config.odoo.url}/json/2/saas.metric.current/ingest_metric_batch`, {
    method: "POST",
    headers,
    body: JSON.stringify({ payload }),
  });
  return { ok: true, dryRun: false, environment: evidence.environment, sourceCode: evidence.sourceCode, records: evidence.metrics.length, odoo };
}

async function readBoundedJson(path, name) {
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_INPUT_BYTES) throw new Error(`${name} must be a JSON file no larger than ${MAX_INPUT_BYTES} bytes.`);
  return JSON.parse(await readFile(path, "utf8"));
}

async function main() {
  const configArg = process.argv.find((arg) => arg.startsWith("--config="));
  const evidenceArg = process.argv.find((arg) => arg.startsWith("--evidence="));
  if (!configArg || !evidenceArg) throw new Error("Usage: node saas_business_sync.mjs --config=<path> --evidence=<path> [--dry-run]");
  const config = await readBoundedJson(configArg.slice("--config=".length), "config");
  const evidence = await readBoundedJson(evidenceArg.slice("--evidence=".length), "evidence");
  const result = await runBusinessSync(config, evidence, { dryRun: process.argv.includes("--dry-run") });
  console.log(JSON.stringify({ ok: result.ok, dryRun: result.dryRun, environment: result.environment, sourceCode: result.sourceCode, records: result.records }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
