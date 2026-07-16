import assert from "node:assert/strict";
import test from "node:test";

import { businessMetricCodes, validateBusinessEvidence } from "./saas_business_sync.mjs";
import { privacyMetricCodes, rollupPrivacyEvidence } from "./saas_privacy_rollup.mjs";

function dailyInput(overrides = {}) {
  return {
    environment: "develop",
    compiled_at: "2026-07-16T00:10:00Z",
    period_start: "2026-07-15T00:00:00Z",
    period_end: "2026-07-16T00:00:00Z",
    granularity: "day",
    sources: [
      {
        contract: "privacy_workflow",
        complete: true,
        source_updated_at: "2026-07-16T00:05:00Z",
        drilldown_url: "https://privacy.example.test/dsr/aggregate",
        metrics: [
          { code: "open_data_subject_requests", value: 2 },
          { code: "overdue_data_subject_requests", value: 0 },
          { code: "dsr_completion_p95_seconds", value: 3600, sample_count: 12 },
        ],
      },
      {
        contract: "consent_registry",
        complete: true,
        source_updated_at: "2026-07-16T00:06:00Z",
        metrics: [
          { code: "tracking_consent_rate", value: 75, numerator: 75, denominator: 100 },
        ],
      },
    ],
    ...overrides,
  };
}

test("all privacy metrics are allowlisted by the Odoo business bridge", () => {
  const business = new Set(businessMetricCodes());
  assert.equal(privacyMetricCodes().length, 11);
  assert.deepEqual(privacyMetricCodes().filter((code) => !business.has(code)), []);
});

test("compiles complete aggregate privacy sources into server-compatible evidence", () => {
  const result = rollupPrivacyEvidence(dailyInput(), Date.parse("2026-07-16T00:10:00Z"));
  assert.deepEqual(result.stats, { sources: 2, metrics: 4, granularity: "day" });
  assert.equal(result.evidence.source_code, "privacy-control-plane");
  assert.equal(result.evidence.metrics[1].value, 0);
  assert.equal(result.evidence.metrics[3].numerator, 75);
  assert.equal(validateBusinessEvidence(result.evidence, Date.parse("2026-07-16T00:10:00Z")).metrics.length, 4);
});

test("compiles all eleven seeded privacy contracts across daily and monthly cadence", () => {
  const daily = dailyInput();
  daily.sources.unshift({
    contract: "data_inventory",
    complete: true,
    source_updated_at: "2026-07-16T00:04:00Z",
    metrics: [
      { code: "pii_field_count", value: 20 },
      { code: "unclassified_data_field_count", value: 0 },
    ],
  });
  daily.sources.push(
    {
      contract: "retention_jobs",
      complete: true,
      source_updated_at: "2026-07-16T00:07:00Z",
      metrics: [
        { code: "records_past_retention_limit", value: 0 },
        { code: "retention_job_failure_count", value: 0 },
      ],
    },
    {
      contract: "privacy_audit",
      complete: true,
      source_updated_at: "2026-07-16T00:08:00Z",
      metrics: [{ code: "tracking_without_valid_consent", value: 0 }],
    },
  );
  const dailyResult = rollupPrivacyEvidence(daily, Date.parse("2026-07-16T00:10:00Z"));

  const monthly = {
    environment: "develop",
    compiled_at: "2026-08-01T00:10:00Z",
    period_start: "2026-07-01T00:00:00Z",
    period_end: "2026-08-01T00:00:00Z",
    granularity: "month",
    sources: [{
      contract: "governance",
      complete: true,
      source_updated_at: "2026-08-01T00:05:00Z",
      metrics: [
        { code: "access_review_completion", value: 100, numerator: 4, denominator: 4 },
        { code: "subprocessor_review_compliance", value: 100, numerator: 3, denominator: 3 },
      ],
    }],
  };
  const monthlyResult = rollupPrivacyEvidence(monthly, Date.parse("2026-08-01T00:10:00Z"));
  const emitted = [...dailyResult.evidence.metrics, ...monthlyResult.evidence.metrics]
    .map(({ code }) => code)
    .sort();
  assert.deepEqual(emitted, privacyMetricCodes());
});

test("requires an explicitly complete source before emitting zero values", () => {
  const input = dailyInput();
  input.sources[0].complete = false;
  assert.throws(() => rollupPrivacyEvidence(input), /complete must be explicitly true/);
});

test("rejects raw records, identities and unknown fields", () => {
  const raw = dailyInput();
  raw.sources[0].metrics[0].requests = [{ email: "private@example.test" }];
  assert.throws(() => rollupPrivacyEvidence(raw), /unsupported fields: requests/);

  const identity = dailyInput();
  identity.sources[0].tenant_id = "tenant-1";
  assert.throws(() => rollupPrivacyEvidence(identity), /unsupported fields: tenant_id/);
});

test("enforces authoritative source ownership and unique contracts", () => {
  const wrongSource = dailyInput();
  wrongSource.sources[0].metrics[0].code = "tracking_without_valid_consent";
  assert.throws(() => rollupPrivacyEvidence(wrongSource), /does not belong to privacy_workflow/);

  const duplicateSource = dailyInput();
  duplicateSource.sources.push({ ...duplicateSource.sources[0] });
  assert.throws(() => rollupPrivacyEvidence(duplicateSource), /contract is duplicated/);
});

test("requires exact ratio evidence and percentile sample size", () => {
  const ratio = dailyInput();
  ratio.sources[1].metrics[0].value = 80;
  assert.throws(() => rollupPrivacyEvidence(ratio), /does not match numerator \/ denominator/);

  const percentile = dailyInput();
  delete percentile.sources[0].metrics[2].sample_count;
  assert.throws(() => rollupPrivacyEvidence(percentile), /sample_count is required/);
});

test("enforces closed UTC cadence and registry granularity", () => {
  const open = dailyInput({ compiled_at: "2026-07-15T23:59:59Z" });
  assert.throws(() => rollupPrivacyEvidence(open), /period is not closed/);

  const monthlyOnly = dailyInput();
  monthlyOnly.sources = [{
    contract: "governance",
    complete: true,
    source_updated_at: "2026-07-16T00:05:00Z",
    metrics: [{ code: "access_review_completion", value: 100, numerator: 4, denominator: 4 }],
  }];
  assert.throws(() => rollupPrivacyEvidence(monthlyOnly), /requires month granularity/);

  const monthly = {
    ...monthlyOnly,
    compiled_at: "2026-08-01T00:10:00Z",
    period_start: "2026-07-01T00:00:00Z",
    period_end: "2026-08-01T00:00:00Z",
    granularity: "month",
  };
  monthly.sources[0].source_updated_at = "2026-08-01T00:05:00Z";
  const result = rollupPrivacyEvidence(monthly, Date.parse("2026-08-01T00:10:00Z"));
  assert.equal(result.evidence.metrics[0].granularity, "month");
  assert.equal(validateBusinessEvidence(result.evidence, Date.parse("2026-08-01T00:10:00Z")).metrics.length, 1);
});

test("rejects credentials, insecure drilldowns and cross-environment ambiguity", () => {
  const secret = dailyInput();
  secret.sources[0].drilldown_url = "https://privacy.example.test/?token=secret";
  assert.throws(() => rollupPrivacyEvidence(secret), /sensitive query parameter/);

  assert.throws(() => rollupPrivacyEvidence(dailyInput({ environment: "production" })), /develop or main/);
});
