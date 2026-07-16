import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { businessMetricCodes, validateBusinessEvidence } from "./saas_business_sync.mjs";
import {
  engineeringQualityMetricCodes,
  rollupEngineeringQualityEvidence,
} from "./saas_engineering_quality_rollup.mjs";

const NOW = Date.parse("2026-07-16T00:10:00Z");

async function example() {
  return JSON.parse(
    await readFile(new URL("./saas_engineering_quality.example.json", import.meta.url), "utf8"),
  );
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

function unavailable(code) {
  return { code, available: false, unavailable_reason: "no_eligible_sample" };
}

test("all 57 Engineering metrics are seeded and accepted by the Odoo business bridge", async () => {
  const csv = await readFile(
    new URL("../addons/arcigy_saas_control_center/data/saas.metric.definition.csv", import.meta.url),
    "utf8",
  );
  const codes = engineeringQualityMetricCodes();
  const business = new Set(businessMetricCodes());
  assert.equal(codes.length, 57);
  assert.deepEqual(codes.filter((code) => !business.has(code)), []);
  assert.deepEqual(codes.filter((code) => !csv.includes(`,${code},`)), []);
});

test("compiles the six complete Engineering sources without raw identities or source artifacts", async () => {
  const input = await example();
  const result = rollupEngineeringQualityEvidence(input, NOW);
  assert.deepEqual(result.stats, { sources: 6, emitted: 57, omitted: [] });
  assert.equal(result.evidence.source_code, "engineering-quality");
  assert.deepEqual(
    result.evidence.metrics.map(({ code }) => code).sort(),
    engineeringQualityMetricCodes(),
  );
  assert.equal(validateBusinessEvidence(result.evidence, NOW).metrics.length, 57);
  assert.ok(result.evidence.metrics.every(({ external_key }) => external_key.startsWith("develop:business:engineering-quality:")));
  assert.doesNotMatch(JSON.stringify(result), /author|email|raw_logs|file_paths|credentials|api_key/i);
});

test("omits sample metrics instead of inventing zeros for empty populations", async () => {
  const input = await example();
  for (const code of [
    "pr_cycle_time_p50_seconds", "pr_time_to_first_review_p50_seconds",
    "pr_approval_to_merge_p50_seconds", "pr_average_diff_lines",
    "pr_average_files_changed", "pr_average_modules_changed", "pr_average_comment_count",
    "branch_age_average_seconds", "branch_oldest_age_seconds",
    "build_duration_p95_seconds", "build_queue_p95_seconds", "flaky_test_age_seconds",
    "feature_flag_age_p95_seconds", "tech_debt_age_average_seconds",
  ]) {
    const current = metric(input, code);
    Object.assign(current, unavailable(code));
    for (const field of ["value", "numerator", "denominator", "sample_count"]) delete current[field];
  }
  for (const code of [
    "open_pr_count", "stale_pr_count", "reopened_pr_count", "stale_branch_count",
    "build_count", "flaky_test_count", "active_feature_flag_count",
    "feature_flag_without_owner_count", "feature_flag_without_expiry_count",
    "permanent_feature_flag_count", "feature_flag_untested_on_count",
    "feature_flag_untested_off_count", "tech_debt_item_count",
  ]) metric(input, code).value = 0;

  const result = rollupEngineeringQualityEvidence(input, NOW);
  assert.equal(result.stats.emitted, 43);
  assert.equal(result.stats.omitted.length, 14);
  assert.equal(validateBusinessEvidence(result.evidence, NOW).metrics.length, 43);
});

test("requires exact complete source and metric contracts before accepting zero values", async () => {
  const incomplete = await example();
  source(incomplete, "ci").complete = false;
  assert.throws(() => rollupEngineeringQualityEvidence(incomplete, NOW), /complete must be explicitly true/);

  const missingSource = await example();
  missingSource.sources.pop();
  assert.throws(() => rollupEngineeringQualityEvidence(missingSource, NOW), /exactly 6 contracts/);

  const duplicateMetric = await example();
  source(duplicateMetric, "ci").metrics[1].code = "build_count";
  assert.throws(() => rollupEngineeringQualityEvidence(duplicateMetric, NOW), /duplicated/);

  const unavailableCount = await example();
  Object.assign(metric(unavailableCount, "build_count"), unavailable("build_count"));
  delete metric(unavailableCount, "build_count").value;
  assert.throws(() => rollupEngineeringQualityEvidence(unavailableCount, NOW), /count must be emitted as zero/);
});

test("rejects raw engineering artifacts and insecure drilldowns", async () => {
  const raw = await example();
  source(raw, "pull_requests").raw_logs = ["secret"];
  assert.throws(() => rollupEngineeringQualityEvidence(raw, NOW), /unsupported fields: raw_logs/);

  const identity = await example();
  metric(identity, "open_pr_count").author = "person@example.test";
  assert.throws(() => rollupEngineeringQualityEvidence(identity, NOW), /unsupported fields: author/);

  const credentialUrl = await example();
  source(credentialUrl, "ci").drilldown_url = "https://user:secret@ci.example.test/builds";
  assert.throws(() => rollupEngineeringQualityEvidence(credentialUrl, NOW), /must not contain credentials/);

  const tokenUrl = await example();
  source(tokenUrl, "ci").drilldown_url = "https://ci.example.test/builds?api_key=secret";
  assert.throws(() => rollupEngineeringQualityEvidence(tokenUrl, NOW), /sensitive query parameter/);
});

test("requires exact ratios and consistent sample evidence", async () => {
  const ratio = await example();
  metric(ratio, "unit_test_pass_rate").value = 99;
  assert.throws(() => rollupEngineeringQualityEvidence(ratio, NOW), /does not match numerator/);

  const average = await example();
  metric(average, "pr_average_diff_lines").sample_count = 9;
  assert.throws(() => rollupEngineeringQualityEvidence(average, NOW), /must equal the completed PR population/);

  const flaky = await example();
  metric(flaky, "flaky_test_age_seconds").sample_count = 3;
  assert.throws(() => rollupEngineeringQualityEvidence(flaky, NOW), /must equal flaky test count/);
});

test("rejects impossible cross-metric Engineering populations", async () => {
  const stalePr = await example();
  metric(stalePr, "stale_pr_count").value = 5;
  assert.throws(() => rollupEngineeringQualityEvidence(stalePr, NOW), /cannot exceed open PR count/);

  const branchAge = await example();
  metric(branchAge, "branch_oldest_age_seconds").value = 100;
  assert.throws(() => rollupEngineeringQualityEvidence(branchAge, NOW), /cannot be lower than average/);

  const flags = await example();
  metric(flags, "feature_flag_without_owner_count").value = 99;
  assert.throws(() => rollupEngineeringQualityEvidence(flags, NOW), /cannot exceed active feature flag count/);

  const debt = await example();
  metric(debt, "tech_debt_age_average_seconds").sample_count = 9;
  assert.throws(() => rollupEngineeringQualityEvidence(debt, NOW), /must equal tech debt item count/);
});

test("rejects cross-environment and non-closed or stale daily evidence", async () => {
  const environment = await example();
  environment.environment = "production";
  assert.throws(() => rollupEngineeringQualityEvidence(environment, NOW), /develop or main/);

  const openPeriod = await example();
  openPeriod.compiled_at = "2026-07-15T23:00:00Z";
  assert.throws(() => rollupEngineeringQualityEvidence(openPeriod, NOW), /period is not closed/);

  const unaligned = await example();
  unaligned.period_start = "2026-07-15T01:00:00Z";
  assert.throws(() => rollupEngineeringQualityEvidence(unaligned, NOW), /aligned to UTC midnight/);

  const stale = await example();
  source(stale, "tests").source_updated_at = "2026-07-15T23:59:59Z";
  assert.throws(() => rollupEngineeringQualityEvidence(stale, NOW), /older than the closed period/);

  const future = await example();
  future.compiled_at = "2026-07-16T00:16:00Z";
  assert.throws(() => rollupEngineeringQualityEvidence(future, NOW), /too far in the future/);
});
