import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_RESPONSE_BYTES = 1024 * 1024;
const ENVIRONMENTS = ["develop", "main"];
const METRIC_CODE = /^[a-z][a-z0-9_]{2,127}$/;
const SECRET_ENV_NAME = /^ARCIGY_[A-Z0-9_]+$/;

function selectedEnvironments(value = ENVIRONMENTS) {
  if (!Array.isArray(value) || value.length === 0) {
    throw new Error("At least one environment must be selected.");
  }
  const selected = [...new Set(value.map((environment) => String(environment).trim()))];
  for (const environment of selected) {
    if (!ENVIRONMENTS.includes(environment)) {
      throw new Error(`Invalid environment: ${environment}. Expected develop or main.`);
    }
  }
  return selected;
}

function normalizedUrl(value, name) {
  const url = new URL(String(value || ""));
  if (url.username || url.password) throw new Error(`${name} must not contain credentials.`);
  const loopback = url.protocol === "http:" && ["127.0.0.1", "localhost", "::1"].includes(url.hostname);
  if (url.protocol !== "https:" && !loopback) throw new Error(`${name} must use HTTPS except on loopback.`);
  url.pathname = url.pathname.replace(/\/+$/, "");
  url.search = "";
  url.hash = "";
  return url.toString().replace(/\/$/, "");
}

function finiteNumber(value, name) {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new Error(`${name} must be a finite number.`);
  return number;
}

function secret(env, name, required = true) {
  if (!SECRET_ENV_NAME.test(String(name || ""))) throw new Error(`Invalid secret environment variable name: ${name}.`);
  const value = env[name]?.trim();
  if (required && !value) throw new Error(`${name} is required.`);
  return value;
}

export function validateConfig(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("Config must be an object.");
  const odoo = raw.odoo;
  if (!odoo || typeof odoo !== "object") throw new Error("odoo config is required.");
  const queries = raw.queries;
  if (!Array.isArray(queries) || queries.length === 0) throw new Error("At least one query is required.");
  const codes = new Set();
  const normalizedQueries = queries.map((query, index) => {
    if (!query || typeof query !== "object") throw new Error(`queries[${index}] must be an object.`);
    const code = String(query.code || "").trim();
    if (!METRIC_CODE.test(code)) throw new Error(`Invalid metric code at queries[${index}].`);
    if (codes.has(code)) throw new Error(`Duplicate metric code: ${code}.`);
    codes.add(code);
    const promql = String(query.promql || "").trim();
    if (!promql) throw new Error(`PromQL is required for ${code}.`);
    const direction = String(query.direction || "neutral");
    if (!['higher', 'lower', 'neutral'].includes(direction)) throw new Error(`Invalid direction for ${code}.`);
    return {
      code,
      promql,
      direction,
      warning: query.warning === undefined ? undefined : finiteNumber(query.warning, `${code}.warning`),
      critical: query.critical === undefined ? undefined : finiteNumber(query.critical, `${code}.critical`),
      freshnessSeconds: Math.max(30, Math.min(86400, finiteNumber(query.freshnessSeconds ?? 300, `${code}.freshnessSeconds`)))
    };
  });
  const sources = {};
  for (const environment of ENVIRONMENTS) {
    const source = raw.sources?.[environment];
    if (!source || typeof source !== "object") throw new Error(`sources.${environment} is required.`);
    const scopeMode = String(source.scopeMode || "");
    if (!['label_filtered', 'isolated_endpoint'].includes(scopeMode)) {
      throw new Error(`sources.${environment}.scopeMode must be label_filtered or isolated_endpoint.`);
    }
    const requiredQueryMarker = String(source.requiredQueryMarker || "").trim();
    if (scopeMode === "label_filtered" && !requiredQueryMarker) {
      throw new Error(`sources.${environment}.requiredQueryMarker is required for label_filtered scope.`);
    }
    sources[environment] = {
      prometheusUrl: normalizedUrl(source.prometheusUrl, `sources.${environment}.prometheusUrl`),
      scopeMode,
      requiredQueryMarker,
      tokenEnv: source.tokenEnv ? String(source.tokenEnv) : undefined
    };
  }
  return {
    odoo: {
      url: normalizedUrl(odoo.url, "odoo.url"),
      database: odoo.database ? String(odoo.database).trim() : undefined,
      apiKeyEnv: String(odoo.apiKeyEnv || "ARCIGY_ODOO_API_KEY")
    },
    sources,
    queries: normalizedQueries
  };
}

export function statusFor(value, query) {
  if (query.direction === "neutral" || query.warning === undefined || query.critical === undefined) return "unknown";
  if (query.direction === "higher") {
    if (value < query.critical) return "critical";
    if (value < query.warning) return "warning";
    return "healthy";
  }
  if (value >= query.critical) return "critical";
  if (value >= query.warning) return "warning";
  return "healthy";
}

export function parsePrometheusResponse(raw, code) {
  if (!raw || raw.status !== "success") throw new Error(`Prometheus query failed for ${code}.`);
  const resultType = raw.data?.resultType;
  const result = raw.data?.result;
  if (resultType === "scalar" && Array.isArray(result) && result.length === 2) {
    return { measuredAt: new Date(finiteNumber(result[0], `${code}.timestamp`) * 1000).toISOString(), value: finiteNumber(result[1], code) };
  }
  if (resultType !== "vector" || !Array.isArray(result) || result.length !== 1 || !Array.isArray(result[0]?.value)) {
    throw new Error(`${code} must return exactly one scalar or vector sample.`);
  }
  return {
    measuredAt: new Date(finiteNumber(result[0].value[0], `${code}.timestamp`) * 1000).toISOString(),
    value: finiteNumber(result[0].value[1], code)
  };
}

async function boundedJson(url, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const text = await response.text();
    if (Buffer.byteLength(text) > MAX_RESPONSE_BYTES) throw new Error(`Response from ${new URL(url).origin} is too large.`);
    if (!response.ok) throw new Error(`${new URL(url).origin} returned HTTP ${response.status}.`);
    return JSON.parse(text);
  } finally {
    clearTimeout(timeout);
  }
}

function renderPromql(query, environment, source) {
  const rendered = query.promql.replaceAll("{{environment}}", environment);
  if (source.scopeMode === "label_filtered" && !rendered.includes(source.requiredQueryMarker)) {
    throw new Error(`${environment}/${query.code} is missing required environment marker: ${source.requiredQueryMarker}.`);
  }
  return rendered;
}

export async function collectEnvironment(config, environment, env = process.env, requestJson = boundedJson) {
  const source = config.sources[environment];
  const headers = { Accept: "application/json", "User-Agent": "Arcigy-SaaS-Prometheus-Sync/1.0" };
  if (source.tokenEnv) headers.Authorization = `Bearer ${secret(env, source.tokenEnv)}`;
  const metrics = [];
  for (const query of config.queries) {
    const promql = renderPromql(query, environment, source);
    const url = `${source.prometheusUrl}/api/v1/query?query=${encodeURIComponent(promql)}`;
    const sample = parsePrometheusResponse(await requestJson(url, { headers }), query.code);
    const measuredMs = Date.parse(sample.measuredAt);
    if (measuredMs > Date.now() + 5 * 60_000) throw new Error(`${environment}/${query.code} has a future timestamp.`);
    metrics.push({
      code: query.code,
      value: sample.value,
      status: statusFor(sample.value, query),
      measured_at: sample.measuredAt,
      freshness_seconds: query.freshnessSeconds,
      external_key: `${environment}:prometheus:${query.code}:${sample.measuredAt}`
    });
  }
  return metrics;
}

async function postToOdoo(config, environment, metrics, env, requestJson = boundedJson) {
  const headers = {
    Authorization: `Bearer ${secret(env, config.odoo.apiKeyEnv)}`,
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "Arcigy-SaaS-Prometheus-Sync/1.0"
  };
  if (config.odoo.database) headers["X-Odoo-Database"] = config.odoo.database;
  return requestJson(`${config.odoo.url}/json/2/saas.metric.current/ingest_metric_batch`, {
    method: "POST",
    headers,
    body: JSON.stringify({ payload: { environment, source_updated_at: new Date().toISOString(), metrics } })
  });
}

export async function runPrometheusSync(
  rawConfig,
  { env = process.env, dryRun = false, environments = ENVIRONMENTS, requestJson = boundedJson } = {}
) {
  const config = validateConfig(rawConfig);
  const selected = selectedEnvironments(environments);
  const results = [];
  const errors = [];
  for (const environment of selected) {
    try {
      const metrics = await collectEnvironment(config, environment, env, requestJson);
      const odoo = dryRun ? undefined : await postToOdoo(config, environment, metrics, env, requestJson);
      results.push({ environment, metrics, odoo });
    } catch (error) {
      errors.push({ environment, message: error instanceof Error ? error.message : String(error) });
    }
  }
  if (errors.length) throw new AggregateError(errors.map((item) => new Error(`${item.environment}: ${item.message}`)), JSON.stringify(errors));
  return results;
}

async function main() {
  const configArg = process.argv.find((arg) => arg.startsWith("--config="));
  const environmentArg = process.argv.find((arg) => arg.startsWith("--environment="));
  if (!configArg) {
    throw new Error(
      "Usage: node saas_prometheus_sync.mjs --config=<path> [--dry-run] [--environment=develop|main|all]"
    );
  }
  const environment = environmentArg?.slice("--environment=".length) || "all";
  const environments = environment === "all" ? ENVIRONMENTS : [environment];
  const raw = JSON.parse(await readFile(configArg.slice("--config=".length), "utf8"));
  const results = await runPrometheusSync(raw, {
    dryRun: process.argv.includes("--dry-run"),
    environments
  });
  console.log(JSON.stringify({ ok: true, results: results.map((item) => ({ environment: item.environment, metrics: item.metrics.length })) }, null, 2));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
