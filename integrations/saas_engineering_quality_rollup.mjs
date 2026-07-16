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
  ["open_pr_count", { source: "pull_requests", type: "count" }],
  ["pr_cycle_time_p50_seconds", { source: "pull_requests", type: "duration" }],
  ["pr_time_to_first_review_p50_seconds", { source: "pull_requests", type: "duration" }],
  ["pr_approval_to_merge_p50_seconds", { source: "pull_requests", type: "duration" }],
  ["pr_average_diff_lines", { source: "pull_requests", type: "average" }],
  ["pr_average_files_changed", { source: "pull_requests", type: "average" }],
  ["pr_average_modules_changed", { source: "pull_requests", type: "average" }],
  ["pr_average_comment_count", { source: "pull_requests", type: "average" }],
  ["pr_requested_changes_count", { source: "pull_requests", type: "count" }],
  ["stale_pr_count", { source: "pull_requests", type: "count" }],
  ["reopened_pr_count", { source: "pull_requests", type: "count" }],

  ["branch_age_average_seconds", { source: "branches", type: "duration" }],
  ["branch_oldest_age_seconds", { source: "branches", type: "duration" }],
  ["stale_branch_count", { source: "branches", type: "count" }],
  ["merge_conflict_count", { source: "branches", type: "count" }],
  ["direct_main_push_count", { source: "branches", type: "count" }],
  ["branch_protection_bypass_count", { source: "branches", type: "count" }],
  ["failed_required_check_count", { source: "branches", type: "count" }],
  ["emergency_merge_count", { source: "branches", type: "count" }],
  ["unreviewed_production_change_count", { source: "branches", type: "count" }],

  ["build_count", { source: "ci", type: "count" }],
  ["build_duration_p95_seconds", { source: "ci", type: "duration" }],
  ["build_queue_p95_seconds", { source: "ci", type: "duration" }],
  ["flaky_job_rate", { source: "ci", type: "ratio" }],

  ["unit_test_pass_rate", { source: "tests", type: "ratio" }],
  ["integration_test_pass_rate", { source: "tests", type: "ratio" }],
  ["e2e_test_pass_rate", { source: "tests", type: "ratio" }],
  ["performance_test_pass_rate", { source: "tests", type: "ratio" }],
  ["security_test_pass_rate", { source: "tests", type: "ratio" }],
  ["flaky_test_count", { source: "tests", type: "count" }],
  ["flaky_test_age_seconds", { source: "tests", type: "duration" }],
  ["ignored_test_count", { source: "tests", type: "count" }],
  ["skipped_test_count", { source: "tests", type: "count" }],
  ["test_duration_p95_seconds", { source: "tests", type: "duration" }],
  ["critical_path_coverage_rate", { source: "tests", type: "ratio" }],
  ["mutation_score_rate", { source: "tests", type: "ratio" }],
  ["bug_reopen_rate", { source: "tests", type: "ratio" }],

  ["active_feature_flag_count", { source: "feature_flags", type: "count" }],
  ["feature_flag_without_owner_count", { source: "feature_flags", type: "count" }],
  ["feature_flag_without_expiry_count", { source: "feature_flags", type: "count" }],
  ["feature_flag_age_p95_seconds", { source: "feature_flags", type: "duration" }],
  ["permanent_feature_flag_count", { source: "feature_flags", type: "count" }],
  ["feature_flag_untested_on_count", { source: "feature_flags", type: "count" }],
  ["feature_flag_untested_off_count", { source: "feature_flags", type: "count" }],
  ["feature_flag_rollback_count", { source: "feature_flags", type: "count" }],

  ["module_boundary_violation_count", { source: "architecture", type: "count" }],
  ["cyclic_dependency_count", { source: "architecture", type: "count" }],
  ["duplicate_code_rate", { source: "architecture", type: "ratio" }],
  ["complexity_regression_rate", { source: "architecture", type: "ratio" }],
  ["oversized_file_count", { source: "architecture", type: "count" }],
  ["oversized_function_count", { source: "architecture", type: "count" }],
  ["dead_code_finding_count", { source: "architecture", type: "count" }],
  ["unused_dependency_count", { source: "architecture", type: "count" }],
  ["todo_count", { source: "architecture", type: "count" }],
  ["tech_debt_item_count", { source: "architecture", type: "count" }],
  ["tech_debt_age_average_seconds", { source: "architecture", type: "duration" }],
  ["sensitive_code_without_codeowner_review_count", { source: "architecture", type: "count" }],
]);

const SOURCE_CONTRACTS = new Map();
for (const [code, contract] of METRIC_CONTRACTS) {
  const codes = SOURCE_CONTRACTS.get(contract.source) || [];
  codes.push(code);
  SOURCE_CONTRACTS.set(contract.source, codes);
}

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
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${name} must be finite numeric data.`);
  }
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
  const sensitiveQuery = [...url.searchParams.keys()].find(
    (key) => /token|secret|password|authorization|api.?key|email/i.test(key),
  );
  if (sensitiveQuery) throw new Error(`${name} contains a sensitive query parameter.`);
  url.hash = "";
  return url.toString();
}

function validateDay(start, end) {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  const date = new Date(startMs);
  if (
    date.getUTCHours() !== 0 || date.getUTCMinutes() !== 0
    || date.getUTCSeconds() !== 0 || date.getUTCMilliseconds() !== 0
  ) {
    throw new Error("Engineering quality period_start must be aligned to UTC midnight.");
  }
  if (endMs - startMs !== 86_400_000) {
    throw new Error("Engineering quality period must cover exactly one UTC day.");
  }
}

function validateUnavailable(metric, contract, name) {
  if (metric.available !== false) return undefined;
  if (contract.type === "count") {
    throw new Error(`${name} count must be emitted as zero instead of unavailable.`);
  }
  if (metric.unavailable_reason !== "no_eligible_sample") {
    throw new Error(`${name}.unavailable_reason must be no_eligible_sample.`);
  }
  if (["value", "numerator", "denominator", "sample_count"].some((field) => metric[field] !== undefined)) {
    throw new Error(`${name} unavailable metric must not include numeric fields.`);
  }
  return { unavailable: true };
}

function validateMetric(metric, contract, name) {
  const unavailable = validateUnavailable(metric, contract, name);
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
    return { value };
  }
  if (contract.type === "ratio") {
    if (numerator === undefined || denominator === undefined || denominator < 1 || numerator > denominator) {
      throw new Error(`${name} ratio requires consistent numerator and denominator counts.`);
    }
    if (value > 100 || Math.abs(value - (numerator / denominator) * 100) > 1e-6) {
      throw new Error(`${name}.value does not match numerator / denominator.`);
    }
    if (sampleCount !== undefined && sampleCount !== denominator) {
      throw new Error(`${name}.sample_count must equal denominator.`);
    }
    return { value, numerator, denominator, sample_count: denominator };
  }
  if (numerator !== undefined || denominator !== undefined) {
    throw new Error(`${name} ${contract.type} must not include numerator or denominator.`);
  }
  if (sampleCount === undefined || sampleCount < 1) {
    throw new Error(`${name}.sample_count is required for ${contract.type} evidence.`);
  }
  return { value, sample_count: sampleCount };
}

function assertUnavailableWhenZero(metric, population, code) {
  if (population === 0 && !metric?.unavailable) {
    throw new Error(`${code} must be unavailable when its eligible population is zero.`);
  }
  if (population > 0 && metric?.unavailable) {
    throw new Error(`${code} cannot be unavailable when its eligible population is positive.`);
  }
}

export function engineeringQualityMetricCodes() {
  return [...METRIC_CONTRACTS.keys()].sort();
}

export function rollupEngineeringQualityEvidence(raw, now = Date.now()) {
  const input = plainObject(raw, "engineering quality evidence");
  rejectUnknownKeys(input, ROOT_FIELDS, "engineering quality evidence");
  const environment = String(input.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) {
    throw new Error("Engineering quality environment must be develop or main.");
  }
  const compiledAt = utcTimestamp(input.compiled_at, "engineering quality evidence.compiled_at");
  if (Date.parse(compiledAt) > now + 5 * 60_000) {
    throw new Error("Engineering quality compiled_at is too far in the future.");
  }
  const periodStart = utcTimestamp(input.period_start, "engineering quality evidence.period_start");
  const periodEnd = utcTimestamp(input.period_end, "engineering quality evidence.period_end");
  const granularity = String(input.granularity || "").trim();
  if (granularity !== "day") throw new Error("Engineering quality granularity must be day.");
  validateDay(periodStart, periodEnd);
  if (Date.parse(periodEnd) > Date.parse(compiledAt)) {
    throw new Error("Engineering quality period is not closed at the compilation watermark.");
  }
  if (!Array.isArray(input.sources) || input.sources.length !== SOURCE_CONTRACTS.size) {
    throw new Error(`Engineering quality sources must contain exactly ${SOURCE_CONTRACTS.size} contracts.`);
  }

  const seenSources = new Set();
  const seenMetrics = new Set();
  const validatedMetrics = new Map();
  const metrics = [];
  const omitted = [];
  const periodKey = periodStart.slice(0, 10).replaceAll("-", "");

  for (const [sourceIndex, rawSource] of input.sources.entries()) {
    const sourceName = `engineering quality evidence.sources[${sourceIndex}]`;
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
    if (Date.parse(sourceUpdatedAt) < Date.parse(periodEnd)) {
      throw new Error(`${sourceName} is older than the closed period.`);
    }
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
      const code = safeCode(metric.code, `${metricName}.code`);
      const contract = METRIC_CONTRACTS.get(code);
      if (!contract || contract.source !== sourceContract) {
        throw new Error(`${metricName}.code does not belong to ${sourceContract}.`);
      }
      if (sourceCodes.has(code) || seenMetrics.has(code)) throw new Error(`${metricName}.code is duplicated.`);
      sourceCodes.add(code);
      seenMetrics.add(code);
      const validated = validateMetric(metric, contract, metricName);
      validatedMetrics.set(code, validated);
      if (validated.unavailable) {
        omitted.push(`${code}:no_eligible_sample`);
        continue;
      }
      metrics.push({
        code,
        ...validated,
        status: "unknown",
        measured_at: sourceUpdatedAt,
        freshness_seconds: 86400,
        scope_key: "global",
        external_key: `${environment}:business:engineering-quality:${code}:${periodKey}`,
        period_start: periodStart,
        period_end: periodEnd,
        granularity: "day",
        ...(drilldownUrl ? { drilldown_url: drilldownUrl } : {}),
      });
    }
    const missing = expectedCodes.filter((code) => !sourceCodes.has(code));
    if (missing.length) throw new Error(`${sourceName}.metrics is missing: ${missing.join(", ")}.`);
  }
  if (seenSources.size !== SOURCE_CONTRACTS.size || seenMetrics.size !== METRIC_CONTRACTS.size) {
    throw new Error("Engineering quality evidence must include every approved source and metric contract.");
  }

  const metric = (code) => validatedMetrics.get(code);
  const prPopulation = metric("pr_cycle_time_p50_seconds")?.sample_count || 0;
  for (const code of [
    "pr_average_diff_lines", "pr_average_files_changed", "pr_average_modules_changed",
    "pr_average_comment_count",
  ]) {
    const item = metric(code);
    assertUnavailableWhenZero(item, prPopulation, code);
    if (prPopulation > 0 && item.sample_count !== prPopulation) {
      throw new Error(`${code}.sample_count must equal the completed PR population.`);
    }
  }
  for (const code of ["pr_time_to_first_review_p50_seconds", "pr_approval_to_merge_p50_seconds"]) {
    const item = metric(code);
    if (!item.unavailable && item.sample_count > prPopulation) {
      throw new Error(`${code}.sample_count cannot exceed the completed PR population.`);
    }
  }
  if (metric("stale_pr_count").value > metric("open_pr_count").value) {
    throw new Error("Stale PR count cannot exceed open PR count.");
  }
  if (metric("reopened_pr_count").value > prPopulation) {
    throw new Error("Reopened PR count cannot exceed the completed PR population.");
  }

  const branchAverage = metric("branch_age_average_seconds");
  const branchOldest = metric("branch_oldest_age_seconds");
  if (branchAverage.unavailable !== branchOldest.unavailable) {
    throw new Error("Branch age average and oldest age must share availability.");
  }
  if (!branchAverage.unavailable) {
    if (branchAverage.sample_count !== branchOldest.sample_count) {
      throw new Error("Branch age metrics must share one branch population.");
    }
    if (branchOldest.value < branchAverage.value) {
      throw new Error("Oldest branch age cannot be lower than average branch age.");
    }
    if (metric("stale_branch_count").value > branchAverage.sample_count) {
      throw new Error("Stale branch count cannot exceed the branch population.");
    }
  } else if (metric("stale_branch_count").value !== 0) {
    throw new Error("Stale branch count must be zero when no branches are eligible.");
  }

  const buildCount = metric("build_count").value;
  for (const code of ["build_duration_p95_seconds", "build_queue_p95_seconds"]) {
    const item = metric(code);
    assertUnavailableWhenZero(item, buildCount, code);
    if (buildCount > 0 && item.sample_count > buildCount) {
      throw new Error(`${code}.sample_count cannot exceed build count.`);
    }
  }

  const flakyCount = metric("flaky_test_count");
  const flakyAge = metric("flaky_test_age_seconds");
  if (flakyCount) {
    assertUnavailableWhenZero(flakyAge, flakyCount.value, "flaky_test_age_seconds");
    if (flakyCount.value > 0 && flakyAge.sample_count !== flakyCount.value) {
      throw new Error("flaky_test_age_seconds.sample_count must equal flaky test count.");
    }
  }

  const activeFlags = metric("active_feature_flag_count").value;
  for (const code of [
    "feature_flag_without_owner_count", "feature_flag_without_expiry_count",
    "permanent_feature_flag_count", "feature_flag_untested_on_count", "feature_flag_untested_off_count",
  ]) {
    if (metric(code).value > activeFlags) throw new Error(`${code} cannot exceed active feature flag count.`);
  }
  const flagAge = metric("feature_flag_age_p95_seconds");
  assertUnavailableWhenZero(flagAge, activeFlags, "feature_flag_age_p95_seconds");
  if (activeFlags > 0 && flagAge.sample_count !== activeFlags) {
    throw new Error("feature_flag_age_p95_seconds.sample_count must equal active feature flag count.");
  }

  const debtCount = metric("tech_debt_item_count").value;
  const debtAge = metric("tech_debt_age_average_seconds");
  assertUnavailableWhenZero(debtAge, debtCount, "tech_debt_age_average_seconds");
  if (debtCount > 0 && debtAge.sample_count !== debtCount) {
    throw new Error("tech_debt_age_average_seconds.sample_count must equal tech debt item count.");
  }

  if (!metrics.length) throw new Error("Engineering quality evidence has no available metrics to emit.");
  return {
    evidence: {
      environment,
      source_code: "engineering-quality",
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
  if (!inputArg) throw new Error("Usage: node saas_engineering_quality_rollup.mjs --input=<path>");
  const result = rollupEngineeringQualityEvidence(await readBoundedJson(inputArg.slice("--input=".length)));
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
