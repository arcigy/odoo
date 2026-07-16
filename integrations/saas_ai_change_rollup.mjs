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
const METRIC_FIELDS = new Set([
  "code", "available", "unavailable_reason", "value", "numerator", "denominator", "sample_count",
]);

const METRIC_CONTRACTS = new Map([
  ["ai_assisted_pr_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_pr_average_diff_lines", { source: "change_inventory", type: "average", granularity: "day" }],
  ["ai_assisted_changed_file_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_modules_touched_per_change", { source: "change_inventory", type: "average", granularity: "day" }],
  ["ai_assisted_security_sensitive_file_touch_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_billing_file_touch_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_auth_file_touch_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_db_migration_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_dependency_proposal_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_dependency_rejection_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_tests_added_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_tests_modified_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_assisted_tests_deleted_count", { source: "change_inventory", type: "count", granularity: "day" }],
  ["ai_change_human_review_coverage", { source: "review_gates", type: "ratio", granularity: "day" }],
  ["ai_change_security_review_coverage", { source: "review_gates", type: "ratio", granularity: "day" }],
  ["ai_assisted_release_incident_rate", { source: "release_outcomes", type: "ratio", granularity: "month" }],
  ["ai_assisted_release_rollback_rate", { source: "release_outcomes", type: "ratio", granularity: "month" }],
  ["ai_assisted_release_hotfix_rate", { source: "release_outcomes", type: "ratio", granularity: "month" }],
  ["ai_assisted_escaped_defect_count", { source: "release_outcomes", type: "count", granularity: "month" }],
  ["ai_assisted_authorization_defect_count", { source: "release_outcomes", type: "count", granularity: "month" }],
  ["ai_assisted_performance_regression_count", { source: "release_outcomes", type: "count", granularity: "month" }],
  ["ai_assisted_query_regression_count", { source: "release_outcomes", type: "count", granularity: "month" }],
  ["ai_assisted_cost_regression_count", { source: "release_outcomes", type: "count", granularity: "month" }],
  ["ai_assisted_change_reopen_rate", { source: "release_outcomes", type: "ratio", granularity: "month" }],
  ["ai_assisted_change_repair_p50_seconds", { source: "release_outcomes", type: "duration", granularity: "month" }],
  ["ai_change_review_risk_p95_score", { source: "risk_assessment", type: "score", granularity: "day" }],
  ["ai_change_low_risk_count", { source: "risk_assessment", type: "count", granularity: "day" }],
  ["ai_change_medium_risk_count", { source: "risk_assessment", type: "count", granularity: "day" }],
  ["ai_change_high_risk_count", { source: "risk_assessment", type: "count", granularity: "day" }],
  ["ai_change_critical_review_required_count", { source: "risk_assessment", type: "count", granularity: "day" }],
]);

const SOURCE_CONTRACTS = new Map();
for (const [code, contract] of METRIC_CONTRACTS) {
  const codes = SOURCE_CONTRACTS.get(contract.source) || [];
  codes.push(code);
  SOURCE_CONTRACTS.set(contract.source, codes);
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

function finite(value, name, { integer = false, minimum = 0 } = {}) {
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

function validatePeriod(start, end, granularity) {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  const startDate = new Date(startMs);
  if (
    startDate.getUTCHours() !== 0 || startDate.getUTCMinutes() !== 0
    || startDate.getUTCSeconds() !== 0 || startDate.getUTCMilliseconds() !== 0
  ) throw new Error("AI change period_start must be aligned to UTC midnight.");
  if (granularity === "day") {
    if (endMs - startMs !== 86_400_000) throw new Error("AI change period must cover exactly one UTC day.");
    return;
  }
  if (granularity === "month") {
    const expected = Date.UTC(startDate.getUTCFullYear(), startDate.getUTCMonth() + 1, 1);
    if (startDate.getUTCDate() !== 1 || endMs !== expected) {
      throw new Error("AI change period must cover exactly one UTC calendar month.");
    }
    return;
  }
  throw new Error("AI change granularity must be day or month.");
}

function validateUnavailableMetric(metric, contract, name) {
  if (metric.available !== false) return undefined;
  if (!["ratio", "average", "duration", "score"].includes(contract.type)) {
    throw new Error(`${name} count must be emitted as zero instead of unavailable.`);
  }
  if (metric.unavailable_reason !== "no_eligible_sample") {
    throw new Error(`${name}.unavailable_reason must be no_eligible_sample.`);
  }
  const numericFields = ["value", "numerator", "denominator", "sample_count"];
  if (numericFields.some((field) => metric[field] !== undefined)) {
    throw new Error(`${name} unavailable metric must not include numeric fields.`);
  }
  return { unavailable: true };
}

function validateMetricValue(metric, contract, name) {
  const unavailable = validateUnavailableMetric(metric, contract, name);
  if (unavailable) return unavailable;
  if (metric.available !== undefined && metric.available !== true) {
    throw new Error(`${name}.available must be true or false.`);
  }
  if (metric.unavailable_reason !== undefined) {
    throw new Error(`${name}.unavailable_reason is only valid when available is false.`);
  }
  const value = finite(metric.value, `${name}.value`);
  const numerator = metric.numerator === undefined
    ? undefined
    : finite(metric.numerator, `${name}.numerator`, { integer: true });
  const denominator = metric.denominator === undefined
    ? undefined
    : finite(metric.denominator, `${name}.denominator`, { integer: true });
  const sampleCount = metric.sample_count === undefined
    ? undefined
    : finite(metric.sample_count, `${name}.sample_count`, { integer: true });

  if (contract.type === "count") {
    if (!Number.isInteger(value)) throw new Error(`${name}.value must be an integer count.`);
    if (numerator !== undefined || denominator !== undefined || sampleCount !== undefined) {
      throw new Error(`${name} count must not include ratio or sample fields.`);
    }
  } else if (contract.type === "ratio") {
    if (numerator === undefined || denominator === undefined || denominator < 1 || numerator > denominator) {
      throw new Error(`${name} ratio requires consistent numerator and denominator counts.`);
    }
    if (value > 100 || Math.abs(value - (numerator / denominator) * 100) > 1e-6) {
      throw new Error(`${name}.value does not match numerator / denominator.`);
    }
    if (sampleCount !== undefined && sampleCount !== denominator) {
      throw new Error(`${name}.sample_count must equal denominator.`);
    }
  } else {
    if (numerator !== undefined || denominator !== undefined) {
      throw new Error(`${name} ${contract.type} must not include numerator or denominator.`);
    }
    if (sampleCount === undefined || sampleCount < 1) {
      throw new Error(`${name}.sample_count is required for ${contract.type} evidence.`);
    }
    if (contract.type === "score" && value > 100) {
      throw new Error(`${name}.value must be a score between 0 and 100.`);
    }
  }
  return {
    value,
    ...(numerator === undefined ? {} : { numerator, denominator }),
    ...(contract.type === "ratio" ? { sample_count: denominator } : {}),
    ...(sampleCount === undefined ? {} : { sample_count: sampleCount }),
  };
}

export function aiChangeMetricCodes() {
  return [...METRIC_CONTRACTS.keys()].sort();
}

export function rollupAiChangeEvidence(raw, now = Date.now()) {
  const input = plainObject(raw, "AI change evidence");
  rejectUnknownKeys(input, ROOT_FIELDS, "AI change evidence");
  const environment = String(input.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("AI change evidence environment must be develop or main.");
  const compiledAt = utcTimestamp(input.compiled_at, "AI change evidence.compiled_at");
  if (Date.parse(compiledAt) > now + 5 * 60_000) throw new Error("AI change evidence.compiled_at is too far in the future.");
  const periodStart = utcTimestamp(input.period_start, "AI change evidence.period_start");
  const periodEnd = utcTimestamp(input.period_end, "AI change evidence.period_end");
  const granularity = String(input.granularity || "").trim();
  validatePeriod(periodStart, periodEnd, granularity);
  if (Date.parse(periodEnd) > Date.parse(compiledAt)) throw new Error("AI change period is not closed at the compilation watermark.");
  if (!Array.isArray(input.sources) || input.sources.length < 1 || input.sources.length > SOURCE_CONTRACTS.size) {
    throw new Error(`AI change evidence.sources must contain between 1 and ${SOURCE_CONTRACTS.size} sources.`);
  }

  const seenSources = new Set();
  const seenMetrics = new Set();
  const validatedMetrics = new Map();
  const omitted = [];
  const metrics = [];
  const periodKey = granularity === "day"
    ? periodStart.slice(0, 10).replaceAll("-", "")
    : periodStart.slice(0, 7).replace("-", "");

  for (const [sourceIndex, rawSource] of input.sources.entries()) {
    const sourceName = `AI change evidence.sources[${sourceIndex}]`;
    const source = plainObject(rawSource, sourceName);
    rejectUnknownKeys(source, SOURCE_FIELDS, sourceName);
    const sourceContract = safeCode(source.contract, `${sourceName}.contract`);
    const expectedCodes = SOURCE_CONTRACTS.get(sourceContract);
    if (!expectedCodes) throw new Error(`${sourceName}.contract is not approved.`);
    if (seenSources.has(sourceContract)) throw new Error(`${sourceName}.contract is duplicated.`);
    seenSources.add(sourceContract);
    if (source.complete !== true) {
      throw new Error(`${sourceName}.complete must be explicitly true before values including zeros can be emitted.`);
    }
    const sourceUpdatedAt = utcTimestamp(source.source_updated_at, `${sourceName}.source_updated_at`);
    if (Date.parse(sourceUpdatedAt) < Date.parse(periodEnd)) throw new Error(`${sourceName} is older than the closed period.`);
    if (Date.parse(sourceUpdatedAt) > Date.parse(compiledAt) + 5 * 60_000) {
      throw new Error(`${sourceName} is newer than the compilation watermark.`);
    }
    const drilldownUrl = secureUrl(source.drilldown_url, `${sourceName}.drilldown_url`);
    if (!Array.isArray(source.metrics) || source.metrics.length !== expectedCodes.length) {
      throw new Error(`${sourceName}.metrics must contain exactly ${expectedCodes.length} contract metrics.`);
    }

    const sourceCodes = new Set();
    for (const [metricIndex, rawMetric] of source.metrics.entries()) {
      const metricName = `${sourceName}.metrics[${metricIndex}]`;
      const metric = plainObject(rawMetric, metricName);
      rejectUnknownKeys(metric, METRIC_FIELDS, metricName);
      const metricCode = safeCode(metric.code, `${metricName}.code`);
      const contract = METRIC_CONTRACTS.get(metricCode);
      if (!contract || contract.source !== sourceContract) {
        throw new Error(`${metricName}.code does not belong to ${sourceContract}.`);
      }
      if (contract.granularity !== granularity) {
        throw new Error(`${metricName}.code requires ${contract.granularity} granularity.`);
      }
      if (sourceCodes.has(metricCode) || seenMetrics.has(metricCode)) throw new Error(`${metricName}.code is duplicated.`);
      sourceCodes.add(metricCode);
      seenMetrics.add(metricCode);
      const validated = validateMetricValue(metric, contract, metricName);
      validatedMetrics.set(metricCode, validated);
      if (validated.unavailable) {
        omitted.push(`${metricCode}:no_eligible_sample`);
        continue;
      }
      metrics.push({
        code: metricCode,
        ...validated,
        status: "unknown",
        measured_at: sourceUpdatedAt,
        freshness_seconds: 86400,
        scope_key: "global",
        external_key: `${environment}:business:ai-change-control-plane:${metricCode}:${periodKey}`,
        period_start: periodStart,
        period_end: periodEnd,
        granularity,
        ...(drilldownUrl ? { drilldown_url: drilldownUrl } : {}),
      });
    }
    const missing = expectedCodes.filter((code) => !sourceCodes.has(code));
    if (missing.length) throw new Error(`${sourceName}.metrics is missing: ${missing.join(", ")}.`);
  }

  const metric = (code) => validatedMetrics.get(code);
  const prCount = metric("ai_assisted_pr_count")?.value;
  if (prCount !== undefined) {
    for (const code of ["ai_assisted_pr_average_diff_lines", "ai_assisted_modules_touched_per_change"]) {
      const item = metric(code);
      if (prCount === 0 && !item?.unavailable) throw new Error(`${code} must be unavailable when AI-assisted PR count is zero.`);
      if (prCount > 0 && (item?.unavailable || item?.sample_count !== prCount)) {
        throw new Error(`${code}.sample_count must equal AI-assisted PR count.`);
      }
    }
    for (const code of [
      "ai_assisted_security_sensitive_file_touch_count",
      "ai_assisted_billing_file_touch_count",
      "ai_assisted_auth_file_touch_count",
    ]) {
      if (metric(code)?.value > prCount) throw new Error(`${code} cannot exceed AI-assisted PR count.`);
    }
    if (metric("ai_assisted_dependency_rejection_count")?.value > metric("ai_assisted_dependency_proposal_count")?.value) {
      throw new Error("Rejected AI dependency proposals cannot exceed proposed dependencies.");
    }
    const humanReview = metric("ai_change_human_review_coverage");
    if (humanReview) {
      if (prCount === 0 && !humanReview.unavailable) throw new Error("Human review coverage must be unavailable when AI-assisted PR count is zero.");
      if (prCount > 0 && (humanReview.unavailable || humanReview.denominator !== prCount)) {
        throw new Error("Human review coverage denominator must equal AI-assisted PR count.");
      }
    }
    const securityReview = metric("ai_change_security_review_coverage");
    if (securityReview && !securityReview.unavailable && securityReview.denominator > prCount) {
      throw new Error("Security review coverage denominator cannot exceed AI-assisted PR count.");
    }
    const riskScore = metric("ai_change_review_risk_p95_score");
    if (riskScore) {
      const classified = [
        "ai_change_low_risk_count", "ai_change_medium_risk_count",
        "ai_change_high_risk_count", "ai_change_critical_review_required_count",
      ].reduce((total, code) => total + metric(code).value, 0);
      if (classified !== prCount) throw new Error("AI risk classification counts must equal AI-assisted PR count.");
      if (prCount === 0 && !riskScore.unavailable) throw new Error("AI review risk p95 must be unavailable when AI-assisted PR count is zero.");
      if (prCount > 0 && (riskScore.unavailable || riskScore.sample_count !== prCount)) {
        throw new Error("AI review risk p95 sample_count must equal AI-assisted PR count.");
      }
    }
  }

  const releaseRates = [
    "ai_assisted_release_incident_rate",
    "ai_assisted_release_rollback_rate",
    "ai_assisted_release_hotfix_rate",
  ].map(metric).filter(Boolean);
  if (releaseRates.length) {
    const availableRates = releaseRates.filter((item) => !item.unavailable);
    if (availableRates.length !== 0 && availableRates.length !== releaseRates.length) {
      throw new Error("AI release outcome rates must share availability for the same release population.");
    }
    if (availableRates.length && new Set(availableRates.map(({ denominator }) => denominator)).size !== 1) {
      throw new Error("AI release outcome rates must share one release denominator.");
    }
  }

  if (!metrics.length) throw new Error("AI change evidence has no available metrics to emit.");

  return {
    evidence: {
      environment,
      source_code: "ai-change-control-plane",
      source_updated_at: compiledAt,
      metrics,
    },
    stats: { sources: seenSources.size, emitted: metrics.length, omitted },
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
  if (!inputArg) throw new Error("Usage: node saas_ai_change_rollup.mjs --input=<path>");
  const result = rollupAiChangeEvidence(await readBoundedJson(inputArg.slice("--input=".length)));
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
