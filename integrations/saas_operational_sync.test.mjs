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
        backup_contract_complete: true,
        failure_count_24h: 0,
        snapshot_count: 7,
        pitr_enabled: true,
        pitr_window_seconds: 86400,
        wal_archive_status: "healthy",
        secondary_copy_status: "healthy",
        storage_cost_monthly_eur: 42.5,
        drilldown_url: "https://evidence.example.test/backup/123",
      },
    ],
  };
}

function syncEvidence(environment = "develop") {
  return {
    model: "saas.sync.run",
    environment,
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: `${environment}:sync:2026-07-16T12:00:00Z`,
        name: "Complete Odoo sync attempt",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "partial",
        sync_contract_complete: true,
        records_read: 100,
        records_created: 30,
        records_updated: 40,
        records_skipped: 20,
        records_rejected: 10,
        duplicate_upsert_count: 2,
        api_error_count: 2,
        authentication_error_count: 1,
        permission_error_count: 0,
        rate_limit_error_count: 1,
        retry_count: 3,
        backlog_count: 5,
        oldest_unsynced_at: "2026-07-16T11:30:00Z",
        error_code: "PARTIAL_SOURCE_REJECTS",
        drilldown_url: "https://evidence.example.test/sync/123",
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
  assert.equal(result.items[0].backup_contract_complete, true);
});

test("fails closed on incomplete or internally inconsistent complete backup evidence", () => {
  const missingSnapshotCount = backupEvidence();
  delete missingSnapshotCount.items[0].snapshot_count;
  assert.throws(
    () => validateOperationalEvidence(missingSnapshotCount),
    /complete backup evidence requires snapshot_count/,
  );

  const disabledPitrWithArchive = backupEvidence();
  disabledPitrWithArchive.items[0].pitr_enabled = false;
  disabledPitrWithArchive.items[0].pitr_window_seconds = 0;
  assert.throws(
    () => validateOperationalEvidence(disabledPitrWithArchive),
    /disabled PITR requires a zero window and not_applicable WAL\/archive status/,
  );

  const falseSuccess = backupEvidence();
  falseSuccess.items[0].secondary_copy_status = "unhealthy";
  assert.throws(
    () => validateOperationalEvidence(falseSuccess),
    /successful complete backup requires size, checksum, encryption, off-host storage and a healthy secondary copy/,
  );
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

test("routes only complete and consistent sync evidence to the dedicated method", async () => {
  const normalized = validateOperationalEvidence(
    syncEvidence(),
    Date.parse("2026-07-16T12:05:00Z"),
  );
  assert.equal(normalized.items[0].sync_contract_complete, true);
  assert.equal(normalized.items[0].backlog_count, 5);

  let capturedUrl;
  await runOperationalSync(config, syncEvidence(), {
    env: { ARCIGY_ODOO_API_KEY: "test-only-key" },
    requestJson: async (url) => {
      capturedUrl = url;
      return { ok: true, created: 1, updated: 0 };
    },
  });
  assert.equal(
    capturedUrl,
    "https://odoo.example.test/json/2/saas.sync.run/ingest_sync_run_batch",
  );

  const incomplete = syncEvidence();
  delete incomplete.items[0].backlog_count;
  assert.throws(
    () => validateOperationalEvidence(incomplete),
    /complete sync evidence requires backlog_count/,
  );

  const falseSuccess = syncEvidence();
  falseSuccess.items[0].status = "success";
  assert.throws(
    () => validateOperationalEvidence(falseSuccess),
    /successful sync evidence cannot contain errors/,
  );

  const missingOldest = syncEvidence();
  delete missingOldest.items[0].oldest_unsynced_at;
  assert.throws(
    () => validateOperationalEvidence(missingOldest),
    /positive backlog requires oldest_unsynced_at/,
  );

  const unsafeError = syncEvidence();
  unsafeError.items[0].error_code = "raw provider error with details";
  assert.throws(
    () => validateOperationalEvidence(unsafeError),
    /error_code must be a bounded symbolic code/,
  );
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
        clock_skew_seconds: 2.5,
        processing_lag_p95_seconds: 0.75,
        dead_letter_count: 1,
      },
    ],
  };
  const normalized = validateOperationalEvidence(
    evidence,
    Date.parse("2026-07-16T12:05:00Z"),
  );
  assert.equal(normalized.items[0].event_stream_complete, true);
  assert.equal(normalized.items[0].retry_adjustment_count, 2);
  assert.equal(normalized.items[0].clock_skew_seconds, 2.5);

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

test("requires a complete and bounded metric-quality contract", () => {
  const evidence = {
    model: "saas.data.quality.run",
    environment: "develop",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "develop:metric-quality:2026-07-16T12:00:00Z",
        name: "Complete metric quality scan",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "warning",
        metric_quality_contract_complete: true,
        eligible_metric_count: 100,
        fresh_metric_count: 90,
        complete_metric_count: 95,
        unique_metric_count: 100,
        valid_metric_count: 98,
        consistent_metric_count: 97,
        reconciliation_difference: -2.5,
        outlier_count: 3,
        unexpected_zero_count: 2,
        unexpected_volume_spike_count: 1,
        numerator_denominator_violation_count: 1,
        negative_value_violation_count: 2,
        missing_dimension_count: 5,
      },
    ],
  };
  const normalized = validateOperationalEvidence(
    evidence,
    Date.parse("2026-07-16T12:05:00Z"),
  );
  assert.equal(normalized.items[0].metric_quality_contract_complete, true);
  assert.equal(normalized.items[0].reconciliation_difference, -2.5);

  const missingField = structuredClone(evidence);
  delete missingField.items[0].missing_dimension_count;
  assert.throws(
    () => validateOperationalEvidence(missingField),
    /complete metric-quality evidence requires missing_dimension_count/,
  );

  const excessiveResult = structuredClone(evidence);
  excessiveResult.items[0].fresh_metric_count = 101;
  assert.throws(
    () => validateOperationalEvidence(excessiveResult),
    /fresh_metric_count cannot exceed eligible_metric_count/,
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

test("validates complete restore and disaster-recovery contracts", () => {
  const restore = {
    model: "saas.restore.test",
    environment: "develop",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "develop:restore:complete:2026-07-16T12:00:00Z",
        name: "Complete isolated restore drill",
        started_at: "2026-07-16T12:00:00Z",
        finished_at: "2026-07-16T12:04:00Z",
        status: "success",
        restore_contract_complete: true,
        actual_rpo_seconds: 0,
        actual_rto_seconds: 240,
        rpo_measured: true,
        rto_measured: true,
        checksum_valid: true,
        application_smoke_passed: true,
        tenant_isolation_passed: true,
        missing_record_count: 0,
        owner_team: "Engineering",
        next_test_at: "2026-08-16T12:00:00Z",
      },
    ],
  };
  const normalizedRestore = validateOperationalEvidence(
    restore,
    Date.parse("2026-07-16T12:05:00Z"),
  );
  assert.equal(normalizedRestore.items[0].restore_contract_complete, true);
  assert.equal(normalizedRestore.items[0].missing_record_count, 0);

  const missingOwner = structuredClone(restore);
  delete missingOwner.items[0].owner_team;
  assert.throws(
    () => validateOperationalEvidence(missingOwner),
    /complete restore evidence requires owner_team/,
  );

  const missingRecords = structuredClone(restore);
  missingRecords.items[0].missing_record_count = 1;
  assert.throws(
    () => validateOperationalEvidence(missingRecords),
    /successful complete restore cannot have missing records/,
  );

  const dr = {
    model: "saas.dr.drill",
    environment: "main",
    source_updated_at: "2026-07-16T12:05:00Z",
    items: [
      {
        external_key: "main:dr-drill:2026-07-16T11:00:00Z",
        name: "Complete Main DR drill",
        started_at: "2026-07-16T11:00:00Z",
        finished_at: "2026-07-16T12:00:00Z",
        status: "success",
        dr_contract_complete: true,
        failover_duration_seconds: 600,
        failback_duration_seconds: 1200,
        dns_propagation_duration_seconds: 180,
        unavailable_dependency_count: 0,
        runbook_accuracy_rate: 100,
        open_remediation_action_count: 0,
        owner_team: "Engineering",
        next_drill_at: "2026-10-16T11:00:00Z",
      },
    ],
  };
  const normalizedDr = validateOperationalEvidence(dr, Date.parse("2026-07-16T12:05:00Z"));
  assert.equal(normalizedDr.model, "saas.dr.drill");
  assert.equal(normalizedDr.items[0].runbook_accuracy_rate, 100);

  const inaccurate = structuredClone(dr);
  inaccurate.items[0].runbook_accuracy_rate = 100.1;
  assert.throws(
    () => validateOperationalEvidence(inaccurate),
    /runbook_accuracy_rate cannot exceed 100/,
  );

  const invalidNextDrill = structuredClone(dr);
  invalidNextDrill.items[0].next_drill_at = "2026-07-16T12:00:00Z";
  assert.throws(
    () => validateOperationalEvidence(invalidNextDrill),
    /next_drill_at must be after finished_at/,
  );
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
