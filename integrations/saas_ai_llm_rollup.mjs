import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 2 * 1024 * 1024;
const ENVIRONMENTS = new Set(["develop", "main"]);
const SAFE_CODE = /^[A-Za-z0-9._:-]{1,120}$/;
const SAFE_DIMENSION = /^[A-Za-z0-9._:-]{1,50}$/;
const UTC_TIMESTAMP = /(?:Z|\+00:00)$/i;
const ROOT_FIELDS = new Set([
  "environment", "compiled_at", "period_start", "period_end", "granularity", "sources",
]);
const SOURCE_FIELDS = new Set([
  "contract", "complete", "source_updated_at", "drilldown_url", "metrics",
  "populations", "breakdowns",
]);
const METRIC_FIELDS = new Set([
  "code", "available", "unavailable_reason", "value", "numerator", "denominator", "sample_count",
]);
const POPULATION_FIELDS = new Set(["request_count", "tenant_count", "successful_outcome_count"]);
const BREAKDOWN_FIELDS = new Set([
  "tenant_complete", "feature_complete", "model_complete", "items",
]);
const BREAKDOWN_ITEM_FIELDS = new Set([
  "code", "value", "tenant_external_id", "feature_code", "model_code",
]);
const BREAKDOWN_CODES = new Set([
  "ai_input_token_count", "ai_output_token_count", "ai_cached_token_count", "ai_cost",
]);
const BREAKDOWN_DIMENSIONS = ["tenant_external_id", "feature_code", "model_code"];

const METRIC_CONTRACTS = new Map([
  ["ai_request_count", { cadence: "hour", source: "gateway", type: "count" }],
  ["ai_successful_request_count", { cadence: "hour", source: "gateway", type: "count" }],
  ["ai_failed_request_count", { cadence: "hour", source: "gateway", type: "count" }],
  ["ai_request_success_rate", { cadence: "hour", source: "gateway", type: "ratio" }],

  ["ai_latency_p95_seconds", { cadence: "hour", source: "performance", type: "duration" }],
  ["ai_time_to_first_token_p95_seconds", { cadence: "hour", source: "performance", type: "duration" }],
  ["ai_model_processing_p95_seconds", { cadence: "hour", source: "performance", type: "duration" }],
  ["ai_tool_call_duration_p95_seconds", { cadence: "hour", source: "performance", type: "duration" }],
  ["ai_retry_rate", { cadence: "hour", source: "performance", type: "ratio" }],
  ["ai_fallback_model_use_rate", { cadence: "hour", source: "performance", type: "ratio" }],
  ["ai_provider_timeout_count", { cadence: "hour", source: "performance", type: "count" }],
  ["ai_provider_rate_limit_count", { cadence: "hour", source: "performance", type: "count" }],

  ["ai_tool_call_success_rate", { cadence: "hour", source: "tooling", type: "ratio" }],

  ["ai_moderation_block_count", { cadence: "hour", source: "safety", type: "count" }],
  ["ai_prompt_injection_detection_count", { cadence: "hour", source: "safety", type: "count" }],
  ["ai_jailbreak_attempt_count", { cadence: "hour", source: "safety", type: "count" }],
  ["ai_sensitive_data_detection_count", { cadence: "hour", source: "safety", type: "count" }],
  ["ai_output_policy_violation_count", { cadence: "hour", source: "safety", type: "count" }],
  ["ai_tool_permission_denial_count", { cadence: "hour", source: "safety", type: "count" }],
  ["ai_tenant_quota_exceeded_count", { cadence: "hour", source: "safety", type: "count" }],

  ["ai_input_token_count", { cadence: "day", source: "usage_cost", type: "count" }],
  ["ai_output_token_count", { cadence: "day", source: "usage_cost", type: "count" }],
  ["ai_cached_token_count", { cadence: "day", source: "usage_cost", type: "count" }],
  ["ai_cost", { cadence: "day", source: "usage_cost", type: "money" }],
  ["ai_cost_per_request", { cadence: "day", source: "usage_cost", type: "average" }],
  ["ai_cost_per_tenant", { cadence: "day", source: "usage_cost", type: "average" }],
  ["ai_cost_per_successful_outcome", { cadence: "day", source: "usage_cost", type: "average" }],

  ["ai_task_completion_rate", { cadence: "day", source: "quality", type: "ratio" }],
  ["ai_user_acceptance_rate", { cadence: "day", source: "quality", type: "ratio" }],
  ["ai_regenerate_rate", { cadence: "day", source: "quality", type: "ratio" }],
  ["ai_correction_rate", { cadence: "day", source: "quality", type: "ratio" }],
  ["ai_thumbs_up_count", { cadence: "day", source: "quality", type: "count" }],
  ["ai_thumbs_down_count", { cadence: "day", source: "quality", type: "count" }],
  ["ai_human_escalation_rate", { cadence: "day", source: "quality", type: "ratio" }],
  ["ai_structured_output_validation_failure_rate", { cadence: "day", source: "quality", type: "ratio" }],
  ["ai_citation_grounding_coverage_rate", { cadence: "day", source: "quality", type: "ratio" }],
  ["ai_detected_hallucination_rate", { cadence: "day", source: "quality", type: "ratio" }],
]);

const CADENCE_SOURCES = new Map();
for (const [code, contract] of METRIC_CONTRACTS) {
  const cadence = CADENCE_SOURCES.get(contract.cadence) || new Map();
  const codes = cadence.get(contract.source) || [];
  codes.push(code);
  cadence.set(contract.source, codes);
  CADENCE_SOURCES.set(contract.cadence, cadence);
}

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object.`);
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function safeCode(value, name, pattern = SAFE_CODE) {
  const normalized = String(value || "").trim();
  if (!pattern.test(normalized)) throw new Error(`${name} must be a safe identifier.`);
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
  const sensitive = [...url.searchParams.keys()].find((key) => /token|secret|password|authorization|api.?key|email/i.test(key));
  if (sensitive) throw new Error(`${name} contains a sensitive query parameter.`);
  url.hash = "";
  return url.toString();
}

function validatePeriod(start, end, granularity) {
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  const date = new Date(startMs);
  if (date.getUTCMinutes() || date.getUTCSeconds() || date.getUTCMilliseconds()) {
    throw new Error("AI/LLM period_start must be aligned to a UTC hour.");
  }
  if (granularity === "hour") {
    if (endMs - startMs !== 3_600_000) throw new Error("AI/LLM hourly period must cover exactly one UTC hour.");
    return;
  }
  if (granularity === "day") {
    if (date.getUTCHours() !== 0) throw new Error("AI/LLM daily period_start must be UTC midnight.");
    if (endMs - startMs !== 86_400_000) throw new Error("AI/LLM daily period must cover exactly one UTC day.");
    return;
  }
  throw new Error("AI/LLM granularity must be hour or day.");
}

function validateMetric(metric, contract, name) {
  if (metric.available === false) {
    if (["count", "money"].includes(contract.type)) throw new Error(`${name} must emit an explicit zero instead of unavailable.`);
    if (metric.unavailable_reason !== "no_eligible_sample") throw new Error(`${name}.unavailable_reason must be no_eligible_sample.`);
    if (["value", "numerator", "denominator", "sample_count"].some((field) => metric[field] !== undefined)) {
      throw new Error(`${name} unavailable metric must not include numeric fields.`);
    }
    return { unavailable: true };
  }
  if (metric.available !== undefined && metric.available !== true) throw new Error(`${name}.available must be true or false.`);
  if (metric.unavailable_reason !== undefined) throw new Error(`${name}.unavailable_reason is only valid when available is false.`);
  const value = finite(metric.value, `${name}.value`);
  const numerator = metric.numerator === undefined ? undefined : finite(metric.numerator, `${name}.numerator`, { integer: true });
  const denominator = metric.denominator === undefined ? undefined : finite(metric.denominator, `${name}.denominator`, { integer: true });
  const sampleCount = metric.sample_count === undefined ? undefined : finite(metric.sample_count, `${name}.sample_count`, { integer: true });
  if (["count", "money"].includes(contract.type)) {
    if (contract.type === "count" && !Number.isInteger(value)) throw new Error(`${name}.value must be an integer count.`);
    if (numerator !== undefined || denominator !== undefined || sampleCount !== undefined) throw new Error(`${name} must not include ratio or sample fields.`);
    return { value };
  }
  if (contract.type === "ratio") {
    if (numerator === undefined || denominator === undefined || denominator < 1 || numerator > denominator) {
      throw new Error(`${name} ratio requires consistent numerator and denominator counts.`);
    }
    if (value > 100 || Math.abs(value - numerator / denominator * 100) > 1e-6) {
      throw new Error(`${name}.value does not match numerator / denominator.`);
    }
    if (sampleCount !== undefined && sampleCount !== denominator) throw new Error(`${name}.sample_count must equal denominator.`);
    return { value, numerator, denominator, sample_count: denominator };
  }
  if (numerator !== undefined || denominator !== undefined) throw new Error(`${name} must not include numerator or denominator.`);
  if (sampleCount === undefined || sampleCount < 1) throw new Error(`${name}.sample_count is required.`);
  return { value, sample_count: sampleCount };
}

function assertAvailability(metric, population, code) {
  if (population === 0 && !metric.unavailable) throw new Error(`${code} must be unavailable when its eligible population is zero.`);
  if (population > 0 && metric.unavailable) throw new Error(`${code} cannot be unavailable when its eligible population is positive.`);
}

function closeEnough(left, right) {
  return Math.abs(left - right) <= 1e-6;
}

export function aiLlmMetricCodes() {
  return [...METRIC_CONTRACTS.keys()].sort();
}

export function rollupAiLlmEvidence(raw, now = Date.now()) {
  const input = plainObject(raw, "AI/LLM evidence");
  rejectUnknownKeys(input, ROOT_FIELDS, "AI/LLM evidence");
  const environment = String(input.environment || "").trim().toLowerCase();
  if (!ENVIRONMENTS.has(environment)) throw new Error("AI/LLM environment must be develop or main.");
  const compiledAt = utcTimestamp(input.compiled_at, "AI/LLM evidence.compiled_at");
  if (Date.parse(compiledAt) > now + 5 * 60_000) throw new Error("AI/LLM compiled_at is too far in the future.");
  const periodStart = utcTimestamp(input.period_start, "AI/LLM evidence.period_start");
  const periodEnd = utcTimestamp(input.period_end, "AI/LLM evidence.period_end");
  const granularity = String(input.granularity || "").trim();
  validatePeriod(periodStart, periodEnd, granularity);
  if (Date.parse(periodEnd) > Date.parse(compiledAt)) throw new Error("AI/LLM period is not closed at the compilation watermark.");
  const sourceContracts = CADENCE_SOURCES.get(granularity);
  if (!sourceContracts || !Array.isArray(input.sources) || input.sources.length !== sourceContracts.size) {
    throw new Error(`AI/LLM ${granularity} sources must contain exactly ${sourceContracts?.size || 0} contracts.`);
  }

  const seenSources = new Set();
  const seenMetrics = new Set();
  const validated = new Map();
  const metrics = [];
  const omitted = [];
  const periodKey = periodStart.replaceAll("-", "").replaceAll(":", "").slice(0, granularity === "hour" ? 11 : 8);
  let usageSource;

  const emit = (code, fields, sourceUpdatedAt, drilldownUrl, dimensions = {}) => {
    const scopeKey = dimensions.tenant_external_id
      ? `tenant:${dimensions.tenant_external_id}`
      : dimensions.feature_code
        ? `feature:${dimensions.feature_code}`
        : dimensions.model_code
          ? `model:${dimensions.model_code}`
          : "global";
    metrics.push({
      code,
      ...fields,
      status: "unknown",
      measured_at: sourceUpdatedAt,
      freshness_seconds: granularity === "hour" ? 3600 : 86400,
      scope_key: scopeKey,
      external_key: `${environment}:business:ai-llm-product:${code}:${scopeKey}:${periodKey}`,
      period_start: periodStart,
      period_end: periodEnd,
      granularity,
      ...(Object.keys(dimensions).length && ["ai_cost", "ai_cost_per_request", "ai_cost_per_tenant", "ai_cost_per_successful_outcome"].includes(code)
        ? { currency_code: "EUR" }
        : {}),
      ...(drilldownUrl ? { drilldown_url: drilldownUrl } : {}),
      ...dimensions,
    });
  };

  for (const [sourceIndex, rawSource] of input.sources.entries()) {
    const sourceName = `AI/LLM evidence.sources[${sourceIndex}]`;
    const source = plainObject(rawSource, sourceName);
    rejectUnknownKeys(source, SOURCE_FIELDS, sourceName);
    const contractName = safeCode(source.contract, `${sourceName}.contract`);
    const expectedCodes = sourceContracts.get(contractName);
    if (!expectedCodes) throw new Error(`${sourceName}.contract is not approved for ${granularity}.`);
    if (seenSources.has(contractName)) throw new Error(`${sourceName}.contract is duplicated.`);
    seenSources.add(contractName);
    if (source.complete !== true) throw new Error(`${sourceName}.complete must be explicitly true before zero values are trusted.`);
    const sourceUpdatedAt = utcTimestamp(source.source_updated_at, `${sourceName}.source_updated_at`);
    if (Date.parse(sourceUpdatedAt) < Date.parse(periodEnd)) throw new Error(`${sourceName} is older than the closed period.`);
    if (Date.parse(sourceUpdatedAt) > Date.parse(compiledAt) + 5 * 60_000) throw new Error(`${sourceName} is newer than the compilation watermark.`);
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
      if (!contract || contract.cadence !== granularity || contract.source !== contractName) {
        throw new Error(`${metricName}.code does not belong to ${granularity}/${contractName}.`);
      }
      if (sourceCodes.has(code) || seenMetrics.has(code)) throw new Error(`${metricName}.code is duplicated.`);
      sourceCodes.add(code);
      seenMetrics.add(code);
      const value = validateMetric(metric, contract, metricName);
      validated.set(code, value);
      if (value.unavailable) omitted.push(`${code}:no_eligible_sample`);
      else emit(code, value, sourceUpdatedAt, drilldownUrl);
    }
    const missing = expectedCodes.filter((code) => !sourceCodes.has(code));
    if (missing.length) throw new Error(`${sourceName}.metrics is missing: ${missing.join(", ")}.`);
    if (contractName === "usage_cost") usageSource = { source, sourceName, sourceUpdatedAt, drilldownUrl };
    else if (source.populations !== undefined || source.breakdowns !== undefined) {
      throw new Error(`${sourceName} does not own populations or breakdowns.`);
    }
  }

  const metric = (code) => validated.get(code);
  if (granularity === "hour") {
    const requestCount = metric("ai_request_count").value;
    if (metric("ai_successful_request_count").value + metric("ai_failed_request_count").value !== requestCount) {
      throw new Error("AI successful and failed request counts must equal request count.");
    }
    const successRate = metric("ai_request_success_rate");
    assertAvailability(successRate, requestCount, "ai_request_success_rate");
    if (requestCount > 0 && (successRate.numerator !== metric("ai_successful_request_count").value || successRate.denominator !== requestCount)) {
      throw new Error("AI request success rate must use the exact request population.");
    }
    for (const code of ["ai_latency_p95_seconds", "ai_model_processing_p95_seconds"]) {
      const item = metric(code);
      assertAvailability(item, requestCount, code);
      if (requestCount > 0 && item.sample_count > requestCount) throw new Error(`${code}.sample_count cannot exceed request count.`);
    }
    for (const code of ["ai_time_to_first_token_p95_seconds", "ai_tool_call_duration_p95_seconds"]) {
      const item = metric(code);
      if (!item.unavailable && item.sample_count > requestCount) throw new Error(`${code}.sample_count cannot exceed request count.`);
    }
    for (const code of ["ai_retry_rate", "ai_fallback_model_use_rate"]) {
      const item = metric(code);
      assertAvailability(item, requestCount, code);
      if (requestCount > 0 && item.denominator !== requestCount) throw new Error(`${code} must use the exact request population.`);
    }
  } else {
    const populations = plainObject(usageSource?.source.populations, `${usageSource?.sourceName}.populations`);
    rejectUnknownKeys(populations, POPULATION_FIELDS, `${usageSource.sourceName}.populations`);
    for (const field of POPULATION_FIELDS) finite(populations[field], `${usageSource.sourceName}.populations.${field}`, { integer: true });
    if (populations.successful_outcome_count > populations.request_count) {
      throw new Error("AI successful outcome count cannot exceed daily request count.");
    }
    const cost = metric("ai_cost").value;
    const costPopulations = new Map([
      ["ai_cost_per_request", populations.request_count],
      ["ai_cost_per_tenant", populations.tenant_count],
      ["ai_cost_per_successful_outcome", populations.successful_outcome_count],
    ]);
    for (const [code, population] of costPopulations) {
      const item = metric(code);
      assertAvailability(item, population, code);
      if (population > 0 && (item.sample_count !== population || !closeEnough(item.value, cost / population))) {
        throw new Error(`${code} must equal total AI cost divided by its exact population.`);
      }
    }
    if (populations.request_count === 0) {
      for (const code of ["ai_input_token_count", "ai_output_token_count", "ai_cached_token_count", "ai_cost"]) {
        if (metric(code).value !== 0) throw new Error(`${code} must be zero when daily request count is zero.`);
      }
    }
    for (const code of [
      "ai_task_completion_rate", "ai_user_acceptance_rate", "ai_regenerate_rate",
      "ai_correction_rate", "ai_human_escalation_rate",
      "ai_structured_output_validation_failure_rate", "ai_citation_grounding_coverage_rate",
      "ai_detected_hallucination_rate",
    ]) {
      const item = metric(code);
      if (!item.unavailable && item.denominator > populations.request_count) {
        throw new Error(`${code}.denominator cannot exceed daily request count.`);
      }
    }
    if (metric("ai_thumbs_up_count").value + metric("ai_thumbs_down_count").value > populations.request_count) {
      throw new Error("AI thumbs feedback cannot exceed daily request count.");
    }

    const breakdowns = plainObject(usageSource.source.breakdowns, `${usageSource.sourceName}.breakdowns`);
    rejectUnknownKeys(breakdowns, BREAKDOWN_FIELDS, `${usageSource.sourceName}.breakdowns`);
    for (const flag of ["tenant_complete", "feature_complete", "model_complete"]) {
      if (breakdowns[flag] !== true) throw new Error(`${usageSource.sourceName}.breakdowns.${flag} must be explicitly true.`);
    }
    if (!Array.isArray(breakdowns.items) || breakdowns.items.length > 180) {
      throw new Error(`${usageSource.sourceName}.breakdowns.items must contain at most 180 records.`);
    }
    const grouped = new Map(BREAKDOWN_DIMENSIONS.map((dimension) => [dimension, new Map()]));
    const totals = new Map(BREAKDOWN_DIMENSIONS.map((dimension) => [dimension, new Map([...BREAKDOWN_CODES].map((code) => [code, 0]))]));
    const duplicates = new Set();
    for (const [index, rawItem] of breakdowns.items.entries()) {
      const name = `${usageSource.sourceName}.breakdowns.items[${index}]`;
      const item = plainObject(rawItem, name);
      rejectUnknownKeys(item, BREAKDOWN_ITEM_FIELDS, name);
      const code = safeCode(item.code, `${name}.code`);
      if (!BREAKDOWN_CODES.has(code)) throw new Error(`${name}.code is not an approved AI usage breakdown metric.`);
      const dimensions = BREAKDOWN_DIMENSIONS.filter((field) => item[field] !== undefined && item[field] !== null && item[field] !== "");
      if (dimensions.length !== 1) throw new Error(`${name} must contain exactly one tenant feature or model dimension.`);
      const dimension = dimensions[0];
      const dimensionValue = safeCode(item[dimension], `${name}.${dimension}`, SAFE_DIMENSION);
      const key = `${dimension}:${dimensionValue}:${code}`;
      if (duplicates.has(key)) throw new Error(`${name} duplicates ${key}.`);
      duplicates.add(key);
      const value = finite(item.value, `${name}.value`, { integer: code !== "ai_cost" });
      const entity = grouped.get(dimension).get(dimensionValue) || new Set();
      entity.add(code);
      grouped.get(dimension).set(dimensionValue, entity);
      totals.get(dimension).set(code, totals.get(dimension).get(code) + value);
      emit(code, { value }, usageSource.sourceUpdatedAt, usageSource.drilldownUrl, { [dimension]: dimensionValue });
    }
    for (const dimension of BREAKDOWN_DIMENSIONS) {
      if (populations.request_count > 0 && grouped.get(dimension).size === 0) {
        throw new Error(`${dimension} breakdown must not be empty when daily requests exist.`);
      }
      for (const [dimensionValue, codes] of grouped.get(dimension)) {
        const missing = [...BREAKDOWN_CODES].filter((code) => !codes.has(code));
        if (missing.length) throw new Error(`${dimension}:${dimensionValue} breakdown is missing: ${missing.join(", ")}.`);
      }
      for (const code of BREAKDOWN_CODES) {
        if (!closeEnough(totals.get(dimension).get(code), metric(code).value)) {
          throw new Error(`${dimension} ${code} breakdown total must equal the global value.`);
        }
      }
    }
  }

  const expectedMetricCount = [...sourceContracts.values()].reduce((total, codes) => total + codes.length, 0);
  if (seenSources.size !== sourceContracts.size || seenMetrics.size !== expectedMetricCount) {
    throw new Error("AI/LLM evidence must include every approved source and metric contract.");
  }
  if (!metrics.length) throw new Error("AI/LLM evidence has no available metrics to emit.");
  return {
    evidence: {
      environment,
      source_code: "ai-llm-product",
      source_updated_at: compiledAt,
      metrics,
    },
    stats: { sources: seenSources.size, emitted: metrics.length, omitted },
  };
}

async function readBoundedJson(path) {
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_INPUT_BYTES) throw new Error(`input must be a JSON file no larger than ${MAX_INPUT_BYTES} bytes.`);
  return JSON.parse(await readFile(path, "utf8"));
}

async function main() {
  const inputArg = process.argv.find((arg) => arg.startsWith("--input="));
  if (!inputArg) throw new Error("Usage: node saas_ai_llm_rollup.mjs --input=<path>");
  const result = rollupAiLlmEvidence(await readBoundedJson(inputArg.slice("--input=".length)));
  console.error(JSON.stringify(result.stats));
  console.log(JSON.stringify(result.evidence, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
