import assert from "node:assert/strict";
import test from "node:test";

import {
  runOperationalSync,
  validateOperationalConfig,
  validateOperationalEvidence,
} from "./saas_operational_sync.mjs";

const config = {
  odoo: {
    url: "https://odoo.example.test",
    database: "arcigy",
    apiKeyEnv: "ARCIGY_ODOO_API_KEY",
  },
};

function backupEvidence(environment = "develop") {
  return {
    model: "saas.backup.run",
    environment,
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: `${environment}:backup:2026-07-16T12:00:00Z`,
        name: "Encrypted off-host backup",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "success",
        backup_type: "full",
        size_bytes: 1048576,
        checksum: "sha256:example",
        encrypted: true,
        off_host: true,
        drilldown_url: "https://evidence.example.test/backup/123",
      },
    ],
  };
}

test("accepts only environment-prefixed, scalar operational evidence", () => {
  const result = validateOperationalEvidence(backupEvidence("develop"), Date.parse("2026-07-16T12:05:00Z"));
  assert.equal(result.model, "saas.backup.run");
  assert.equal(result.environment, "develop");
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].encrypted, true);
});

test("rejects cross-environment keys and unsupported raw fields", () => {
  const wrongEnvironment = backupEvidence("develop");
  wrongEnvironment.items[0].external_key = "main:backup:123";
  assert.throws(() => validateOperationalEvidence(wrongEnvironment), /prefixed by develop/);

  const rawLeak = backupEvidence("develop");
  rawLeak.items[0].raw_log = "private payload";
  assert.throws(() => validateOperationalEvidence(rawLeak), /unsupported fields: raw_log/);
});

test("rejects credentials in URLs and insecure external transport", () => {
  assert.throws(
    () => validateOperationalConfig({ odoo: { url: "https://user:pass@odoo.example.test" } }),
    /must not contain credentials/,
  );
  assert.throws(
    () => validateOperationalConfig({ odoo: { url: "http://odoo.example.test" } }),
    /must use HTTPS/,
  );
  assert.throws(
    () => validateOperationalConfig({ odoo: { url: "https://odoo.example.test", database: "bad\ndatabase" } }),
    /unsupported characters/,
  );
});

test("dry-run validates both environments without reading an API key or writing Odoo", async () => {
  let requests = 0;
  for (const environment of ["develop", "main"]) {
    const result = await runOperationalSync(config, backupEvidence(environment), {
      env: {},
      dryRun: true,
      requestJson: async () => {
        requests += 1;
        throw new Error("dry-run must not make requests");
      },
    });
    assert.equal(result.environment, environment);
    assert.equal(result.dryRun, true);
  }
  assert.equal(requests, 0);
});

test("live mode posts one bounded JSON-2 batch with secret-store authentication", async () => {
  let captured;
  const result = await runOperationalSync(config, backupEvidence("develop"), {
    env: { ARCIGY_ODOO_API_KEY: "test-only-key" },
    requestJson: async (url, options) => {
      captured = { url, options };
      return { ok: true, created: 1, updated: 0 };
    },
  });
  assert.equal(result.odoo.created, 1);
  assert.equal(captured.url, "https://odoo.example.test/json/2/saas.backup.run/ingest_operational_batch");
  assert.equal(captured.options.method, "POST");
  assert.equal(captured.options.headers.Authorization, "Bearer test-only-key");
  assert.equal(captured.options.headers["X-Odoo-Database"], "arcigy");
  const body = JSON.parse(captured.options.body);
  assert.deepEqual(Object.keys(body), ["payload"]);
  assert.equal(body.payload.environment, "develop");
  assert.equal(body.payload.items.length, 1);
});

test("rejects unsupported models and chronologically invalid evidence", () => {
  const unsupported = backupEvidence();
  unsupported.model = "res.partner";
  assert.throws(() => validateOperationalEvidence(unsupported), /Unsupported operational model/);

  const invalidTime = backupEvidence();
  invalidTime.items[0].finished_at = "2026-07-16T11:59:59Z";
  assert.throws(() => validateOperationalEvidence(invalidTime), /must be after started_at/);

  const future = backupEvidence();
  future.items[0].started_at = "2026-07-16T12:11:00Z";
  future.items[0].finished_at = "2026-07-16T12:12:00Z";
  assert.throws(
    () => validateOperationalEvidence(future, Date.parse("2026-07-16T12:05:00Z")),
    /started_at is too far in the future/,
  );
});

test("accepts a signed reconciliation difference without broadening the schema", () => {
  const evidence = {
    model: "saas.data.quality.run",
    environment: "main",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "main:data-quality:2026-07-16T12:00:00Z",
        name: "Hourly reconciliation",
        started_at: "2026-07-16T12:00:00Z",
        status: "warning",
        reconciliation_difference: -2,
      },
    ],
  };
  const result = validateOperationalEvidence(evidence, Date.parse("2026-07-16T12:05:00Z"));
  assert.equal(result.items[0].reconciliation_difference, -2);
});

test("requires a complete and internally consistent event-stream contract", () => {
  const evidence = {
    model: "saas.data.quality.run",
    environment: "develop",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "develop:event-stream:2026-07-16T12:00:00Z",
        name: "Complete event stream",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "warning",
        event_stream_complete: true,
        events_sent: 100,
        events_received: 97,
        events_processed: 96,
        events_rejected: 1,
        retry_adjustment_count: 2,
        duplicate_count: 1,
        schema_failure_count: 1,
        missing_field_count: 0,
        late_event_count: 1,
        unknown_tenant_count: 0,
      },
    ],
  };
  const normalized = validateOperationalEvidence(
    evidence,
    Date.parse("2026-07-16T12:05:00Z"),
  );
  assert.equal(normalized.items[0].event_stream_complete, true);
  assert.equal(normalized.items[0].retry_adjustment_count, 2);

  const missingCount = structuredClone(evidence);
  delete missingCount.items[0].retry_adjustment_count;
  assert.throws(
    () => validateOperationalEvidence(missingCount),
    /complete event-stream evidence requires retry_adjustment_count/,
  );

  const inconsistent = structuredClone(evidence);
  inconsistent.items[0].events_processed = 97;
  inconsistent.items[0].events_rejected = 1;
  assert.throws(
    () => validateOperationalEvidence(inconsistent),
    /processed and rejected events cannot exceed received events/,
  );

  const excessiveRetryAdjustment = structuredClone(evidence);
  excessiveRetryAdjustment.items[0].retry_adjustment_count = 4;
  assert.throws(
    () => validateOperationalEvidence(excessiveRetryAdjustment),
    /retry_adjustment_count exceeds the sent\/received difference/,
  );
});

test("validates restore and load evidence without accepting raw test output", () => {
  const restore = validateOperationalEvidence(
    {
      model: "saas.restore.test",
      environment: "develop",
      source_updated_at: "2026-07-16T12:05:00Z",
      items: [
        {
          external_key: "develop:restore:2026-07-16T12:00:00Z",
          name: "Isolated restore drill",
          started_at: "2026-07-16T12:00:00Z",
          finished_at: "2026-07-16T12:04:00Z",
          status: "success",
          actual_rpo_seconds: 0,
          actual_rto_seconds: 240,
          rpo_measured: true,
          rto_measured: true,
          checksum_valid: true,
          application_smoke_passed: true,
          tenant_isolation_passed: true,
        },
      ],
    },
    Date.parse("2026-07-16T12:05:00Z"),
  );
  assert.equal(restore.items[0].tenant_isolation_passed, true);

  const load = {
    model: "saas.load.test",
    environment: "main",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "main:load:2026-07-16T12:00:00Z",
        name: "Synthetic capacity test",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "ready_with_risk",
        test_type: "ramp",
        representative: true,
        architecture_version: "architecture-v1",
        concurrent_users: 1000,
        p95_seconds: 1.2,
        raw_results: ["must not leave the load-test backend"],
      },
    ],
  };
  const validLoad = structuredClone(load);
  delete validLoad.items[0].raw_results;
  const normalizedLoad = validateOperationalEvidence(
    validLoad,
    Date.parse("2026-07-16T12:05:00Z"),
  );
  assert.equal(normalizedLoad.items[0].representative, true);
  assert.equal(normalizedLoad.items[0].architecture_version, "architecture-v1");
  assert.throws(() => validateOperationalEvidence(load), /unsupported fields: raw_results/);
});

test("requires explicit restore measurements and representative load evidence", () => {
  const restore = {
    model: "saas.restore.test",
    environment: "develop",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "develop:restore:2026-07-16T12:00:00Z",
        name: "Restore without measurement marker",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "success",
        actual_rpo_seconds: 0,
        actual_rto_seconds: 240,
        rto_measured: true,
        checksum_valid: true,
        application_smoke_passed: true,
        tenant_isolation_passed: true,
      },
    ],
  };
  assert.throws(() => validateOperationalEvidence(restore), /actual_rpo_seconds requires rpo_measured=true/);

  const load = {
    model: "saas.load.test",
    environment: "main",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "main:load:2026-07-16T12:00:00Z",
        name: "Unqualified capacity claim",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "ready",
        test_type: "ramp",
        representative: true,
        concurrent_users: 1000,
      },
    ],
  };
  assert.throws(() => validateOperationalEvidence(load), /requires architecture_version/);
});
