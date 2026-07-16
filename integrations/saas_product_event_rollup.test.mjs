import assert from "node:assert/strict";
import test from "node:test";

import { rollupProductEvents, validateProductEventConfig } from "./saas_product_event_rollup.mjs";
import { validateAggregateEvidence } from "./saas_aggregate_sync.mjs";

const userHash = `sha256:${"a".repeat(64)}`;
const objectHash = `sha256:${"b".repeat(64)}`;
const config = {
  environment: "develop",
  server_sources: ["server", "worker"],
  meaningful_events: ["core_action_succeeded", "feature_used"],
};

function event(overrides = {}) {
  return {
    event_id: "evt-1",
    event_name: "core_action_succeeded",
    event_version: 1,
    occurred_at_utc: "2026-07-15T12:00:00Z",
    received_at_utc: "2026-07-15T12:00:01Z",
    environment: "develop",
    release_version: "2026.07.15",
    tenant_id: "tenant-opaque-1",
    user_id_hash: userHash,
    session_id: "session-opaque-1",
    feature: "project-editor",
    object_type: "project",
    object_id_hash: objectHash,
    outcome: "succeeded",
    failure_reason: null,
    request_id: "request-1",
    trace_id: "trace-1",
    source: "server",
    plan: "professional",
    properties: { workflow: "project-save" },
    ...overrides,
  };
}

function exported(events) {
  return { source_updated_at: "2026-07-16T00:05:00Z", events };
}

test("rolls deduplicated events into privacy-safe daily Odoo evidence", () => {
  const signup = event({ event_id: "evt-signup", event_name: "account_signed_up", feature: null, object_type: null, object_id_hash: null });
  const feature = event({ event_id: "evt-feature", event_name: "feature_used" });
  const result = rollupProductEvents(config, exported([event(), event(), signup, feature]));
  assert.deepEqual(result.stats, { eventsRead: 4, uniqueEvents: 3, duplicatesSuppressed: 1, dailyRows: 1 });
  assert.deepEqual(result.evidence.items[0], {
    external_key: "develop:product:event-rollup-v1:2026-07-15",
    period_start: "2026-07-15T00:00:00.000Z",
    period_end: "2026-07-16T00:00:00.000Z",
    status: "unknown",
    data_quality_status: "valid",
    active_users: 1,
    active_tenants: 1,
    core_actions: 1,
    signup_count: 1,
  });
  const serialized = JSON.stringify(result.evidence);
  assert.equal(serialized.includes("tenant-opaque"), false);
  assert.equal(serialized.includes(userHash), false);
  assert.equal(serialized.includes("properties"), false);
  assert.equal(validateAggregateEvidence(result.evidence).items.length, 1);
});

test("rejects conflicting duplicate IDs", () => {
  assert.throws(
    () => rollupProductEvents(config, exported([event(), event({ outcome: "completed" })])),
    /conflicting duplicate payloads/,
  );
});

test("rejects cross-environment and open-day events", () => {
  assert.throws(() => rollupProductEvents(config, exported([event({ environment: "main" })])), /must match develop/);
  assert.throws(
    () => rollupProductEvents(config, exported([event({ occurred_at_utc: "2026-07-16T00:01:00Z", received_at_utc: "2026-07-16T00:01:01Z" })])),
    /is not closed/,
  );
});

test("requires hashed identities, explicit UTC and bounded scalar properties", () => {
  assert.throws(() => rollupProductEvents(config, exported([event({ user_id_hash: "person@example.test" })])), /SHA-256 hash/);
  assert.throws(() => rollupProductEvents(config, exported([event({ occurred_at_utc: "2026-07-15T12:00:00+02:00" })])), /explicitly use UTC/);
  assert.throws(() => rollupProductEvents(config, exported([event({ properties: { email: "person@example.test" } })])), /forbidden PII/);
  assert.throws(() => rollupProductEvents(config, exported([event({ properties: { nested: { raw: true } } })])), /bounded scalar/);
});

test("requires billing and authorization events to originate on approved server sources", () => {
  assert.throws(
    () => rollupProductEvents(config, exported([event({ event_name: "payment_succeeded", source: "browser" })])),
    /approved server source/,
  );
  assert.throws(
    () => rollupProductEvents(config, exported([event({ event_name: "permission_changed", source: "browser" })])),
    /approved server source/,
  );
});

test("requires the complete versioned event envelope and failure reason", () => {
  const missing = event();
  delete missing.trace_id;
  assert.throws(() => rollupProductEvents(config, exported([missing])), /missing required fields: trace_id/);
  assert.throws(
    () => rollupProductEvents(config, exported([event({ event_name: "core_action_failed", outcome: "failed" })])),
    /failure_reason is required/,
  );
});

test("validates strict environment-scoped rollup configuration", () => {
  assert.equal(validateProductEventConfig(config).environment, "develop");
  assert.throws(() => validateProductEventConfig({ ...config, environment: "production" }), /develop or main/);
  assert.throws(() => validateProductEventConfig({ ...config, server_sources: ["browser"] }), /non-server sources/);
  assert.throws(() => validateProductEventConfig({ ...config, raw_log_path: "secret.log" }), /unsupported fields/);
});
