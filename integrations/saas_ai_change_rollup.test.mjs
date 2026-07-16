import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { aiChangeMetricCodes, rollupAiChangeEvidence } from "./saas_ai_change_rollup.mjs";
import { businessMetricCodes, validateBusinessEvidence } from "./saas_business_sync.mjs";

const dailyValues = new Map([
  ["ai_assisted_pr_count", { value: 2 }],
  ["ai_assisted_pr_average_diff_lines", { value: 300, sample_count: 2 }],
  ["ai_assisted_changed_file_count", { value: 8 }],
  ["ai_assisted_modules_touched_per_change", { value: 2.5, sample_count: 2 }],
  ["ai_assisted_security_sensitive_file_touch_count", { value: 1 }],
  ["ai_assisted_billing_file_touch_count", { value: 0 }],
  ["ai_assisted_auth_file_touch_count", { value: 1 }],
  ["ai_assisted_db_migration_count", { value: 0 }],
  ["ai_assisted_dependency_proposal_count", { value: 1 }],
  ["ai_assisted_dependency_rejection_count", { value: 0 }],
  ["ai_assisted_tests_added_count", { value: 5 }],
  ["ai_assisted_tests_modified_count", { value: 1 }],
  ["ai_assisted_tests_deleted_count", { value: 0 }],
]);

const reviewValues = new Map([
  ["ai_change_human_review_coverage", { value: 100, numerator: 2, denominator: 2 }],
  ["ai_change_security_review_coverage", { value: 100, numerator: 1, denominator: 1 }],
]);

const riskValues = new Map([
  ["ai_change_review_risk_p95_score", { value: 40, sample_count: 2 }],
  ["ai_change_low_risk_count", { value: 1 }],
  ["ai_change_medium_risk_count", { value: 1 }],
  ["ai_change_high_risk_count", { value: 0 }],
  ["ai_change_critical_review_required_count", { value: 0 }],
]);

const monthlyValues = new Map([
  ["ai_assisted_release_incident_rate", { value: 25, numerator: 1, denominator: 4 }],
  ["ai_assisted_release_rollback_rate", { value: 0, numerator: 0, denominator: 4 }],
  ["ai_assisted_release_hotfix_rate", { value: 25, numerator: 1, denominator: 4 }],
  ["ai_assisted_escaped_defect_count", { value: 1 }],
  ["ai_assisted_authorization_defect_count", { value: 0 }],
  ["ai_assisted_performance_regression_count", { value: 1 }],
  ["ai_assisted_query_regression_count", { value: 0 }],
  ["ai_assisted_cost_regression_count", { value: 0 }],
  ["ai_assisted_change_reopen_rate", { value: 25, numerator: 1, denominator: 4 }],
  ["ai_assisted_change_repair_p50_seconds", { value: 3600, sample_count: 1 }],
]);

function source(contract, values, overrides = {}) {
  return {
    contract,
    complete: true,
    source_updated_at: "2026-07-16T00:05:00Z",
    drilldown_url: "https://github.example.test/aggregate",
    metrics: [...values].map(([code, fields]) => ({ code, ...fields })),
    ...overrides,
  };
}

function dailyInput(overrides = {}) {
  return {
    environment: "develop",
    compiled_at: "2026-07-16T00:10:00Z",
    period_start: "2026-07-15T00:00:00Z",
    period_end: "2026-07-16T00:00:00Z",
    granularity: "day",
    sources: [
      source("change_inventory", dailyValues),
      source("review_gates", reviewValues),
      source("risk_assessment", riskValues),
    ],
    ...overrides,
  };
}

function monthlyInput(overrides = {}) {
  return {
    environment: "main",
    compiled_at: "2026-08-01T00:10:00Z",
    period_start: "2026-07-01T00:00:00Z",
    period_end: "2026-08-01T00:00:00Z",
    granularity: "month",
    sources: [source("release_outcomes", monthlyValues, { source_updated_at: "2026-08-01T00:05:00Z" })],
    ...overrides,
  };
}

test("all thirty AI change metrics are seeded and accepted by the Odoo business bridge", async () => {
  const csv = await readFile(
    new URL("../addons/arcigy_saas_control_center/data/saas.metric.definition.csv", import.meta.url),
    "utf8",
  );
  const business = new Set(businessMetricCodes());
  assert.equal(aiChangeMetricCodes().length, 30);
  assert.deepEqual(aiChangeMetricCodes().filter((code) => !business.has(code)), []);
  assert.deepEqual(aiChangeMetricCodes().filter((code) => !csv.includes(`,${code},`)), []);
});

test("both synthetic example contracts remain executable and cover all thirty metrics", async () => {
  const daily = JSON.parse(await readFile(new URL("./saas_ai_change_daily.example.json", import.meta.url), "utf8"));
  const monthly = JSON.parse(await readFile(new URL("./saas_ai_change_monthly.example.json", import.meta.url), "utf8"));
  const emitted = [
    ...rollupAiChangeEvidence(daily, Date.parse(daily.compiled_at)).evidence.metrics,
    ...rollupAiChangeEvidence(monthly, Date.parse(monthly.compiled_at)).evidence.metrics,
  ].map(({ code }) => code).sort();
  assert.deepEqual(emitted, aiChangeMetricCodes());
});

test("compiles complete daily aggregate evidence without code paths prompts or identities", () => {
  const result = rollupAiChangeEvidence(dailyInput(), Date.parse("2026-07-16T00:10:00Z"));
  assert.deepEqual(result.stats, { sources: 3, emitted: 20, omitted: [] });
  assert.equal(result.evidence.source_code, "ai-change-control-plane");
  assert.ok(result.evidence.metrics.every(({ external_key }) => external_key.startsWith("develop:business:ai-change-control-plane:")));
  assert.equal(validateBusinessEvidence(result.evidence, Date.parse("2026-07-16T00:10:00Z")).metrics.length, 20);
  assert.doesNotMatch(JSON.stringify(result), /prompt|file_path|author|email|source_code_payload/);
});

test("compiles complete monthly release outcomes with exact ratio evidence", () => {
  const result = rollupAiChangeEvidence(monthlyInput(), Date.parse("2026-08-01T00:10:00Z"));
  assert.deepEqual(result.stats, { sources: 1, emitted: 10, omitted: [] });
  const incident = result.evidence.metrics.find(({ code }) => code === "ai_assisted_release_incident_rate");
  assert.equal(incident.value, 25);
  assert.equal(incident.sample_count, 4);
  assert.equal(validateBusinessEvidence(result.evidence, Date.parse("2026-08-01T00:10:00Z")).metrics.length, 10);
});

test("represents a missing denominator as unavailable instead of a false zero", () => {
  const input = monthlyInput();
  input.sources[0].metrics[8] = {
    code: "ai_assisted_change_reopen_rate",
    available: false,
    unavailable_reason: "no_eligible_sample",
  };
  const result = rollupAiChangeEvidence(input, Date.parse("2026-08-01T00:10:00Z"));
  assert.equal(result.stats.emitted, 9);
  assert.deepEqual(result.stats.omitted, ["ai_assisted_change_reopen_rate:no_eligible_sample"]);
  assert.equal(result.evidence.metrics.some(({ code }) => code === "ai_assisted_change_reopen_rate"), false);
});

test("requires explicit complete sources and every metric in each source contract", () => {
  const incomplete = dailyInput();
  incomplete.sources[0].complete = false;
  assert.throws(() => rollupAiChangeEvidence(incomplete), /complete must be explicitly true/);

  const missing = dailyInput();
  missing.sources[0].metrics.pop();
  assert.throws(() => rollupAiChangeEvidence(missing), /must contain exactly 13 contract metrics/);
});

test("rejects raw code prompts identities file paths and unknown contracts", () => {
  for (const [field, value] of [
    ["prompt", "rewrite authorization"],
    ["source_code_payload", "private code"],
    ["author_email", "person@example.test"],
    ["file_paths", ["src/auth.ts"]],
  ]) {
    const input = dailyInput();
    input.sources[0][field] = value;
    assert.throws(() => rollupAiChangeEvidence(input), new RegExp(`unsupported fields: ${field}`));
  }
  const unknown = dailyInput();
  unknown.sources[0].contract = "raw_git_export";
  assert.throws(() => rollupAiChangeEvidence(unknown), /contract is not approved/);
});

test("enforces metric ownership cadence uniqueness and closed UTC periods", () => {
  const wrongOwner = dailyInput();
  wrongOwner.sources[1].metrics[0].code = "ai_change_review_risk_p95_score";
  assert.throws(() => rollupAiChangeEvidence(wrongOwner), /does not belong to review_gates/);

  const duplicate = dailyInput();
  duplicate.sources.push({ ...duplicate.sources[1] });
  assert.throws(() => rollupAiChangeEvidence(duplicate), /contract is duplicated/);

  const wrongCadence = dailyInput();
  wrongCadence.sources = [source("release_outcomes", monthlyValues)];
  assert.throws(() => rollupAiChangeEvidence(wrongCadence), /requires month granularity/);

  const open = dailyInput({ compiled_at: "2026-07-15T23:59:59Z" });
  assert.throws(() => rollupAiChangeEvidence(open), /period is not closed/);
});

test("enforces exact percentages bounded risk scores and sample sizes", () => {
  const ratio = dailyInput();
  ratio.sources[1].metrics[0].value = 50;
  assert.throws(() => rollupAiChangeEvidence(ratio), /does not match numerator \/ denominator/);

  const score = dailyInput();
  score.sources[2].metrics[0].value = 101;
  assert.throws(() => rollupAiChangeEvidence(score), /score between 0 and 100/);

  const average = dailyInput();
  delete average.sources[0].metrics[1].sample_count;
  assert.throws(() => rollupAiChangeEvidence(average), /sample_count is required for average evidence/);
});

test("enforces cross-metric populations instead of accepting internally inconsistent aggregates", () => {
  const averagePopulation = dailyInput();
  averagePopulation.sources[0].metrics[1].sample_count = 1;
  assert.throws(() => rollupAiChangeEvidence(averagePopulation), /sample_count must equal AI-assisted PR count/);

  const rejectedDependency = dailyInput();
  rejectedDependency.sources[0].metrics[9].value = 2;
  assert.throws(() => rollupAiChangeEvidence(rejectedDependency), /cannot exceed proposed dependencies/);

  const riskPopulation = dailyInput();
  riskPopulation.sources[2].metrics[1].value = 0;
  assert.throws(() => rollupAiChangeEvidence(riskPopulation), /classification counts must equal AI-assisted PR count/);

  const releasePopulation = monthlyInput();
  releasePopulation.sources[0].metrics[1].denominator = 5;
  releasePopulation.sources[0].metrics[1].value = 0;
  assert.throws(
    () => rollupAiChangeEvidence(releasePopulation, Date.parse("2026-08-01T00:10:00Z")),
    /share one release denominator/,
  );
});

test("rejects insecure or credential-bearing drilldowns and environment ambiguity", () => {
  const insecure = dailyInput();
  insecure.sources[0].drilldown_url = "http://github.example.test/aggregate";
  assert.throws(() => rollupAiChangeEvidence(insecure), /must use HTTPS/);

  const secret = dailyInput();
  secret.sources[0].drilldown_url = "https://github.example.test/aggregate?token=secret";
  assert.throws(() => rollupAiChangeEvidence(secret), /sensitive query parameter/);

  assert.throws(() => rollupAiChangeEvidence(dailyInput({ environment: "production" })), /develop or main/);
});
