import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { businessMetricCodes, runBusinessSync, validateBusinessConfig, validateBusinessEvidence } from "./saas_business_sync.mjs";

const config = { odoo: { url: "https://odoo.example.test", database: "arcigy", apiKeyEnv: "ARCIGY_ODOO_API_KEY" } };

function evidence(overrides = {}) {
  return {
    environment: "develop",
    source_code: "billing-provider",
    source_updated_at: "2026-07-16T00:05:00Z",
    metrics: [
      {
        code: "mrr",
        value: 1000,
        status: "unknown",
        measured_at: "2026-07-16T00:00:00Z",
        sample_count: 10,
        scope_key: "global",
        external_key: "develop:business:billing-provider:mrr:20260715",
        period_start: "2026-07-15T00:00:00Z",
        period_end: "2026-07-16T00:00:00Z",
        granularity: "day",
        currency_code: undefined,
      },
    ],
    ...overrides,
  };
}

test("all allowlisted business metrics exist in the seeded Odoo metric registry", async () => {
  const csv = await readFile(new URL("../addons/arcigy_saas_control_center/data/saas.metric.definition.csv", import.meta.url), "utf8");
  const missing = businessMetricCodes().filter((code) => !csv.includes(`,${code},`));
  assert.deepEqual(missing, []);
  assert.equal(businessMetricCodes().length, 226);
});

test("validates privacy-safe daily and monthly business metric evidence", () => {
  const daily = validateBusinessEvidence(evidence());
  assert.equal(daily.metrics[0].code, "mrr");
  assert.equal(daily.metrics[0].granularity, "day");

  const monthlyInput = evidence({ source_updated_at: "2026-08-01T00:05:00Z" });
  monthlyInput.metrics[0].external_key = "develop:business:billing-provider:mrr:202607";
  monthlyInput.metrics[0].period_start = "2026-07-01T00:00:00Z";
  monthlyInput.metrics[0].period_end = "2026-08-01T00:00:00Z";
  monthlyInput.metrics[0].measured_at = "2026-08-01T00:00:00Z";
  monthlyInput.metrics[0].granularity = "month";
  assert.equal(
    validateBusinessEvidence(monthlyInput, Date.parse("2026-08-01T00:05:00Z")).metrics[0].granularity,
    "month",
  );
});

test("validates an exact closed UTC hour and bounded AI model dimension", () => {
  const hourly = evidence({ source_code: "ai-llm-product", source_updated_at: "2026-07-16T21:05:00Z" });
  hourly.metrics[0] = {
    ...hourly.metrics[0],
    code: "ai_request_count",
    value: 10,
    scope_key: "model:model-v1",
    model_code: "model-v1",
    external_key: "develop:business:ai-llm-product:ai_request_count:model:model-v1:20260716T20",
    period_start: "2026-07-16T20:00:00Z",
    period_end: "2026-07-16T21:00:00Z",
    measured_at: "2026-07-16T21:00:00Z",
    granularity: "hour",
  };
  assert.equal(validateBusinessEvidence(hourly, Date.parse("2026-07-16T21:05:00Z")).metrics[0].model_code, "model-v1");
});

test("rejects raw payloads, unknown metrics and cross-environment keys", () => {
  const raw = evidence();
  raw.metrics[0].raw_customers = [{ email: "private@example.test" }];
  assert.throws(() => validateBusinessEvidence(raw), /unsupported fields: raw_customers/);

  const unknown = evidence();
  unknown.metrics[0].code = "res.partner";
  assert.throws(() => validateBusinessEvidence(unknown), /not an approved business metric/);

  const crossEnvironment = evidence();
  crossEnvironment.metrics[0].external_key = "main:business:billing-provider:mrr:20260715";
  assert.throws(() => validateBusinessEvidence(crossEnvironment), /prefixed by develop:business:billing-provider:/);
});

test("requires non-global scopes for bounded dimensions", () => {
  const scoped = evidence();
  scoped.metrics[0].tenant_external_id = "tenant-opaque-1";
  assert.throws(() => validateBusinessEvidence(scoped), /scope_key must be non-global/);
  scoped.metrics[0].scope_key = "tenant:tenant-opaque-1";
  assert.equal(validateBusinessEvidence(scoped).metrics[0].tenant_external_id, "tenant-opaque-1");

  const noDimensions = evidence();
  noDimensions.metrics[0].scope_key = "plan:professional";
  assert.throws(() => validateBusinessEvidence(noDimensions), /scope_key must be global/);
});

test("rejects invalid ratios, periods, URLs and future evidence", () => {
  const ratio = evidence();
  ratio.metrics[0].numerator = 11;
  ratio.metrics[0].denominator = 10;
  assert.throws(() => validateBusinessEvidence(ratio), /numerator cannot exceed denominator/);

  const partialRatio = evidence();
  partialRatio.metrics[0].numerator = 5;
  assert.throws(() => validateBusinessEvidence(partialRatio), /must be supplied together/);

  const partialDay = evidence();
  partialDay.metrics[0].period_end = "2026-07-15T23:00:00Z";
  assert.throws(() => validateBusinessEvidence(partialDay), /exactly one UTC day/);

  const insecure = evidence();
  insecure.metrics[0].drilldown_url = "https://example.test/detail?api_key=secret";
  assert.throws(() => validateBusinessEvidence(insecure), /sensitive query parameter/);

  assert.throws(() => validateBusinessEvidence(evidence({ source_updated_at: "2999-01-01T00:00:00Z" })), /too far in the future/);
});

test("dry-run validates without reading secrets or writing Odoo", async () => {
  let requests = 0;
  const result = await runBusinessSync(config, evidence(), {
    env: {},
    dryRun: true,
    requestJson: async () => {
      requests += 1;
      throw new Error("dry-run must not make a request");
    },
  });
  assert.equal(result.dryRun, true);
  assert.equal(requests, 0);
});

test("live mode posts exactly one server-compatible metric batch", async () => {
  let captured;
  const result = await runBusinessSync(config, evidence(), {
    env: { ARCIGY_ODOO_API_KEY: "test-only-key" },
    requestJson: async (url, options) => {
      captured = { url, options };
      return { ok: true, created: 1, history_created: 1 };
    },
  });
  assert.equal(result.odoo.created, 1);
  assert.equal(captured.url, "https://odoo.example.test/json/2/saas.metric.current/ingest_metric_batch");
  assert.equal(captured.options.headers.Authorization, "Bearer test-only-key");
  const payload = JSON.parse(captured.options.body).payload;
  assert.equal(payload.environment, "develop");
  assert.equal(payload.sourceCode, undefined);
  assert.equal(payload.metrics[0].code, "mrr");
});

test("config requires secure Odoo transport and secret-store variable names", () => {
  assert.equal(validateBusinessConfig(config).odoo.database, "arcigy");
  assert.throws(() => validateBusinessConfig({ odoo: { url: "http://odoo.example.test" } }), /must use HTTPS/);
  assert.throws(() => validateBusinessConfig({ odoo: { url: "https://user:pass@odoo.example.test" } }), /must not contain credentials/);
  assert.throws(() => validateBusinessConfig({ odoo: { url: "https://odoo.example.test", apiKeyEnv: "API_KEY" } }), /must name an ARCIGY_/);
});
