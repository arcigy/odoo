import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { aiLlmMetricCodes, rollupAiLlmEvidence } from "./saas_ai_llm_rollup.mjs";
import { businessMetricCodes, validateBusinessEvidence } from "./saas_business_sync.mjs";

const HOUR_NOW = Date.parse("2026-07-16T21:10:00Z");
const DAY_NOW = Date.parse("2026-07-16T00:10:00Z");

async function fixture(cadence) {
  return JSON.parse(await readFile(
    new URL(`./saas_ai_llm_${cadence}.example.json`, import.meta.url),
    "utf8",
  ));
}

function source(input, contract) {
  return input.sources.find((item) => item.contract === contract);
}

function metric(input, code) {
  for (const item of input.sources) {
    const found = item.metrics.find((candidate) => candidate.code === code);
    if (found) return found;
  }
  throw new Error(`Missing fixture metric ${code}.`);
}

function makeUnavailable(item) {
  item.available = false;
  item.unavailable_reason = "no_eligible_sample";
  for (const field of ["value", "numerator", "denominator", "sample_count"]) delete item[field];
}

test("all 37 AI/LLM metrics are seeded and allowlisted by the Odoo bridge", async () => {
  const csv = await readFile(
    new URL("../addons/arcigy_saas_control_center/data/saas.metric.definition.csv", import.meta.url),
    "utf8",
  );
  const codes = aiLlmMetricCodes();
  const business = new Set(businessMetricCodes());
  assert.equal(codes.length, 37);
  assert.deepEqual(codes.filter((code) => !business.has(code)), []);
  assert.deepEqual(codes.filter((code) => !csv.includes(`,${code},`)), []);
});

test("compiles complete hourly and daily AI/LLM evidence including three reconciled dimensions", async () => {
  const hourly = rollupAiLlmEvidence(await fixture("hourly"), HOUR_NOW);
  const daily = rollupAiLlmEvidence(await fixture("daily"), DAY_NOW);
  assert.deepEqual(hourly.stats, { sources: 4, emitted: 20, omitted: [] });
  assert.deepEqual(daily.stats, { sources: 2, emitted: 29, omitted: [] });
  assert.equal(validateBusinessEvidence(hourly.evidence, HOUR_NOW).metrics.length, 20);
  assert.equal(validateBusinessEvidence(daily.evidence, DAY_NOW).metrics.length, 29);
  const emittedCodes = new Set([
    ...hourly.evidence.metrics.map(({ code }) => code),
    ...daily.evidence.metrics.map(({ code }) => code),
  ]);
  assert.deepEqual([...emittedCodes].sort(), aiLlmMetricCodes());
  assert.deepEqual(
    new Set(daily.evidence.metrics.filter(({ scope_key }) => scope_key !== "global").flatMap((item) =>
      ["tenant_external_id", "feature_code", "model_code"].filter((field) => item[field]),
    )),
    new Set(["tenant_external_id", "feature_code", "model_code"]),
  );
  assert.doesNotMatch(JSON.stringify({ hourly, daily }), /raw_prompt|prompt_content|response_content|email|user_identity|raw_log/i);
});

test("represents empty hourly samples as unavailable while preserving explicit count zeroes", async () => {
  const input = await fixture("hourly");
  for (const code of ["ai_request_count", "ai_successful_request_count", "ai_failed_request_count", "ai_provider_timeout_count", "ai_provider_rate_limit_count"]) {
    metric(input, code).value = 0;
  }
  for (const code of [
    "ai_request_success_rate", "ai_latency_p95_seconds", "ai_time_to_first_token_p95_seconds",
    "ai_model_processing_p95_seconds", "ai_tool_call_duration_p95_seconds", "ai_retry_rate",
    "ai_fallback_model_use_rate", "ai_tool_call_success_rate",
  ]) makeUnavailable(metric(input, code));
  for (const item of source(input, "safety").metrics) item.value = 0;
  const result = rollupAiLlmEvidence(input, HOUR_NOW);
  assert.equal(result.stats.emitted, 12);
  assert.equal(result.stats.omitted.length, 8);
  assert.equal(validateBusinessEvidence(result.evidence, HOUR_NOW).metrics.length, 12);
});

test("represents an empty daily population without invented rates costs or breakdown rows", async () => {
  const input = await fixture("daily");
  const usage = source(input, "usage_cost");
  usage.populations = { request_count: 0, tenant_count: 0, successful_outcome_count: 0 };
  for (const code of ["ai_input_token_count", "ai_output_token_count", "ai_cached_token_count", "ai_cost"]) metric(input, code).value = 0;
  for (const code of ["ai_cost_per_request", "ai_cost_per_tenant", "ai_cost_per_successful_outcome"]) makeUnavailable(metric(input, code));
  for (const item of source(input, "quality").metrics) {
    if (item.code.endsWith("_count")) item.value = 0;
    else makeUnavailable(item);
  }
  usage.breakdowns.items = [];
  const result = rollupAiLlmEvidence(input, DAY_NOW);
  assert.equal(result.stats.emitted, 6);
  assert.equal(result.stats.omitted.length, 11);
  assert.equal(validateBusinessEvidence(result.evidence, DAY_NOW).metrics.length, 6);
});

test("requires exact complete sources and metric ownership before trusting zeroes", async () => {
  const incomplete = await fixture("hourly");
  source(incomplete, "safety").complete = false;
  assert.throws(() => rollupAiLlmEvidence(incomplete, HOUR_NOW), /complete must be explicitly true/);

  const missingSource = await fixture("hourly");
  missingSource.sources.pop();
  assert.throws(() => rollupAiLlmEvidence(missingSource, HOUR_NOW), /exactly 4 contracts/);

  const wrongOwner = await fixture("hourly");
  source(wrongOwner, "gateway").metrics[0].code = "ai_provider_timeout_count";
  assert.throws(() => rollupAiLlmEvidence(wrongOwner, HOUR_NOW), /does not belong/);

  const unavailableCount = await fixture("hourly");
  makeUnavailable(metric(unavailableCount, "ai_failed_request_count"));
  assert.throws(() => rollupAiLlmEvidence(unavailableCount, HOUR_NOW), /explicit zero/);
});

test("rejects inconsistent request populations ratios and percentile samples", async () => {
  const counts = await fixture("hourly");
  metric(counts, "ai_failed_request_count").value = 2;
  assert.throws(() => rollupAiLlmEvidence(counts, HOUR_NOW), /must equal request count/);

  const ratio = await fixture("hourly");
  metric(ratio, "ai_request_success_rate").value = 80;
  assert.throws(() => rollupAiLlmEvidence(ratio, HOUR_NOW), /does not match numerator/);

  const retry = await fixture("hourly");
  metric(retry, "ai_retry_rate").denominator = 5;
  metric(retry, "ai_retry_rate").value = 40;
  assert.throws(() => rollupAiLlmEvidence(retry, HOUR_NOW), /exact request population/);

  const duration = await fixture("hourly");
  metric(duration, "ai_latency_p95_seconds").sample_count = 11;
  assert.throws(() => rollupAiLlmEvidence(duration, HOUR_NOW), /cannot exceed request count/);
});

test("requires exact cost populations and complete tenant feature and model allocations", async () => {
  const cost = await fixture("daily");
  metric(cost, "ai_cost_per_request").value = 1;
  assert.throws(() => rollupAiLlmEvidence(cost, DAY_NOW), /total AI cost divided/);

  const population = await fixture("daily");
  source(population, "usage_cost").populations.successful_outcome_count = 11;
  assert.throws(() => rollupAiLlmEvidence(population, DAY_NOW), /cannot exceed daily request count/);

  const incompleteBreakdown = await fixture("daily");
  source(incompleteBreakdown, "usage_cost").breakdowns.model_complete = false;
  assert.throws(() => rollupAiLlmEvidence(incompleteBreakdown, DAY_NOW), /model_complete must be explicitly true/);

  const missingMetric = await fixture("daily");
  source(missingMetric, "usage_cost").breakdowns.items.pop();
  assert.throws(() => rollupAiLlmEvidence(missingMetric, DAY_NOW), /breakdown is missing/);

  const wrongTotal = await fixture("daily");
  source(wrongTotal, "usage_cost").breakdowns.items[0].value = 999;
  assert.throws(() => rollupAiLlmEvidence(wrongTotal, DAY_NOW), /breakdown total must equal/);
});

test("rejects raw content identities unknown fields and credential-bearing URLs", async () => {
  const raw = await fixture("hourly");
  source(raw, "gateway").raw_prompts = ["forbidden"];
  assert.throws(() => rollupAiLlmEvidence(raw, HOUR_NOW), /unsupported fields: raw_prompts/);

  const identity = await fixture("daily");
  source(identity, "quality").metrics[0].email = "private@example.test";
  assert.throws(() => rollupAiLlmEvidence(identity, DAY_NOW), /unsupported fields: email/);

  const credentials = await fixture("hourly");
  source(credentials, "performance").drilldown_url = "https://user:secret@ai.example.test/performance";
  assert.throws(() => rollupAiLlmEvidence(credentials, HOUR_NOW), /must not contain credentials/);

  const unsafeDimension = await fixture("daily");
  source(unsafeDimension, "usage_cost").breakdowns.items[0].tenant_external_id = "person@example.test";
  assert.throws(() => rollupAiLlmEvidence(unsafeDimension, DAY_NOW), /safe identifier/);
});

test("rejects ambiguous environments open periods and misaligned cadence", async () => {
  const environment = await fixture("hourly");
  environment.environment = "production";
  assert.throws(() => rollupAiLlmEvidence(environment, HOUR_NOW), /develop or main/);

  const open = await fixture("hourly");
  open.compiled_at = "2026-07-16T20:59:00Z";
  assert.throws(() => rollupAiLlmEvidence(open, HOUR_NOW), /period is not closed/);

  const unaligned = await fixture("hourly");
  unaligned.period_start = "2026-07-16T20:01:00Z";
  assert.throws(() => rollupAiLlmEvidence(unaligned, HOUR_NOW), /aligned to a UTC hour/);

  const stale = await fixture("daily");
  source(stale, "quality").source_updated_at = "2026-07-15T23:59:59Z";
  assert.throws(() => rollupAiLlmEvidence(stale, DAY_NOW), /older than the closed period/);
});
