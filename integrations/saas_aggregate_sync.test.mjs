import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  aggregateContractFields,
  runAggregateSync,
  validateAggregateConfig,
  validateAggregateEvidence,
} from "./saas_aggregate_sync.mjs";

const config = {
  odoo: {
    url: "https://odoo.example.test",
    database: "arcigy",
    apiKeyEnv: "ARCIGY_ODOO_API_KEY",
  },
};

const hourly = {
  period_start: "2026-07-16T12:00:00Z",
  period_end: "2026-07-16T13:00:00Z",
};
const daily = {
  period_start: "2026-07-15T00:00:00Z",
  period_end: "2026-07-16T00:00:00Z",
};

const modelItems = {
  "saas.tenant.daily": { ...daily, tenant_external_id: "tenant-1", active_users: 3 },
  "saas.endpoint.hourly": {
    ...hourly,
    method: "GET",
    endpoint_group: "projects",
    slo_class: "critical",
    request_count: 10,
  },
  "saas.database.hourly": { ...hourly, open_connections: 4 },
  "saas.cache.hourly": { ...hourly, namespace: "tenant-cache", hit_count: 9 },
  "saas.queue.hourly": { ...hourly, queue_name: "exports", job_type: "pdf", queue_depth: 2 },
  "saas.dependency.hourly": {
    ...hourly,
    integration_code: "email-provider",
    currency_code: "EUR",
    request_count: 5,
  },
  "saas.cost.daily": { ...daily, provider: "cloud", category: "compute", amount: 12.5, currency_code: "EUR" },
  "saas.product.daily": { ...daily, active_users: 4 },
  "saas.security.daily": { ...daily, login_attempts: 20 },
  "saas.capacity.daily": { ...daily, peak_rps: 15 },
};

function evidence(model, environment = "develop") {
  const cadenceTime = model.endsWith(".hourly") ? "2026-07-16T13:05:00Z" : "2026-07-16T00:05:00Z";
  return {
    model,
    environment,
    source_updated_at: cadenceTime,
    items: [
      {
        external_key: `${environment}:${model}:period-1`,
        status: "healthy",
        data_quality_status: "valid",
        ...modelItems[model],
      },
    ],
  };
}

test("validates all ten domain aggregate contracts", () => {
  assert.equal(Object.keys(modelItems).length, 10);
  for (const model of Object.keys(modelItems)) {
    const result = validateAggregateEvidence(evidence(model), Date.parse("2026-07-16T13:05:00Z"));
    assert.equal(result.model, model);
    assert.equal(result.items.length, 1);
  }
});

test("client allowlists exactly match every server aggregate model and field", async () => {
  const source = await readFile(
    new URL("../addons/arcigy_saas_control_center/models/aggregates.py", import.meta.url),
    "utf8",
  );
  const client = aggregateContractFields();
  assert.deepEqual(Object.keys(client).sort(), Object.keys(modelItems).sort());
  for (const [model, clientFields] of Object.entries(client)) {
    const escapedModel = model.replaceAll(".", "\\.");
    const match = source.match(new RegExp(`"${escapedModel}"\\s*:\\s*\\{([\\s\\S]*?)\\n\\s*\\},`));
    assert.ok(match, `Missing server allowlist for ${model}`);
    const serverFields = [...match[1].matchAll(/"([a-z0-9_]+)"/g)].map((item) => item[1]).sort();
    assert.deepEqual(clientFields, serverFields, model);
  }
});

test("rejects cross-environment keys and unapproved raw payloads", () => {
  const crossEnvironment = evidence("saas.endpoint.hourly", "develop");
  crossEnvironment.items[0].external_key = "main:endpoint:period-1";
  assert.throws(() => validateAggregateEvidence(crossEnvironment), /prefixed by develop/);

  const rawLeak = evidence("saas.security.daily");
  rawLeak.items[0].raw_events = [{ actor: "private" }];
  assert.throws(() => validateAggregateEvidence(rawLeak), /unsupported fields: raw_events/);
});

test("requires exact hourly and daily UTC periods", () => {
  const badHour = evidence("saas.database.hourly");
  badHour.items[0].period_end = "2026-07-16T12:59:00Z";
  assert.throws(() => validateAggregateEvidence(badHour), /exactly one hour/);

  const badDay = evidence("saas.product.daily");
  badDay.items[0].period_end = "2026-07-15T23:00:00Z";
  assert.throws(() => validateAggregateEvidence(badDay), /exactly one day/);
});

test("rejects invalid numeric, selection and empty aggregate data", () => {
  const negative = evidence("saas.queue.hourly");
  negative.items[0].queue_depth = -1;
  assert.throws(() => validateAggregateEvidence(negative), /must not be negative/);

  const invalidSelection = evidence("saas.cost.daily");
  invalidSelection.items[0].category = "unknown-provider-category";
  assert.throws(() => validateAggregateEvidence(invalidSelection), /unsupported value/);

  const empty = evidence("saas.database.hourly");
  delete empty.items[0].open_connections;
  assert.throws(() => validateAggregateEvidence(empty), /must contain at least one/);

  const inconsistent = evidence("saas.endpoint.hourly");
  inconsistent.items[0].request_count = 10;
  inconsistent.items[0].success_count = 11;
  assert.throws(() => validateAggregateEvidence(inconsistent), /cannot exceed request_count/);
});

test("allows signed cost adjustments and storage growth", () => {
  const cost = evidence("saas.cost.daily");
  cost.items[0].amount = -2.5;
  assert.equal(validateAggregateEvidence(cost).items[0].amount, -2.5);

  const database = evidence("saas.database.hourly");
  database.items[0].storage_growth_bytes = -1024;
  assert.equal(validateAggregateEvidence(database).items[0].storage_growth_bytes, -1024);
});

test("rejects credentials in config and drilldown URLs", () => {
  assert.throws(
    () => validateAggregateConfig({ odoo: { url: "https://user:pass@odoo.example.test" } }),
    /must not contain credentials/,
  );
  const item = evidence("saas.endpoint.hourly");
  item.items[0].drilldown_url = "https://user:pass@observability.example.test/detail";
  assert.throws(() => validateAggregateEvidence(item), /must not contain credentials/);
});

test("dry-run validates without reading credentials or making requests", async () => {
  let requests = 0;
  const result = await runAggregateSync(config, evidence("saas.product.daily"), {
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

test("live mode posts one bounded JSON-2 aggregate batch", async () => {
  let captured;
  const result = await runAggregateSync(config, evidence("saas.endpoint.hourly", "main"), {
    env: { ARCIGY_ODOO_API_KEY: "test-only-key" },
    requestJson: async (url, options) => {
      captured = { url, options };
      return { ok: true, created: 1, updated: 0 };
    },
  });
  assert.equal(result.odoo.created, 1);
  assert.equal(captured.url, "https://odoo.example.test/json/2/saas.endpoint.hourly/ingest_aggregate_batch");
  assert.equal(captured.options.headers.Authorization, "Bearer test-only-key");
  assert.equal(captured.options.headers["X-Odoo-Database"], "arcigy");
  const payload = JSON.parse(captured.options.body).payload;
  assert.equal(payload.environment, "main");
  assert.equal(payload.items.length, 1);
});

test("requires known models, required dimensions and bounded item counts", () => {
  const unknown = evidence("saas.product.daily");
  unknown.model = "res.partner";
  assert.throws(() => validateAggregateEvidence(unknown), /Unsupported aggregate model/);

  const dependency = evidence("saas.dependency.hourly");
  delete dependency.items[0].integration_code;
  assert.throws(() => validateAggregateEvidence(dependency), /integration_code is required/);

  const tenantMoney = evidence("saas.tenant.daily");
  tenantMoney.items[0].mrr = 100;
  assert.throws(() => validateAggregateEvidence(tenantMoney), /currency_code is required/);

  const tooMany = evidence("saas.product.daily");
  tooMany.items = Array.from({ length: 501 }, (_, index) => ({
    ...tooMany.items[0],
    external_key: `develop:product:${index}`,
  }));
  assert.throws(() => validateAggregateEvidence(tooMany), /between 1 and 500/);
});
