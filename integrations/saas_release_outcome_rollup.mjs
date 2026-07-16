import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const UTC_TIMESTAMP = /(?:Z|\+00:00)$/i;
const ROOT_FIELDS = new Set([
  "environment", "compiled_at", "period_start", "period_end", "complete",
  "source_updated_at", "drilldown_url", "metrics",
]);
const METRIC_FIELDS = new Set([
  "code", "available", "unavailable_reason", "value", "numerator", "denominator", "sample_count",
]);

const METRIC_CONTRACTS = new Map([
  ["deployment_count", "count"],
  ["deployment_success_rate", "ratio"],
  ["deployment_duration_p95_seconds", "duration"],
  ["deployment_queue_p95_seconds", "duration"],
  ["deployment_change_failure_count", "count"],
  ["change_failure_rate", "ratio"],
  ["release_rollback_count", "count"],
  ["release_rollback_rate", "ratio"],
  ["rollback_attempt_count", "count"],
  ["rollback_success_rate", "ratio"],
  ["hotfix_count", "count"],
  ["release_incident_count", "count"],
  ["time_to_restore_service_seconds", "duration"],
  ["canary_failure_count", "count"],
  ["artifact_mismatch_count", "count"],
  ["environment_drift_count", "count"],
]);

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object.`);
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function utcTimestamp(value, name) {
  const raw = String(value || "").trim();
  if (!UTC_TIMESTAMP.test(raw)) throw new Error(`${name} must explicitly use UTC.`);
  const timestamp = Date.parse(raw);
  if (!Number.isFinite(timestamp)) throw new Error(`${name} must be an ISO-8601 timestamp.`);
  return new Date(timestamp).toISOString();
}

function finite(value, name, { integer = false } = {}) {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) {
    throw new Error(`${name} must be finite non-negative numeric data.`);
  }
  if (integer && !Number.isInteger(value)) throw new Error(`${name} must be an integer.`);
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

function validateDay(start, end) {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  const startDate = new Date(startMs);
  if (
    startDate.getUTCHours() !== 0 || startDate.getUTCMinutes() !== 0
    || startDate.getUTCSeconds() !== 0 || startDate.getUTCMilliseconds() !== 0
    || endMs - startMs !== 86_400_000
  ) throw new Error("Release outcome period must cover exactly one UTC day.");
}

function validateMetric(raw, type, name) {
  const metric = plainObject(raw, name);
  rejectUnknownKeys(metric, METRIC_FIELDS, name);
  if (metric.available === false) {
    if (type === "count") throw new Error(`${name} count must be emitted as zero instead of unavailable.`);
    if (metric.unavailable_reason !== "no_eligible_sample") {
      throw new Error(`${name}.unavailable_reason must be no_eligible_sample.`);
    }
    if (["value", "numerator", "denominator", "sample_count"].some((field) => metric[field] !== undefined)) {
      throw new Error(`${name} unavailable metric must not include numeric fields.`);
    }
    return { unavailable: true };
  }
  if (metric.available !== undefined && metric.available !== true) {
    throw new Error(`${name}.available must be true or false.`);
  }
  if (metric.unavailable_reason !== undefined) {
    throw new Error(`${name}.unavailable_reason is only valid when available is false.`);
  }
  const value = finite(metric.value, `${name}.value`, { integer: type === "count" });
  if (type === "count") {
    if (["numerator", "denominator", "sample_count"].some((field) => metric[field] !== undefined)) {
      throw new Error(`${name} count must not include ratio or sample fields.`);
    }
    return { value };
  }
  if (type === "ratio") {
    const numerator = finite(metric.numerator, `${name}.numerator`, { integer: true });
    const denominator = finite(metric.denominator, `${name}.denominator`, { integer: true });
    if (denominator < 1 || numerator > denominator) throw new Error(`${name} ratio counts are inconsistent.`);
    if (value > 100 || Math.abs(value - (numerator / denominator) * 100) > 1e-6) {
      throw new Error(`${name}.value does not match numerator / denominator.`);
    }
    if (metric.sample_count !== undefined && metric.sample_count !== denominator) {
      throw new Error(`${name}.sample_count must equal denominator.`);
    }
    return { value, numerator, denominator, sample_count: denominator };
  }
  if (metric.numerator !== undefined || metric.denominator !== undefined) {
    throw new Error(`${name} duration must not include numerator or denominator.`);
  }
  const sampleCount = finite(metric.sample_count, `${name}.sample_count`, { integer: true });
  if (sampleCount < 1) throw new Error(`${name}.sample_count must be at least 1.`);
  return { value, sample_count: sampleCount };
}

function assertAvailablePopulation(item, count, name) {
  if (count === 0 && !item.unavailable) throw new Error(`${name} must be unavailable when its population is zero.`);
  if (count > 0 && item.unavailable) throw new Error(`${name} must be available when its population is non-zero.`);
}

export function releaseOutcomeMetricCodes() {
  return [...METRIC_CONTRACTS.keys()].sort();
}

export function rollupReleaseOutcomeEvidence(raw, now = Date.now()) {
  const input = plainObject(raw, "release outcome evidence");
  rejectUnknownKeys(input, ROOT_FIELDS, "release outcome evidence");
  const environment = String(input.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("release outcome evidence.environment must be develop or main.");
  if (input.complete !== true) {
    throw new Error("release outcome evidence.complete must be explicitly true before values including zeros can be emitted.");
  }
  const compiledAt = utcTimestamp(input.compiled_at, "release outcome evidence.compiled_at");
  if (Date.parse(compiledAt) > now + 5 * 60_000) throw new Error("release outcome evidence.compiled_at is too far in the future.");
  const periodStart = utcTimestamp(input.period_start, "release outcome evidence.period_start");
  const periodEnd = utcTimestamp(input.period_end, "release outcome evidence.period_end");
  validateDay(periodStart, periodEnd);
  if (Date.parse(periodEnd) > Date.parse(compiledAt)) throw new Error("release outcome period is not closed at the compilation watermark.");
  const sourceUpdatedAt = utcTimestamp(input.source_updated_at, "release outcome evidence.source_updated_at");
  if (Date.parse(sourceUpdatedAt) < Date.parse(periodEnd)) throw new Error("release outcome source is older than the closed period.");
  if (Date.parse(sourceUpdatedAt) > Date.parse(compiledAt) + 5 * 60_000) {
    throw new Error("release outcome source is newer than the compilation watermark.");
  }
  const drilldownUrl = secureUrl(input.drilldown_url, "release outcome evidence.drilldown_url");
  if (!Array.isArray(input.metrics) || input.metrics.length !== METRIC_CONTRACTS.size) {
    throw new Error(`release outcome evidence.metrics must contain exactly ${METRIC_CONTRACTS.size} contract metrics.`);
  }

  const values = new Map();
  for (const [index, rawMetric] of input.metrics.entries()) {
    const name = `release outcome evidence.metrics[${index}]`;
    const code = String(rawMetric?.code || "").trim();
    const type = METRIC_CONTRACTS.get(code);
    if (!type) throw new Error(`${name}.code is not an approved release outcome metric.`);
    if (values.has(code)) throw new Error(`${name}.code is duplicated.`);
    values.set(code, validateMetric(rawMetric, type, name));
  }
  const missing = [...METRIC_CONTRACTS.keys()].filter((code) => !values.has(code));
  if (missing.length) throw new Error(`release outcome evidence.metrics is missing: ${missing.join(", ")}.`);

  const deployments = values.get("deployment_count").value;
  for (const code of [
    "deployment_change_failure_count", "release_rollback_count", "hotfix_count",
    "release_incident_count", "canary_failure_count", "artifact_mismatch_count",
  ]) {
    if (values.get(code).value > deployments) throw new Error(`${code} cannot exceed deployment_count.`);
  }
  for (const code of ["deployment_success_rate", "change_failure_rate", "release_rollback_rate"]) {
    const item = values.get(code);
    assertAvailablePopulation(item, deployments, code);
    if (!item.unavailable && item.denominator !== deployments) {
      throw new Error(`${code}.denominator must equal deployment_count.`);
    }
  }
  for (const code of ["deployment_duration_p95_seconds", "deployment_queue_p95_seconds"]) {
    const item = values.get(code);
    assertAvailablePopulation(item, deployments, code);
    if (!item.unavailable && item.sample_count > deployments) {
      throw new Error(`${code}.sample_count cannot exceed deployment_count.`);
    }
  }
  if (values.get("change_failure_rate").numerator !== undefined
      && values.get("change_failure_rate").numerator !== values.get("deployment_change_failure_count").value) {
    throw new Error("change_failure_rate numerator must equal deployment_change_failure_count.");
  }
  if (values.get("release_rollback_rate").numerator !== undefined
      && values.get("release_rollback_rate").numerator !== values.get("release_rollback_count").value) {
    throw new Error("release_rollback_rate numerator must equal release_rollback_count.");
  }

  const rollbackAttempts = values.get("rollback_attempt_count").value;
  const rollbackSuccess = values.get("rollback_success_rate");
  assertAvailablePopulation(rollbackSuccess, rollbackAttempts, "rollback_success_rate");
  if (!rollbackSuccess.unavailable) {
    if (rollbackSuccess.denominator !== rollbackAttempts) {
      throw new Error("rollback_success_rate denominator must equal rollback_attempt_count.");
    }
    if (rollbackSuccess.numerator !== values.get("release_rollback_count").value) {
      throw new Error("rollback_success_rate numerator must equal release_rollback_count.");
    }
  } else if (values.get("release_rollback_count").value !== 0) {
    throw new Error("release_rollback_count must be zero when there are no rollback attempts.");
  }

  const incidentCount = values.get("release_incident_count").value;
  const restoreTime = values.get("time_to_restore_service_seconds");
  assertAvailablePopulation(restoreTime, incidentCount, "time_to_restore_service_seconds");
  if (!restoreTime.unavailable && restoreTime.sample_count > incidentCount) {
    throw new Error("time_to_restore_service_seconds.sample_count cannot exceed release_incident_count.");
  }

  const periodKey = periodStart.slice(0, 10).replaceAll("-", "");
  const omitted = [];
  const metrics = [];
  for (const [code, item] of values) {
    if (item.unavailable) {
      omitted.push(`${code}:no_eligible_sample`);
      continue;
    }
    metrics.push({
      code,
      ...item,
      status: "unknown",
      measured_at: sourceUpdatedAt,
      freshness_seconds: 86400,
      scope_key: "global",
      external_key: `${environment}:business:release-control-plane:${code}:${periodKey}`,
      period_start: periodStart,
      period_end: periodEnd,
      granularity: "day",
      ...(drilldownUrl ? { drilldown_url: drilldownUrl } : {}),
    });
  }
  return {
    evidence: {
      environment,
      source_code: "release-control-plane",
      source_updated_at: compiledAt,
      metrics,
    },
    stats: { emitted: metrics.length, omitted },
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
  if (!inputArg) throw new Error("Usage: node saas_release_outcome_rollup.mjs --input=<path>");
  const result = rollupReleaseOutcomeEvidence(await readBoundedJson(inputArg.slice("--input=".length)));
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
