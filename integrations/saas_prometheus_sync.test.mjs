import assert from "node:assert/strict";
import test from "node:test";
import { parsePrometheusResponse, runPrometheusSync, statusFor, validateConfig } from "./saas_prometheus_sync.mjs";

function config() {
  return {
    odoo: { url: "https://odoo.example.com", apiKeyEnv: "ARCIGY_ODOO_API_KEY" },
    sources: {
      develop: { prometheusUrl: "https://prom.example.com", scopeMode: "label_filtered", requiredQueryMarker: 'environment="develop"' },
      main: { prometheusUrl: "https://prom.example.com", scopeMode: "label_filtered", requiredQueryMarker: 'environment="main"' }
    },
    queries: [{ code: "queue_depth", promql: 'sum(queue_depth{environment="{{environment}}"})', direction: "lower", warning: 100, critical: 1000, freshnessSeconds: 300 }]
  };
}

test("requires explicit Develop and Main source isolation", () => {
  const raw = config();
  raw.queries[0].promql = "sum(queue_depth)";
  const validated = validateConfig(raw);
  assert.rejects(
    () => runPrometheusSync(validated, { dryRun: true, requestJson: async () => ({}) }),
    (error) => error instanceof AggregateError && error.errors.length === 2
  );
});

test("parses exactly one Prometheus sample and rejects ambiguous vectors", () => {
  const one = parsePrometheusResponse({ status: "success", data: { resultType: "vector", result: [{ value: [100, "42"] }] } }, "queue_depth");
  assert.equal(one.value, 42);
  assert.throws(() => parsePrometheusResponse({ status: "success", data: { resultType: "vector", result: [{ value: [100, "1"] }, { value: [100, "2"] }] } }, "queue_depth"));
});

test("calculates higher and lower threshold status", () => {
  assert.equal(statusFor(1000, { direction: "lower", warning: 100, critical: 1000 }), "critical");
  assert.equal(statusFor(80, { direction: "higher", warning: 99, critical: 95 }), "critical");
  assert.equal(statusFor(99.5, { direction: "higher", warning: 99, critical: 95 }), "healthy");
});

test("dry-run collects separate environment payloads and never writes Odoo", async () => {
  const calls = [];
  const result = await runPrometheusSync(config(), {
    dryRun: true,
    requestJson: async (url) => {
      calls.push(url);
      return { status: "success", data: { resultType: "vector", result: [{ value: [Math.floor(Date.now() / 1000), "12"] }] } };
    }
  });
  assert.deepEqual(result.map((item) => item.environment), ["develop", "main"]);
  assert.equal(result[0].metrics[0].external_key.startsWith("develop:"), true);
  assert.equal(result[1].metrics[0].external_key.startsWith("main:"), true);
  assert.equal(calls.length, 2);
  assert.match(decodeURIComponent(calls[0]), /environment="develop"/);
  assert.match(decodeURIComponent(calls[1]), /environment="main"/);
});
