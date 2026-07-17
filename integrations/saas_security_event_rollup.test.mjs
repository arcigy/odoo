import assert from "node:assert/strict";
import test from "node:test";

import { validateAggregateEvidence } from "./saas_aggregate_sync.mjs";
import { rollupSecurityEvents, validateSecurityEventConfig } from "./saas_security_event_rollup.mjs";

const hashA = `sha256:${"a".repeat(64)}`;
const hashB = `sha256:${"b".repeat(64)}`;
const config = { environment: "develop", server_sources: ["server", "worker"], security_stream_complete: true };

function event(overrides = {}) {
  return {
    event_id: "security-event-1",
    event_name: "login_failed",
    event_version: 1,
    occurred_at_utc: "2026-07-15T12:00:00Z",
    received_at_utc: "2026-07-15T12:00:01Z",
    environment: "develop",
    release_version: "2026.07.15",
    tenant_id: "tenant-opaque-1",
    user_id_hash: hashA,
    session_id: "session-opaque-1",
    feature: "authentication",
    object_type: "session",
    object_id_hash: hashB,
    outcome: "failed",
    failure_reason: "invalid_credentials",
    request_id: "request-1",
    trace_id: "trace-1",
    source: "server",
    plan: "professional",
    properties: {},
    ...overrides,
  };
}

function exported(events) {
  return { source_updated_at: "2026-07-16T00:05:00Z", events };
}

test("rolls complete server security events into anonymous daily evidence", () => {
  const events = [
    event(),
    event({ event_id: "e2", event_name: "login_succeeded", outcome: "succeeded", failure_reason: null }),
    event({ event_id: "e3", event_name: "rate_limit_triggered", outcome: "completed", failure_reason: null }),
    event({ event_id: "e4", event_name: "suspicious_login_detected", outcome: "completed", failure_reason: null }),
    event({ event_id: "e5", event_name: "cross_tenant_access_denied", outcome: "completed", failure_reason: null }),
    event({ event_id: "e6", event_name: "permission_changed", outcome: "completed", failure_reason: null }),
    event({ event_id: "e7", event_name: "webhook_rejected", outcome: "rejected", failure_reason: "invalid_signature" }),
    event({ event_id: "e8", event_name: "audit_log_delivery_failed", outcome: "failed", failure_reason: "provider_unavailable" }),
  ];
  const result = rollupSecurityEvents(config, exported(events));
  assert.equal(result.evidence.items.length, 1);
  assert.deepEqual(
    Object.fromEntries([
      "external_key", "period_start", "period_end", "status", "data_quality_status",
      "login_attempts", "login_failures", "rate_limit_events", "suspicious_login_count",
      "cross_tenant_denied_count", "confirmed_cross_tenant_exposure_count",
      "privileged_action_count", "webhook_signature_failure_count", "audit_delivery_failure_count",
    ].map((key) => [key, result.evidence.items[0][key]])),
    {
    external_key: "develop:security:event-rollup-v1:2026-07-15",
    period_start: "2026-07-15T00:00:00.000Z",
    period_end: "2026-07-16T00:00:00.000Z",
    status: "unknown",
    data_quality_status: "valid",
    login_attempts: 2,
    login_failures: 1,
    rate_limit_events: 1,
    suspicious_login_count: 1,
    cross_tenant_denied_count: 1,
    confirmed_cross_tenant_exposure_count: 0,
    privileged_action_count: 1,
    webhook_signature_failure_count: 1,
    audit_delivery_failure_count: 1,
    },
  );
  assert.equal(validateAggregateEvidence(result.evidence).items.length, 1);
  const serialized = JSON.stringify(result.evidence);
  assert.equal(serialized.includes("tenant-opaque"), false);
  assert.equal(serialized.includes(hashA), false);
});

test("rolls newly contracted credential and application-security events without identities", () => {
  const events = [
    event({ event_id: "key-created", event_name: "api_key_created", outcome: "completed", failure_reason: null }),
    event({ event_id: "key-revoked", event_name: "api_key_revoked", outcome: "completed", failure_reason: null }),
    event({ event_id: "oauth", event_name: "oauth_error", outcome: "failed", failure_reason: "provider_rejected" }),
    event({ event_id: "sql", event_name: "sql_injection_detected", outcome: "blocked", failure_reason: "rule_match" }),
    event({ event_id: "download", event_name: "unusual_download_volume_detected", outcome: "blocked", failure_reason: "policy" }),
  ];
  const item = rollupSecurityEvents(config, exported(events)).evidence.items[0];
  assert.equal(item.api_key_created_count, 1);
  assert.equal(item.api_key_revoked_count, 1);
  assert.equal(item.oauth_error_count, 1);
  assert.equal(item.sql_injection_detection_count, 1);
  assert.equal(item.unusual_download_volume_count, 1);
  assert.equal(JSON.stringify(item).includes("tenant-opaque"), false);
});

test("emits explicit zero security counts only for a declared complete stream", () => {
  const productOnly = event({
    event_name: "core_action_succeeded",
    outcome: "succeeded",
    failure_reason: null,
    feature: "project-editor",
  });
  const item = rollupSecurityEvents(config, exported([productOnly])).evidence.items[0];
  assert.equal(item.login_attempts, 0);
  assert.equal(item.privileged_action_count, 0);
  assert.throws(
    () => validateSecurityEventConfig({ ...config, security_stream_complete: false }),
    /must be explicitly true/,
  );
});

test("security and authorization events must originate from an approved server source", () => {
  assert.throws(
    () => rollupSecurityEvents(config, exported([event({ source: "browser" })])),
    /approved server source/,
  );
  assert.throws(
    () => rollupSecurityEvents(config, exported([event({ event_name: "cross_tenant_access_denied", source: "browser", outcome: "completed", failure_reason: null })])),
    /approved server source/,
  );
});

test("deduplicates exact event IDs and rejects conflicting duplicates", () => {
  const result = rollupSecurityEvents(config, exported([event(), event()]));
  assert.equal(result.stats.duplicatesSuppressed, 1);
  assert.equal(result.evidence.items[0].login_attempts, 1);
  assert.throws(
    () => rollupSecurityEvents(config, exported([event(), event({ failure_reason: "other" })])),
    /conflicting duplicate payloads/,
  );
});

test("rejects non-server source declarations and unknown config fields", () => {
  assert.throws(() => validateSecurityEventConfig({ ...config, server_sources: ["browser"] }), /approved server source/);
  assert.throws(() => validateSecurityEventConfig({ ...config, raw_log_path: "audit.log" }), /unsupported fields/);
});
