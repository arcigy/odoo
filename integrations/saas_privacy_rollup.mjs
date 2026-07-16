import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SAFE_CODE = /^[A-Za-z0-9._:-]{1,120}$/;
const UTC_TIMESTAMP = /(?:Z|\+00:00)$/i;
const ROOT_FIELDS = new Set([
  "environment", "compiled_at", "period_start", "period_end", "granularity", "sources",
]);
const SOURCE_FIELDS = new Set([
  "contract", "complete", "source_updated_at", "drilldown_url", "metrics",
]);
const METRIC_FIELDS = new Set(["code", "value", "numerator", "denominator", "sample_count"]);

const METRIC_CONTRACTS = new Map([
  ["pii_field_count", { source: "data_inventory", type: "count", granularity: "day" }],
  ["unclassified_data_field_count", { source: "data_inventory", type: "count", granularity: "day" }],
  ["open_data_subject_requests", { source: "privacy_workflow", type: "count", granularity: "day" }],
  ["overdue_data_subject_requests", { source: "privacy_workflow", type: "count", granularity: "day" }],
  ["dsr_completion_p95_seconds", { source: "privacy_workflow", type: "duration", granularity: "day" }],
  ["records_past_retention_limit", { source: "retention_jobs", type: "count", granularity: "day" }],
  ["retention_job_failure_count", { source: "retention_jobs", type: "count", granularity: "day" }],
  ["tracking_consent_rate", { source: "consent_registry", type: "ratio", granularity: "day" }],
  ["tracking_without_valid_consent", { source: "privacy_audit", type: "count", granularity: "day" }],
  ["access_review_completion", { source: "governance", type: "ratio", granularity: "month" }],
  ["subprocessor_review_compliance", { source: "governance", type: "ratio", granularity: "month" }],
]);
const SOURCE_CONTRACTS = new Set([...METRIC_CONTRACTS.values()].map(({ source }) => source));

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

function finite(value, name, { integer = false, minimum = 0 } = {}) {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${name} must be finite numeric data.`);
  if (integer && !Number.isInteger(value)) throw new Error(`${name} must be an integer.`);
  if (value < minimum) throw new Error(`${name} must be at least ${minimum}.`);
  return value;
}

function optionalInteger(value, name) {
  if (value === undefined || value === null) return undefined;
  return finite(value, name, { integer: true });
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

function validatePeriod(start, end, granularity) {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  const startDate = new Date(startMs);
  if (
    startDate.getUTCHours() !== 0 || startDate.getUTCMinutes() !== 0
    || startDate.getUTCSeconds() !== 0 || startDate.getUTCMilliseconds() !== 0
  ) throw new Error("privacy.period_start must be aligned to UTC midnight.");
  if (granularity === "day") {
    if (endMs - startMs !== 86_400_000) throw new Error("privacy period must cover exactly one UTC day.");
    return;
  }
  if (granularity === "month") {
    const expected = Date.UTC(startDate.getUTCFullYear(), startDate.getUTCMonth() + 1, 1);
    if (startDate.getUTCDate() !== 1 || endMs !== expected) {
      throw new Error("privacy period must cover exactly one UTC calendar month.");
    }
    return;
  }
  throw new Error("privacy.granularity must be day or month.");
}

function validateMetricValue(rawMetric, contract, name) {
  const value = finite(rawMetric.value, `${name}.value`);
  const numerator = rawMetric.numerator === undefined
    ? undefined
    : finite(rawMetric.numerator, `${name}.numerator`, { integer: true });
  const denominator = rawMetric.denominator === undefined
    ? undefined
    : finite(rawMetric.denominator, `${name}.denominator`, { integer: true });
  const sampleCount = optionalInteger(rawMetric.sample_count, `${name}.sample_count`);

  if (contract.type === "count") {
    if (!Number.isInteger(value)) throw new Error(`${name}.value must be an integer count.`);
    if (numerator !== undefined || denominator !== undefined) throw new Error(`${name} count must not include numerator or denominator.`);
  } else if (contract.type === "duration") {
    if (numerator !== undefined || denominator !== undefined) throw new Error(`${name} duration must not include numerator or denominator.`);
    if (sampleCount === undefined || sampleCount < 1) throw new Error(`${name}.sample_count is required for a percentile duration.`);
  } else {
    if (numerator === undefined || denominator === undefined) throw new Error(`${name} ratio requires numerator and denominator.`);
    if (denominator < 1 || numerator > denominator) throw new Error(`${name} ratio counts are inconsistent.`);
    if (value > 100) throw new Error(`${name}.value must be a percentage between 0 and 100.`);
    const expected = (numerator / denominator) * 100;
    if (Math.abs(value - expected) > 1e-6) throw new Error(`${name}.value does not match numerator / denominator.`);
  }
  return {
    value,
    ...(numerator === undefined ? {} : { numerator, denominator }),
    ...(sampleCount === undefined ? {} : { sample_count: sampleCount }),
  };
}

export function privacyMetricCodes() {
  return [...METRIC_CONTRACTS.keys()].sort();
}

export function rollupPrivacyEvidence(raw, now = Date.now()) {
  const input = plainObject(raw, "privacy");
  rejectUnknownKeys(input, ROOT_FIELDS, "privacy");
  const environment = String(input.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("privacy.environment must be develop or main.");
  const compiledAt = utcTimestamp(input.compiled_at, "privacy.compiled_at");
  if (Date.parse(compiledAt) > now + 5 * 60_000) throw new Error("privacy.compiled_at is too far in the future.");
  const periodStart = utcTimestamp(input.period_start, "privacy.period_start");
  const periodEnd = utcTimestamp(input.period_end, "privacy.period_end");
  const granularity = String(input.granularity || "").trim();
  validatePeriod(periodStart, periodEnd, granularity);
  if (Date.parse(periodEnd) > Date.parse(compiledAt)) throw new Error("privacy period is not closed at the compilation watermark.");
  if (!Array.isArray(input.sources) || input.sources.length < 1 || input.sources.length > SOURCE_CONTRACTS.size) {
    throw new Error(`privacy.sources must contain between 1 and ${SOURCE_CONTRACTS.size} sources.`);
  }

  const seenSources = new Set();
  const seenMetrics = new Set();
  const periodKey = granularity === "day"
    ? periodStart.slice(0, 10).replaceAll("-", "")
    : periodStart.slice(0, 7).replace("-", "");
  const metrics = [];
  for (const [sourceIndex, rawSource] of input.sources.entries()) {
    const sourceName = `privacy.sources[${sourceIndex}]`;
    const source = plainObject(rawSource, sourceName);
    rejectUnknownKeys(source, SOURCE_FIELDS, sourceName);
    const sourceContract = safeCode(source.contract, `${sourceName}.contract`);
    if (!SOURCE_CONTRACTS.has(sourceContract)) throw new Error(`${sourceName}.contract is not approved.`);
    if (seenSources.has(sourceContract)) throw new Error(`${sourceName}.contract is duplicated.`);
    seenSources.add(sourceContract);
    if (source.complete !== true) throw new Error(`${sourceName}.complete must be explicitly true before values, including zeros, can be emitted.`);
    const sourceUpdatedAt = utcTimestamp(source.source_updated_at, `${sourceName}.source_updated_at`);
    if (Date.parse(sourceUpdatedAt) < Date.parse(periodEnd)) throw new Error(`${sourceName} is older than the closed period.`);
    if (Date.parse(sourceUpdatedAt) > Date.parse(compiledAt) + 5 * 60_000) throw new Error(`${sourceName} is newer than the compilation watermark.`);
    const drilldownUrl = secureUrl(source.drilldown_url, `${sourceName}.drilldown_url`);
    if (!Array.isArray(source.metrics) || source.metrics.length < 1 || source.metrics.length > METRIC_CONTRACTS.size) {
      throw new Error(`${sourceName}.metrics must contain between 1 and ${METRIC_CONTRACTS.size} metrics.`);
    }
    for (const [metricIndex, rawMetric] of source.metrics.entries()) {
      const metricName = `${sourceName}.metrics[${metricIndex}]`;
      const metric = plainObject(rawMetric, metricName);
      rejectUnknownKeys(metric, METRIC_FIELDS, metricName);
      const metricCode = safeCode(metric.code, `${metricName}.code`);
      const contract = METRIC_CONTRACTS.get(metricCode);
      if (!contract) throw new Error(`${metricName}.code is not an approved privacy metric.`);
      if (contract.source !== sourceContract) throw new Error(`${metricName}.code does not belong to ${sourceContract}.`);
      if (contract.granularity !== granularity) throw new Error(`${metricName}.code requires ${contract.granularity} granularity.`);
      if (seenMetrics.has(metricCode)) throw new Error(`${metricName}.code is duplicated.`);
      seenMetrics.add(metricCode);
      metrics.push({
        code: metricCode,
        ...validateMetricValue(metric, contract, metricName),
        status: "unknown",
        measured_at: sourceUpdatedAt,
        freshness_seconds: 86400,
        scope_key: "global",
        external_key: `${environment}:business:privacy-control-plane:${metricCode}:${periodKey}`,
        period_start: periodStart,
        period_end: periodEnd,
        granularity,
        ...(drilldownUrl ? { drilldown_url: drilldownUrl } : {}),
      });
    }
  }
  return {
    evidence: {
      environment,
      source_code: "privacy-control-plane",
      source_updated_at: compiledAt,
      metrics,
    },
    stats: { sources: seenSources.size, metrics: metrics.length, granularity },
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
  if (!inputArg) throw new Error("Usage: node saas_privacy_rollup.mjs --input=<path>");
  const result = rollupPrivacyEvidence(await readBoundedJson(inputArg.slice("--input=".length)));
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
