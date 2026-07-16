import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SAFE_CODE = /^[A-Za-z0-9._:-]{1,120}$/;
const UTC_TIMESTAMP = /(?:Z|\+00:00)$/i;
const CHECK_CODES = new Set([
  "payment_provider_vs_odoo_invoices",
  "app_subscription_vs_billing_provider",
  "measured_usage_vs_invoiced_usage",
  "app_tenant_vs_odoo_partner",
  "active_seats_vs_paid_seats",
  "cloud_invoice_vs_cost_import",
  "observability_requests_vs_business_totals",
]);
const COUNT_UNITS = new Set(["count", "tenants", "partners", "seats", "requests", "events"]);
const CHECK_FIELDS = new Set([
  "code",
  "authoritative_source",
  "comparison_source",
  "unit",
  "authoritative_value",
  "comparison_value",
  "tolerance_absolute",
  "tolerance_relative",
  "warning_multiplier",
  "drilldown_url",
]);

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object.`);
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function code(value, name) {
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

function finite(value, name, minimum = Number.NEGATIVE_INFINITY) {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${name} must be finite numeric data.`);
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

function statusForDifference(absoluteDifference, reference, absoluteTolerance, relativeTolerance, warningMultiplier) {
  const relativeDifference = reference === 0 ? (absoluteDifference === 0 ? 0 : Number.POSITIVE_INFINITY) : absoluteDifference / Math.abs(reference);
  if (absoluteDifference <= absoluteTolerance || relativeDifference <= relativeTolerance) return "valid";
  if (absoluteDifference <= absoluteTolerance * warningMultiplier || relativeDifference <= relativeTolerance * warningMultiplier) return "warning";
  return "invalid";
}

export function rollupReconciliation(raw) {
  const input = plainObject(raw, "reconciliation");
  rejectUnknownKeys(
    input,
    new Set(["environment", "source_updated_at", "period_start", "period_end", "checks"]),
    "reconciliation",
  );
  const environment = String(input.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("reconciliation.environment must be develop or main.");
  const sourceUpdatedAt = utcTimestamp(input.source_updated_at, "reconciliation.source_updated_at");
  const periodStart = utcTimestamp(input.period_start, "reconciliation.period_start");
  const periodEnd = utcTimestamp(input.period_end, "reconciliation.period_end");
  const duration = Date.parse(periodEnd) - Date.parse(periodStart);
  if (duration <= 0 || duration > 366 * 86_400_000) {
    throw new Error("reconciliation period must be positive and no longer than 366 days.");
  }
  if (Date.parse(periodEnd) > Date.parse(sourceUpdatedAt) + 5 * 60_000) {
    throw new Error("reconciliation.period_end is newer than the source watermark.");
  }
  if (!Array.isArray(input.checks) || input.checks.length < 1 || input.checks.length > CHECK_CODES.size) {
    throw new Error(`reconciliation.checks must contain between 1 and ${CHECK_CODES.size} checks.`);
  }
  const seen = new Set();
  const statuses = { valid: 0, warning: 0, invalid: 0 };
  const periodKey = periodStart.replace(/[-:.TZ]/g, "").slice(0, 14);
  const items = input.checks.map((rawCheck, index) => {
    const name = `reconciliation.checks[${index}]`;
    const check = plainObject(rawCheck, name);
    rejectUnknownKeys(check, CHECK_FIELDS, name);
    const checkCode = code(check.code, `${name}.code`);
    if (!CHECK_CODES.has(checkCode)) throw new Error(`${name}.code is not an approved reconciliation.`);
    if (seen.has(checkCode)) throw new Error(`${name}.code is duplicated.`);
    seen.add(checkCode);
    const authoritativeSource = code(check.authoritative_source, `${name}.authoritative_source`);
    const comparisonSource = code(check.comparison_source, `${name}.comparison_source`);
    if (authoritativeSource === comparisonSource) throw new Error(`${name} must compare two distinct sources.`);
    const unit = code(check.unit, `${name}.unit`);
    const minimum = COUNT_UNITS.has(unit) ? 0 : Number.NEGATIVE_INFINITY;
    const authoritativeValue = finite(check.authoritative_value, `${name}.authoritative_value`, minimum);
    const comparisonValue = finite(check.comparison_value, `${name}.comparison_value`, minimum);
    if (COUNT_UNITS.has(unit) && (!Number.isInteger(authoritativeValue) || !Number.isInteger(comparisonValue))) {
      throw new Error(`${name} count values must be integers.`);
    }
    const absoluteTolerance = finite(check.tolerance_absolute, `${name}.tolerance_absolute`, 0);
    const relativeTolerance = finite(check.tolerance_relative, `${name}.tolerance_relative`, 0);
    if (relativeTolerance > 1) throw new Error(`${name}.tolerance_relative must be between 0 and 1.`);
    const warningMultiplier = check.warning_multiplier === undefined
      ? 2
      : finite(check.warning_multiplier, `${name}.warning_multiplier`, 1);
    if (warningMultiplier > 10) throw new Error(`${name}.warning_multiplier must not exceed 10.`);
    const difference = comparisonValue - authoritativeValue;
    const status = statusForDifference(Math.abs(difference), authoritativeValue, absoluteTolerance, relativeTolerance, warningMultiplier);
    statuses[status] += 1;
    const drilldownUrl = secureUrl(check.drilldown_url, `${name}.drilldown_url`);
    return {
      external_key: `${environment}:reconciliation:${checkCode}:${periodKey}`,
      name: `Reconciliation ${checkCode} (${unit})`,
      started_at: periodEnd,
      finished_at: sourceUpdatedAt,
      status,
      events_sent: 1,
      events_received: 1,
      events_processed: 1,
      events_rejected: status === "valid" ? 0 : 1,
      duplicate_count: 0,
      schema_failure_count: 0,
      missing_field_count: 0,
      late_event_count: 0,
      unknown_tenant_count: 0,
      reconciliation_difference: difference,
      ...(drilldownUrl ? { drilldown_url: drilldownUrl } : {}),
    };
  });
  return {
    evidence: {
      model: "saas.data.quality.run",
      environment,
      source_updated_at: sourceUpdatedAt,
      items,
    },
    stats: { checks: items.length, statuses },
  };
}

async function readBoundedJson(path) {
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_INPUT_BYTES) {
    throw new Error(`input must be a JSON file no larger than ${MAX_INPUT_BYTES} bytes.`);
  }
  return JSON.parse(await readFile(path, "utf8"));
}

async function main() {
  const inputArg = process.argv.find((arg) => arg.startsWith("--input="));
  if (!inputArg) throw new Error("Usage: node saas_reconciliation_rollup.mjs --input=<path>");
  const result = rollupReconciliation(await readBoundedJson(inputArg.slice("--input=".length)));
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
