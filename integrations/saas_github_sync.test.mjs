import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  collectGithubEnvironment,
  runGithubSync,
  statusForGithubMetric,
  validateGithubConfig,
} from "./saas_github_sync.mjs";

function rawConfig() {
  return {
    odoo: {
      url: "https://odoo.example.test",
      database: "arcigy",
      apiKeyEnv: "ARCIGY_ODOO_API_KEY",
    },
    github: { tokenEnv: "ARCIGY_GITHUB_READ_TOKEN", windowDays: 30, stalePullRequestDays: 14 },
    sources: {
      develop: {
        repository: "arcigy/kitchen_app",
        branch: "develop",
        buildWorkflow: "ci.yml",
        deploymentWorkflow: "deploy-caprover.yml",
        includeSecurity: false,
      },
      main: {
        repository: "arcigy/kitchen_app",
        branch: "main",
        buildWorkflow: "ci.yml",
        deploymentWorkflow: "deploy-caprover.yml",
        includeSecurity: true,
      },
    },
  };
}

function run(conclusion, acceptedAt, completedAt, queuedAt, startedAt) {
  return {
    conclusion,
    head_commit: acceptedAt ? { timestamp: acceptedAt } : null,
    created_at: queuedAt,
    run_started_at: startedAt,
    updated_at: completedAt,
  };
}

function githubFixture({ emptyBuilds = false, securityAlertCount = 3, invalidPullDetail = false } = {}) {
  const requests = [];
  const odooPosts = [];
  const requestJson = async (url, options = {}) => {
    requests.push({ url, options });
    const parsed = new URL(url);
    if (parsed.hostname === "odoo.example.test") {
      odooPosts.push({ url, options });
      return { ok: true, created: 1 };
    }
    assert.equal(parsed.hostname, "api.github.com");
    assert.equal(options.headers["X-GitHub-Api-Version"], "2026-03-10");
    assert.equal(options.headers.Authorization, "Bearer test-read-token");
    const branch = parsed.searchParams.get("branch") || parsed.searchParams.get("base");
    if (parsed.pathname.endsWith("/actions/workflows/ci.yml/runs")) {
      if (emptyBuilds) return { workflow_runs: [] };
      return {
        workflow_runs:
          branch === "develop"
            ? [
                run("success", null, "2026-07-15T10:11:00Z", "2026-07-15T10:00:00Z", "2026-07-15T10:01:00Z"),
                run("failure", null, "2026-07-14T11:22:00Z", "2026-07-14T11:00:00Z", "2026-07-14T11:02:00Z"),
                run("cancelled"),
              ]
            : [
                run("success", null, "2026-07-15T09:06:00Z", "2026-07-15T09:00:00Z", "2026-07-15T09:01:00Z"),
                run("success", null, "2026-07-14T09:12:00Z", "2026-07-14T09:00:00Z", "2026-07-14T09:02:00Z"),
                run("success", null, "2026-07-13T09:18:00Z", "2026-07-13T09:00:00Z", "2026-07-13T09:03:00Z"),
              ],
      };
    }
    if (parsed.pathname.endsWith("/actions/workflows/deploy-caprover.yml/runs")) {
      return {
        workflow_runs:
          branch === "develop"
            ? [
                run("success", "2026-07-15T10:00:00Z", "2026-07-15T10:10:00Z", "2026-07-15T10:00:00Z", "2026-07-15T10:02:00Z"),
                run("success", "2026-07-14T10:00:00Z", "2026-07-14T10:20:00Z", "2026-07-14T10:00:00Z", "2026-07-14T10:05:00Z"),
                run("failure", "2026-07-13T10:00:00Z", "2026-07-13T10:05:00Z", "2026-07-13T10:00:00Z", "2026-07-13T10:01:00Z"),
              ]
            : [
                run("success", "2026-07-15T09:00:00Z", "2026-07-15T09:30:00Z", "2026-07-15T09:00:00Z", "2026-07-15T09:05:00Z"),
              ],
      };
    }
    if (parsed.pathname.endsWith("/pulls")) {
      if (parsed.searchParams.get("state") === "open") {
        return branch === "develop"
          ? [
              { number: 1, updated_at: "2026-07-15T12:00:00Z" },
              { number: 2, updated_at: "2026-06-30T12:00:00Z" },
            ]
          : [{ number: 3, updated_at: "2026-07-16T10:00:00Z" }];
      }
      return branch === "develop"
        ? [
            {
              number: 11,
              created_at: "2026-07-15T09:00:00Z",
              merged_at: "2026-07-15T10:00:00Z",
              updated_at: "2026-07-15T10:00:00Z",
            },
            {
              number: 12,
              created_at: "2026-07-12T10:00:00Z",
              merged_at: "2026-07-14T10:00:00Z",
              updated_at: "2026-07-14T10:00:00Z",
            },
          ]
        : [];
    }
    if (parsed.pathname.endsWith("/pulls/11")) {
      return {
        number: 11,
        additions: invalidPullDetail ? -1 : 100,
        deletions: 50,
        changed_files: 5,
        created_at: "2026-07-15T09:00:00Z",
        merged_at: "2026-07-15T10:00:00Z",
      };
    }
    if (parsed.pathname.endsWith("/pulls/12")) {
      return {
        number: 12,
        additions: 1000,
        deletions: 200,
        changed_files: 25,
        created_at: "2026-07-12T10:00:00Z",
        merged_at: "2026-07-14T10:00:00Z",
      };
    }
    if (parsed.pathname.endsWith("/dependabot/alerts")) {
      return Array.from({ length: securityAlertCount }, (_, index) => ({
        state: "open",
        security_advisory: { severity: index < 2 ? "critical" : "high" },
      }));
    }
    if (parsed.pathname.endsWith("/secret-scanning/alerts")) {
      assert.equal(parsed.searchParams.get("hide_secret"), "true");
      return [{ state: "open" }];
    }
    throw new Error(`Unexpected fixture URL: ${url}`);
  };
  return { requestJson, requests, odooPosts };
}

test("requires explicit Develop and Main branch contracts", () => {
  const config = validateGithubConfig(rawConfig());
  assert.equal(config.sources.develop.branch, "develop");
  assert.equal(config.sources.main.branch, "main");

  const unsafe = rawConfig();
  unsafe.sources.develop.branch = "../main";
  assert.throws(() => validateGithubConfig(unsafe), /branch is invalid/);

  const duplicateSecurity = rawConfig();
  duplicateSecurity.sources.develop.includeSecurity = true;
  assert.throws(() => validateGithubConfig(duplicateSecurity), /only one environment/);

  const invalidStaleWindow = rawConfig();
  invalidStaleWindow.github.stalePullRequestDays = 0;
  assert.throws(() => validateGithubConfig(invalidStaleWindow), /stalePullRequestDays/);
});

test("calculates status from the Odoo metric contract thresholds", () => {
  assert.equal(statusForGithubMetric("build_success_rate", 99), "healthy");
  assert.equal(statusForGithubMetric("build_success_rate", 92), "warning");
  assert.equal(statusForGithubMetric("build_success_rate", 80), "critical");
  assert.equal(statusForGithubMetric("critical_vulnerability_count", 0), "healthy");
  assert.equal(statusForGithubMetric("critical_vulnerability_count", 1), "warning");
  assert.equal(statusForGithubMetric("critical_vulnerability_count", 2), "critical");
  assert.equal(statusForGithubMetric("deployment_frequency", 4), "unknown");
});

test("every emitted GitHub metric is present in the seeded Odoo contract", async () => {
  const csv = await readFile(
    new URL("../addons/arcigy_saas_control_center/data/saas.metric.definition.csv", import.meta.url),
    "utf8",
  );
  for (const code of [
    "build_success_rate",
    "build_count",
    "build_duration_p95_seconds",
    "build_queue_p95_seconds",
    "deployment_frequency",
    "deployment_count",
    "deployment_success_rate",
    "deployment_duration_p95_seconds",
    "deployment_queue_p95_seconds",
    "lead_time_for_changes_seconds",
    "open_pr_count",
    "pr_cycle_time_p50_seconds",
    "pr_average_diff_lines",
    "pr_average_files_changed",
    "stale_pr_count",
    "critical_vulnerability_count",
    "secret_scan_finding_count",
  ]) {
    assert.match(csv, new RegExp(`,${code},`));
  }
});

test("collects branch-scoped CI and deployment metrics without inventing rollback data", async () => {
  const config = validateGithubConfig(rawConfig());
  const fixture = githubFixture();
  const collected = await collectGithubEnvironment(config, "develop", {
    env: { ARCIGY_GITHUB_READ_TOKEN: "test-read-token" },
    requestJson: fixture.requestJson,
    now: Date.parse("2026-07-16T12:00:00Z"),
  });
  const byCode = Object.fromEntries(collected.metrics.map((metric) => [metric.code, metric]));
  assert.equal(byCode.build_success_rate.value, 50);
  assert.equal(byCode.build_success_rate.denominator, 2);
  assert.equal(byCode.build_count.value, 2);
  assert.equal(byCode.build_duration_p95_seconds.value, 1200);
  assert.equal(byCode.build_queue_p95_seconds.value, 120);
  assert.equal(byCode.deployment_frequency.value, 2 / 30);
  assert.equal(byCode.deployment_count.value, 3);
  assert.equal(byCode.deployment_success_rate.numerator, 2);
  assert.equal(byCode.deployment_success_rate.denominator, 3);
  assert.equal(byCode.deployment_duration_p95_seconds.value, 900);
  assert.equal(byCode.deployment_queue_p95_seconds.value, 300);
  assert.equal(byCode.lead_time_for_changes_seconds.value, 900);
  assert.equal(byCode.open_pr_count.value, 2);
  assert.equal(byCode.stale_pr_count.value, 1);
  assert.equal(byCode.pr_cycle_time_p50_seconds.value, 88200);
  assert.equal(byCode.pr_average_diff_lines.value, 675);
  assert.equal(byCode.pr_average_files_changed.value, 15);
  assert.equal(byCode.change_failure_rate, undefined);
  assert.equal(byCode.release_rollback_rate, undefined);
  assert.ok(collected.metrics.every((metric) => metric.external_key.startsWith("develop:github:")));
});

test("collects repository-wide security counts only for the configured environment", async () => {
  const config = validateGithubConfig(rawConfig());
  const fixture = githubFixture();
  const main = await collectGithubEnvironment(config, "main", {
    env: { ARCIGY_GITHUB_READ_TOKEN: "test-read-token" },
    requestJson: fixture.requestJson,
    now: Date.parse("2026-07-16T12:00:00Z"),
  });
  const byCode = Object.fromEntries(main.metrics.map((metric) => [metric.code, metric.value]));
  assert.equal(byCode.critical_vulnerability_count, 2);
  assert.equal(byCode.secret_scan_finding_count, 1);
  assert.equal(byCode.stale_pr_count, 0);
  assert.equal(byCode.pr_cycle_time_p50_seconds, undefined);
  assert.ok(main.omitted.includes("pr_cycle_time_p50_seconds:no_merged_pull_requests"));
  assert.equal(
    fixture.requests.some((request) => request.url.includes("secret-scanning/alerts?state=open&hide_secret=true")),
    true,
  );
});

test("dry-run reads GitHub but never writes Odoo", async () => {
  const fixture = githubFixture();
  const results = await runGithubSync(rawConfig(), {
    env: { ARCIGY_GITHUB_READ_TOKEN: "test-read-token" },
    dryRun: true,
    requestJson: fixture.requestJson,
    now: Date.parse("2026-07-16T12:00:00Z"),
  });
  assert.equal(results.length, 2);
  assert.equal(fixture.odooPosts.length, 0);
  assert.ok(fixture.requests.length >= 6);
});

test("live mode posts separate bounded Odoo payloads for Develop and Main", async () => {
  const fixture = githubFixture();
  await runGithubSync(rawConfig(), {
    env: {
      ARCIGY_GITHUB_READ_TOKEN: "test-read-token",
      ARCIGY_ODOO_API_KEY: "test-odoo-key",
    },
    requestJson: fixture.requestJson,
    now: Date.parse("2026-07-16T12:00:00Z"),
  });
  assert.equal(fixture.odooPosts.length, 2);
  const payloads = fixture.odooPosts.map((post) => JSON.parse(post.options.body).payload);
  assert.deepEqual(payloads.map((payload) => payload.environment), ["develop", "main"]);
  assert.ok(payloads[0].metrics.every((metric) => metric.external_key.startsWith("develop:")));
  assert.ok(payloads[1].metrics.every((metric) => metric.external_key.startsWith("main:")));
  assert.ok(fixture.odooPosts.every((post) => post.options.headers.Authorization === "Bearer test-odoo-key"));
});

test("an empty build window is explicit and does not become a fake success rate", async () => {
  const config = validateGithubConfig(rawConfig());
  const fixture = githubFixture({ emptyBuilds: true });
  const collected = await collectGithubEnvironment(config, "develop", {
    env: { ARCIGY_GITHUB_READ_TOKEN: "test-read-token" },
    requestJson: fixture.requestJson,
    now: Date.parse("2026-07-16T12:00:00Z"),
  });
  assert.equal(collected.metrics.some((metric) => metric.code === "build_success_rate"), false);
  assert.equal(collected.metrics.find((metric) => metric.code === "build_count").value, 0);
  assert.ok(collected.omitted.includes("build_success_rate:no_eligible_completed_runs"));
  assert.ok(collected.omitted.includes("build_duration_p95_seconds:no_valid_completed_build_timing"));
  assert.ok(collected.omitted.includes("build_queue_p95_seconds:no_valid_completed_build_timing"));
});

test("rejects missing credentials and invalid environment selections before collection", async () => {
  const fixture = githubFixture();
  await assert.rejects(
    () => runGithubSync(rawConfig(), { env: {}, dryRun: true, requestJson: fixture.requestJson }),
    /ARCIGY_GITHUB_READ_TOKEN is required/,
  );
  await assert.rejects(
    () => runGithubSync(rawConfig(), { env: { ARCIGY_GITHUB_READ_TOKEN: "x" }, environments: ["prod"] }),
    /must select develop, main or both/,
  );
});

test("fails closed instead of undercounting a capped security result", async () => {
  const config = validateGithubConfig(rawConfig());
  const fixture = githubFixture({ securityAlertCount: 100 });
  await assert.rejects(
    () =>
      collectGithubEnvironment(config, "main", {
        env: { ARCIGY_GITHUB_READ_TOKEN: "test-read-token" },
        requestJson: fixture.requestJson,
        now: Date.parse("2026-07-16T12:00:00Z"),
      }),
    /cursor pagination is required/,
  );
});

test("fails closed on incomplete pull-request detail instead of emitting partial size metrics", async () => {
  const config = validateGithubConfig(rawConfig());
  const fixture = githubFixture({ invalidPullDetail: true });
  await assert.rejects(
    () =>
      collectGithubEnvironment(config, "develop", {
        env: { ARCIGY_GITHUB_READ_TOKEN: "test-read-token" },
        requestJson: fixture.requestJson,
        now: Date.parse("2026-07-16T12:00:00Z"),
      }),
    /additions must be a non-negative integer/,
  );
});
