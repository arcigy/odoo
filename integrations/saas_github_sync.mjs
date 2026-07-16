import { readFile, stat } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const API_VERSION = "2026-03-10";
const GITHUB_API_URL = "https://api.github.com";
const MAX_INPUT_BYTES = 1024 * 1024;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_PAGES = 10;
const PAGE_SIZE = 100;
const ENVIRONMENTS = ["develop", "main"];
const SAFE_REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const SAFE_BRANCH = /^[A-Za-z0-9._/-]{1,255}$/;
const SAFE_WORKFLOW = /^[A-Za-z0-9_.-]+\.ya?ml$/;
const SECRET_ENV_NAME = /^ARCIGY_[A-Z0-9_]+$/;

const METRICS = Object.freeze({
  build_success_rate: { direction: "higher", warning: 95, critical: 90, freshnessSeconds: 86400 },
  deployment_frequency: { direction: "neutral", freshnessSeconds: 86400 },
  lead_time_for_changes_seconds: {
    direction: "lower",
    warning: 259200,
    critical: 604800,
    freshnessSeconds: 86400,
  },
  critical_vulnerability_count: { direction: "lower", warning: 1, critical: 2, freshnessSeconds: 86400 },
  secret_scan_finding_count: { direction: "lower", warning: 1, critical: 2, freshnessSeconds: 86400 },
});

const ELIGIBLE_BUILD_CONCLUSIONS = new Set(["success", "failure", "timed_out", "action_required", "startup_failure"]);

function plainObject(value, name) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${name} must be an object.`);
  return value;
}

function rejectUnknownKeys(value, allowed, name) {
  const unknown = Object.keys(value).filter((key) => !allowed.has(key));
  if (unknown.length) throw new Error(`${name} contains unsupported fields: ${unknown.sort().join(", ")}.`);
}

function secureUrl(value, name) {
  const url = new URL(String(value || ""));
  if (url.username || url.password) throw new Error(`${name} must not contain credentials.`);
  const loopback = url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  if (url.protocol !== "https:" && !loopback) throw new Error(`${name} must use HTTPS except on loopback.`);
  url.pathname = url.pathname.replace(/\/+$/, "");
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

function secret(env, name) {
  if (!SECRET_ENV_NAME.test(String(name || ""))) throw new Error(`Invalid secret environment variable name: ${name}.`);
  const value = env[name]?.trim();
  if (!value) throw new Error(`${name} is required.`);
  return value;
}

function finiteDate(value, name) {
  const timestamp = Date.parse(String(value || ""));
  if (!Number.isFinite(timestamp)) throw new Error(`${name} must be an ISO-8601 timestamp.`);
  return timestamp;
}

function median(values) {
  if (!values.length) return undefined;
  const ordered = [...values].sort((a, b) => a - b);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2 ? ordered[middle] : (ordered[middle - 1] + ordered[middle]) / 2;
}

export function statusForGithubMetric(code, value) {
  const metric = METRICS[code];
  if (!metric) throw new Error(`Unknown GitHub metric code: ${code}.`);
  if (metric.direction === "neutral") return "unknown";
  if (metric.direction === "higher") {
    if (value < metric.critical) return "critical";
    if (value < metric.warning) return "warning";
    return "healthy";
  }
  if (value >= metric.critical) return "critical";
  if (value >= metric.warning) return "warning";
  return "healthy";
}

function validateSource(raw, environment) {
  const source = plainObject(raw, `sources.${environment}`);
  rejectUnknownKeys(
    source,
    new Set(["repository", "branch", "buildWorkflow", "deploymentWorkflow", "includeSecurity"]),
    `sources.${environment}`,
  );
  const repository = String(source.repository || "").trim();
  const branch = String(source.branch || "").trim();
  const buildWorkflow = String(source.buildWorkflow || "").trim();
  const deploymentWorkflow = String(source.deploymentWorkflow || "").trim();
  if (!SAFE_REPOSITORY.test(repository)) throw new Error(`sources.${environment}.repository is invalid.`);
  if (!SAFE_BRANCH.test(branch) || branch.includes("..") || branch.startsWith("/") || branch.endsWith("/")) {
    throw new Error(`sources.${environment}.branch is invalid.`);
  }
  if (!SAFE_WORKFLOW.test(buildWorkflow)) throw new Error(`sources.${environment}.buildWorkflow is invalid.`);
  if (!SAFE_WORKFLOW.test(deploymentWorkflow)) throw new Error(`sources.${environment}.deploymentWorkflow is invalid.`);
  if (typeof source.includeSecurity !== "boolean") throw new Error(`sources.${environment}.includeSecurity must be boolean.`);
  return { repository, branch, buildWorkflow, deploymentWorkflow, includeSecurity: source.includeSecurity };
}

export function validateGithubConfig(raw) {
  const config = plainObject(raw, "config");
  rejectUnknownKeys(config, new Set(["odoo", "github", "sources"]), "config");
  const odoo = plainObject(config.odoo, "config.odoo");
  rejectUnknownKeys(odoo, new Set(["url", "database", "apiKeyEnv"]), "config.odoo");
  const github = plainObject(config.github, "config.github");
  rejectUnknownKeys(github, new Set(["tokenEnv", "windowDays"]), "config.github");
  const sources = plainObject(config.sources, "config.sources");
  rejectUnknownKeys(sources, new Set(ENVIRONMENTS), "config.sources");

  const database = odoo.database === undefined ? undefined : String(odoo.database).trim();
  if (database !== undefined && !/^[A-Za-z0-9_.-]{1,128}$/.test(database)) {
    throw new Error("config.odoo.database contains unsupported characters.");
  }
  const apiKeyEnv = String(odoo.apiKeyEnv || "ARCIGY_ODOO_API_KEY");
  const githubTokenEnv = String(github.tokenEnv || "ARCIGY_GITHUB_READ_TOKEN");
  if (!SECRET_ENV_NAME.test(apiKeyEnv) || !SECRET_ENV_NAME.test(githubTokenEnv)) {
    throw new Error("Credential settings must name ARCIGY_ environment variables.");
  }
  const windowDays = Number(github.windowDays ?? 30);
  if (!Number.isInteger(windowDays) || windowDays < 1 || windowDays > 90) {
    throw new Error("config.github.windowDays must be an integer between 1 and 90.");
  }
  const normalizedSources = Object.fromEntries(
    ENVIRONMENTS.map((environment) => [environment, validateSource(sources[environment], environment)]),
  );
  if (ENVIRONMENTS.filter((environment) => normalizedSources[environment].includeSecurity).length > 1) {
    throw new Error("Repository-wide security findings may be assigned to only one environment.");
  }
  return {
    odoo: { url: secureUrl(odoo.url, "config.odoo.url"), database, apiKeyEnv },
    github: { tokenEnv: githubTokenEnv, windowDays },
    sources: normalizedSources,
  };
}

async function boundedJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    const target = new URL(url);
    if (Buffer.byteLength(text) > MAX_RESPONSE_BYTES) throw new Error(`Response from ${target.origin}${target.pathname} is too large.`);
    if (!response.ok) throw new Error(`${target.origin}${target.pathname} returned HTTP ${response.status}.`);
    return JSON.parse(text);
  } finally {
    clearTimeout(timeout);
  }
}

async function pagedGithub(path, headers, selectPage, requestJson) {
  const values = [];
  for (let page = 1; page <= MAX_PAGES; page += 1) {
    const separator = path.includes("?") ? "&" : "?";
    const raw = await requestJson(`${GITHUB_API_URL}${path}${separator}per_page=${PAGE_SIZE}&page=${page}`, { headers });
    const pageValues = selectPage(raw);
    if (!Array.isArray(pageValues)) throw new Error("GitHub returned an unexpected paginated response.");
    values.push(...pageValues);
    if (pageValues.length < PAGE_SIZE) return values;
  }
  throw new Error(`GitHub result exceeded the bounded ${MAX_PAGES * PAGE_SIZE}-record window.`);
}

async function boundedGithubList(path, headers, requestJson) {
  const separator = path.includes("?") ? "&" : "?";
  const values = await requestJson(`${GITHUB_API_URL}${path}${separator}per_page=${PAGE_SIZE}`, { headers });
  if (!Array.isArray(values)) throw new Error("GitHub returned an unexpected list response.");
  if (values.length >= PAGE_SIZE) {
    throw new Error(`GitHub security result reached the ${PAGE_SIZE}-record safety limit; cursor pagination is required.`);
  }
  return values;
}

function workflowPath(source, workflow, createdAfter) {
  const repository = source.repository.split("/").map(encodeURIComponent).join("/");
  const query = new URLSearchParams({
    branch: source.branch,
    status: "completed",
    created: `>=${createdAfter}`,
    exclude_pull_requests: "true",
  });
  return `/repos/${repository}/actions/workflows/${encodeURIComponent(workflow)}/runs?${query}`;
}

function metricItem(code, value, environment, source, periodStart, periodEnd, sampleCount, extra = {}) {
  const day = periodEnd.slice(0, 10);
  const repositoryKey = source.repository.replace("/", "-");
  return {
    code,
    value,
    status: statusForGithubMetric(code, value),
    measured_at: periodEnd,
    freshness_seconds: METRICS[code].freshnessSeconds,
    sample_count: sampleCount,
    external_key: `${environment}:github:${repositoryKey}:${code}:${day}`,
    period_start: periodStart,
    period_end: periodEnd,
    granularity: "day",
    drilldown_url: `https://github.com/${source.repository}/actions`,
    ...extra,
  };
}

export async function collectGithubEnvironment(
  config,
  environment,
  { env = process.env, requestJson = boundedJson, now = Date.now() } = {},
) {
  if (!ENVIRONMENTS.includes(environment)) throw new Error(`Invalid environment: ${environment}.`);
  const source = config.sources[environment];
  const token = secret(env, config.github.tokenEnv);
  const headers = {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "User-Agent": "Arcigy-SaaS-GitHub-Sync/1.0",
    "X-GitHub-Api-Version": API_VERSION,
  };
  const periodEnd = new Date(now).toISOString();
  const periodStart = new Date(now - config.github.windowDays * 86400_000).toISOString();
  const buildRuns = await pagedGithub(
    workflowPath(source, source.buildWorkflow, periodStart),
    headers,
    (raw) => raw?.workflow_runs,
    requestJson,
  );
  const deployRuns = await pagedGithub(
    workflowPath(source, source.deploymentWorkflow, periodStart),
    headers,
    (raw) => raw?.workflow_runs,
    requestJson,
  );
  const metrics = [];
  const omitted = [];
  const eligibleBuilds = buildRuns.filter((run) => ELIGIBLE_BUILD_CONCLUSIONS.has(run?.conclusion));
  if (eligibleBuilds.length) {
    const successfulBuilds = eligibleBuilds.filter((run) => run.conclusion === "success").length;
    metrics.push(
      metricItem(
        "build_success_rate",
        (100 * successfulBuilds) / eligibleBuilds.length,
        environment,
        source,
        periodStart,
        periodEnd,
        eligibleBuilds.length,
        { numerator: successfulBuilds, denominator: eligibleBuilds.length },
      ),
    );
  } else {
    omitted.push("build_success_rate:no_eligible_completed_runs");
  }

  const successfulDeploys = deployRuns.filter((run) => run?.conclusion === "success");
  metrics.push(
    metricItem(
      "deployment_frequency",
      successfulDeploys.length / config.github.windowDays,
      environment,
      source,
      periodStart,
      periodEnd,
      deployRuns.length,
    ),
  );
  const leadTimes = successfulDeploys.flatMap((run) => {
    const acceptedAt = Date.parse(run?.head_commit?.timestamp || "");
    const deployedAt = Date.parse(run?.updated_at || "");
    if (!Number.isFinite(acceptedAt) || !Number.isFinite(deployedAt) || deployedAt < acceptedAt) return [];
    return [(deployedAt - acceptedAt) / 1000];
  });
  const medianLeadTime = median(leadTimes);
  if (medianLeadTime !== undefined) {
    metrics.push(
      metricItem(
        "lead_time_for_changes_seconds",
        medianLeadTime,
        environment,
        source,
        periodStart,
        periodEnd,
        leadTimes.length,
      ),
    );
  } else {
    omitted.push("lead_time_for_changes_seconds:no_successful_deploy_with_commit_timestamp");
  }

  if (source.includeSecurity) {
    const repository = source.repository.split("/").map(encodeURIComponent).join("/");
    const dependabot = await boundedGithubList(
      `/repos/${repository}/dependabot/alerts?state=open`,
      headers,
      requestJson,
    );
    const criticalCount = dependabot.filter(
      (alert) => alert?.state === "open" && alert?.security_advisory?.severity === "critical",
    ).length;
    metrics.push(
      metricItem(
        "critical_vulnerability_count",
        criticalCount,
        environment,
        source,
        periodStart,
        periodEnd,
        dependabot.length,
        { drilldown_url: `https://github.com/${source.repository}/security/dependabot` },
      ),
    );
    const secretAlerts = await boundedGithubList(
      `/repos/${repository}/secret-scanning/alerts?state=open&hide_secret=true`,
      headers,
      requestJson,
    );
    const openSecretCount = secretAlerts.filter((alert) => alert?.state === "open").length;
    metrics.push(
      metricItem(
        "secret_scan_finding_count",
        openSecretCount,
        environment,
        source,
        periodStart,
        periodEnd,
        secretAlerts.length,
        { drilldown_url: `https://github.com/${source.repository}/security/secret-scanning` },
      ),
    );
  }
  return { environment, source, metrics, omitted, periodStart, periodEnd };
}

async function postToOdoo(config, collected, env, requestJson) {
  const headers = {
    Authorization: `Bearer ${secret(env, config.odoo.apiKeyEnv)}`,
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "Arcigy-SaaS-GitHub-Sync/1.0",
  };
  if (config.odoo.database) headers["X-Odoo-Database"] = config.odoo.database;
  return requestJson(`${config.odoo.url}/json/2/saas.metric.current/ingest_metric_batch`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      payload: {
        environment: collected.environment,
        source_updated_at: collected.periodEnd,
        metrics: collected.metrics,
      },
    }),
  });
}

export async function runGithubSync(
  rawConfig,
  {
    env = process.env,
    dryRun = false,
    environments = ENVIRONMENTS,
    requestJson = boundedJson,
    now = Date.now(),
  } = {},
) {
  const config = validateGithubConfig(rawConfig);
  if (!Array.isArray(environments) || !environments.length || environments.some((item) => !ENVIRONMENTS.includes(item))) {
    throw new Error("environments must select develop, main or both.");
  }
  const selected = [...new Set(environments)];
  const results = [];
  for (const environment of selected) {
    const collected = await collectGithubEnvironment(config, environment, { env, requestJson, now });
    const odoo = dryRun ? undefined : await postToOdoo(config, collected, env, requestJson);
    results.push({ ...collected, odoo });
  }
  return results;
}

async function readBoundedJson(path, name) {
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_INPUT_BYTES) {
    throw new Error(`${name} must be a JSON file no larger than ${MAX_INPUT_BYTES} bytes.`);
  }
  return JSON.parse(await readFile(path, "utf8"));
}

async function main() {
  const configArg = process.argv.find((arg) => arg.startsWith("--config="));
  const environmentArg = process.argv.find((arg) => arg.startsWith("--environment="));
  if (!configArg) {
    throw new Error("Usage: node saas_github_sync.mjs --config=<path> [--dry-run] [--environment=develop|main|all]");
  }
  const environment = environmentArg?.slice("--environment=".length) || "all";
  const environments = environment === "all" ? ENVIRONMENTS : [environment];
  const config = await readBoundedJson(configArg.slice("--config=".length), "config");
  const results = await runGithubSync(config, { dryRun: process.argv.includes("--dry-run"), environments });
  console.log(
    JSON.stringify(
      {
        ok: true,
        dryRun: process.argv.includes("--dry-run"),
        results: results.map((item) => ({
          environment: item.environment,
          metricCodes: item.metrics.map((metric) => metric.code),
          omitted: item.omitted,
        })),
      },
      null,
      2,
    ),
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
