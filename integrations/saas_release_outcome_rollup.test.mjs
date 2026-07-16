import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { businessMetricCodes, validateBusinessEvidence } from "./saas_business_sync.mjs";
import { releaseOutcomeMetricCodes, rollupReleaseOutcomeEvidence } from "./saas_release_outcome_rollup.mjs";

async function example() {
  return JSON.parse(await readFile(new URL("./saas_release_outcome.example.json", import.meta.url), "utf8"));
}

test("all sixteen release outcome metrics are seeded and accepted by the Odoo bridge", async () => {
  const csv = await readFile(
    new URL("../addons/arcigy_saas_control_center/data/saas.metric.definition.csv", import.meta.url),
    "utf8",
  );
  const business = new Set(businessMetricCodes());
  assert.equal(releaseOutcomeMetricCodes().length, 16);
  assert.deepEqual(releaseOutcomeMetricCodes().filter((code) => !business.has(code)), []);
  assert.deepEqual(releaseOutcomeMetricCodes().filter((code) => !csv.includes(`,${code},`)), []);
});

test("compiles the complete release registry into truthful DORA and rollback evidence", async () => {
  const input = await example();
  const result = rollupReleaseOutcomeEvidence(input, Date.parse(input.compiled_at));
  assert.deepEqual(result.stats, { emitted: 16, omitted: [] });
  assert.equal(result.evidence.source_code, "release-control-plane");
  assert.ok(result.evidence.metrics.every(({ external_key }) => external_key.startsWith("develop:business:release-control-plane:")));
  assert.equal(validateBusinessEvidence(result.evidence, Date.parse(input.compiled_at)).metrics.length, 16);
});

test("requires a complete closed-day source before zero counts are trusted", async () => {
  const incomplete = await example();
  incomplete.complete = false;
  assert.throws(() => rollupReleaseOutcomeEvidence(incomplete), /complete must be explicitly true/);

  const open = await example();
  open.compiled_at = "2026-07-15T23:59:59Z";
  assert.throws(() => rollupReleaseOutcomeEvidence(open), /period is not closed/);

  const wrongCadence = await example();
  wrongCadence.period_start = "2026-07-15T01:00:00Z";
  assert.throws(() => rollupReleaseOutcomeEvidence(wrongCadence), /exactly one UTC day/);
});

test("uses unavailable for undefined rates and durations when no eligible population exists", async () => {
  const input = await example();
  const unavailable = (code) => ({ code, available: false, unavailable_reason: "no_eligible_sample" });
  for (const metric of input.metrics) {
    if ([
      "deployment_count", "deployment_change_failure_count", "release_rollback_count",
      "rollback_attempt_count", "hotfix_count", "release_incident_count",
      "canary_failure_count", "artifact_mismatch_count", "environment_drift_count",
    ].includes(metric.code)) {
      metric.value = 0;
    }
  }
  for (const code of [
    "deployment_success_rate", "deployment_duration_p95_seconds", "deployment_queue_p95_seconds",
    "change_failure_rate", "release_rollback_rate", "rollback_success_rate",
    "time_to_restore_service_seconds",
  ]) {
    input.metrics[input.metrics.findIndex((metric) => metric.code === code)] = unavailable(code);
  }
  const result = rollupReleaseOutcomeEvidence(input, Date.parse(input.compiled_at));
  assert.equal(result.stats.emitted, 9);
  assert.equal(result.stats.omitted.length, 7);
  assert.equal(validateBusinessEvidence(result.evidence, Date.parse(input.compiled_at)).metrics.length, 9);
});

test("rejects raw workflow logs commits actors and deployment identities", async () => {
  for (const [field, value] of [
    ["workflow_logs", ["private output"]],
    ["commit_sha", "deadbeef"],
    ["actor_email", "person@example.test"],
    ["deployment_ids", [123]],
  ]) {
    const input = await example();
    input[field] = value;
    assert.throws(() => rollupReleaseOutcomeEvidence(input), new RegExp(`unsupported fields: ${field}`));
  }
});

test("rejects duplicate unknown or incomplete metric contracts", async () => {
  const duplicate = await example();
  duplicate.metrics[1].code = "deployment_count";
  assert.throws(() => rollupReleaseOutcomeEvidence(duplicate), /duplicated/);

  const unknown = await example();
  unknown.metrics[0].code = "failed_workflow_guess";
  assert.throws(() => rollupReleaseOutcomeEvidence(unknown), /not an approved release outcome metric/);

  const short = await example();
  short.metrics.pop();
  assert.throws(() => rollupReleaseOutcomeEvidence(short), /exactly 16 contract metrics/);
});

test("enforces one deployment population for success change failure and rollback", async () => {
  const input = await example();
  input.metrics.find(({ code }) => code === "change_failure_rate").denominator = 5;
  input.metrics.find(({ code }) => code === "change_failure_rate").value = 20;
  assert.throws(
    () => rollupReleaseOutcomeEvidence(input, Date.parse(input.compiled_at)),
    /denominator must equal deployment_count/,
  );

  const numerator = await example();
  numerator.metrics.find(({ code }) => code === "change_failure_rate").numerator = 2;
  numerator.metrics.find(({ code }) => code === "change_failure_rate").value = 50;
  assert.throws(
    () => rollupReleaseOutcomeEvidence(numerator, Date.parse(numerator.compiled_at)),
    /numerator must equal deployment_change_failure_count/,
  );
});

test("enforces rollback attempts and incident recovery sample populations", async () => {
  const rollback = await example();
  rollback.metrics.find(({ code }) => code === "rollback_attempt_count").value = 2;
  assert.throws(
    () => rollupReleaseOutcomeEvidence(rollback, Date.parse(rollback.compiled_at)),
    /denominator must equal rollback_attempt_count/,
  );

  const restore = await example();
  restore.metrics.find(({ code }) => code === "time_to_restore_service_seconds").sample_count = 2;
  assert.throws(
    () => rollupReleaseOutcomeEvidence(restore, Date.parse(restore.compiled_at)),
    /sample_count cannot exceed release_incident_count/,
  );
});

test("rejects cross-environment ambiguity and insecure or credential-bearing links", async () => {
  const environment = await example();
  environment.environment = "production";
  assert.throws(() => rollupReleaseOutcomeEvidence(environment), /develop or main/);

  const insecure = await example();
  insecure.drilldown_url = "http://releases.example.test/aggregate";
  assert.throws(() => rollupReleaseOutcomeEvidence(insecure), /must use HTTPS/);

  const secret = await example();
  secret.drilldown_url = "https://releases.example.test/aggregate?token=secret";
  assert.throws(() => rollupReleaseOutcomeEvidence(secret), /sensitive query parameter/);
});
