import assert from "node:assert/strict";
import test from "node:test";

import { rollupReconciliation } from "./saas_reconciliation_rollup.mjs";
import { validateOperationalEvidence } from "./saas_operational_sync.mjs";

const allCodes = [
  "payment_provider_vs_odoo_invoices",
  "app_subscription_vs_billing_provider",
  "measured_usage_vs_invoiced_usage",
  "app_tenant_vs_odoo_partner",
  "active_seats_vs_paid_seats",
  "cloud_invoice_vs_cost_import",
  "observability_requests_vs_business_totals",
];

function input(checks) {
  return {
    environment: "develop",
    source_updated_at: "2026-07-16T00:05:00Z",
    period_start: "2026-07-15T00:00:00Z",
    period_end: "2026-07-16T00:00:00Z",
    checks,
  };
}

function check(overrides = {}) {
  return {
    code: "payment_provider_vs_odoo_invoices",
    authoritative_source: "payment-provider",
    comparison_source: "odoo-invoices",
    unit: "EUR",
    authoritative_value: 100,
    comparison_value: 100,
    tolerance_absolute: 0.01,
    tolerance_relative: 0.001,
    warning_multiplier: 2,
    drilldown_url: "https://analytics.example.test/reconciliation",
    ...overrides,
  };
}

test("emits operational evidence for every mandated reconciliation", () => {
  const checks = allCodes.map((code, index) => check({
    code,
    authoritative_source: `source-a-${index}`,
    comparison_source: `source-b-${index}`,
    unit: code.includes("seat") ? "seats" : "EUR",
    authoritative_value: code.includes("seat") ? 10 : 100,
    comparison_value: code.includes("seat") ? 10 : 100,
  }));
  const result = rollupReconciliation(input(checks));
  assert.deepEqual(result.stats, { checks: 7, statuses: { valid: 7, warning: 0, invalid: 0 } });
  assert.equal(result.evidence.items.length, 7);
  assert.equal(validateOperationalEvidence(result.evidence).items.length, 7);
  assert.equal(JSON.stringify(result.evidence).includes("authoritative_value"), false);
  assert.equal(JSON.stringify(result.evidence).includes("comparison_value"), false);
});

test("classifies valid, warning and invalid differences without hiding the signed delta", () => {
  const valid = check({ comparison_value: 100.5, tolerance_absolute: 1, tolerance_relative: 0 });
  const warning = check({ code: "cloud_invoice_vs_cost_import", comparison_value: 101.5, tolerance_absolute: 1, tolerance_relative: 0 });
  const invalid = check({ code: "measured_usage_vs_invoiced_usage", comparison_value: 103, tolerance_absolute: 1, tolerance_relative: 0 });
  const result = rollupReconciliation(input([valid, warning, invalid]));
  assert.deepEqual(result.stats.statuses, { valid: 1, warning: 1, invalid: 1 });
  assert.equal(result.evidence.items[0].reconciliation_difference, 0.5);
  assert.equal(result.evidence.items[1].reconciliation_difference, 1.5);
  assert.equal(result.evidence.items[2].reconciliation_difference, 3);
  assert.deepEqual(result.evidence.items.map((item) => item.events_rejected), [0, 1, 1]);
});

test("rejects unknown, duplicate and same-source checks", () => {
  assert.throws(() => rollupReconciliation(input([check({ code: "raw_customer_dump" })])), /not an approved reconciliation/);
  assert.throws(() => rollupReconciliation(input([check(), check()])), /is duplicated/);
  assert.throws(
    () => rollupReconciliation(input([check({ comparison_source: "payment-provider" })])),
    /two distinct sources/,
  );
});

test("rejects cross-environment, open periods and insecure drilldowns", () => {
  assert.throws(() => rollupReconciliation({ ...input([check()]), environment: "production" }), /develop or main/);
  assert.throws(
    () => rollupReconciliation({ ...input([check()]), period_end: "2026-07-17T00:00:00Z" }),
    /newer than the source watermark/,
  );
  assert.throws(
    () => rollupReconciliation(input([check({ drilldown_url: "https://user:pass@example.test" })])),
    /must not contain credentials/,
  );
  assert.throws(
    () => rollupReconciliation(input([check({ drilldown_url: "https://example.test/detail?api_key=secret" })])),
    /sensitive query parameter/,
  );
});

test("requires exact count values and bounded tolerances", () => {
  assert.throws(() => rollupReconciliation(input([check({ unit: "seats", authoritative_value: 1.5 })])), /count values must be integers/);
  assert.throws(() => rollupReconciliation(input([check({ tolerance_relative: 1.1 })])), /between 0 and 1/);
  assert.throws(() => rollupReconciliation(input([check({ warning_multiplier: 11 })])), /must not exceed 10/);
});

test("rejects raw or unknown payload fields", () => {
  assert.throws(() => rollupReconciliation(input([check({ raw_records: [{ customer: "private" }] })])), /unsupported fields: raw_records/);
  assert.throws(() => rollupReconciliation({ ...input([check()]), credentials: "forbidden" }), /unsupported fields: credentials/);
});
