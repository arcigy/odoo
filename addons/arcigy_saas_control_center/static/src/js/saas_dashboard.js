/** @odoo-module **/

import { Component, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { standardActionServiceProps } from "@web/webclient/actions/action_service";

export class ArcigySaasDashboard extends Component {
    static template = "arcigy_saas_control_center.Dashboard";
    static props = { ...standardActionServiceProps };

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            loading: true,
            error: null,
            generatedAt: null,
            freshnessSummary: {
                develop: { status: "missing", presentMetricCount: 0, expectedMetricCount: 0 },
                main: { status: "missing", presentMetricCount: 0, expectedMetricCount: 0 },
            },
            sections: [],
            selectedDashboard: "founder",
            filterOptions: {
                dashboards: [], services: [], regions: [], releases: [], tenants: [], plans: [],
                features: [], integrations: [], countries: [], currencies: [],
            },
            filters: {
                period: "24h",
                compare_previous: true,
                service_id: "", region_id: "", release_id: "", tenant_id: "",
                plan_id: "", feature_id: "", integration_id: "", country_id: "",
                currency_id: "", tenant_size_band: "", endpoint_group: "",
                job_type: "", acquisition_source: "", browser: "",
                operating_system: "", device: "", model_code: "", incident_severity: "", status: "",
            },
        });
        onWillStart(async () => {
            await this.loadFilterOptions();
            await this.refresh();
        });
        this.refreshTimer = setInterval(() => this.refresh(), 60000);
        onWillUnmount(() => clearInterval(this.refreshTimer));
    }

    get visibleSections() {
        if (!this.state.selectedDashboard) {
            return this.state.sections;
        }
        return this.state.sections.filter((section) => section.code === this.state.selectedDashboard);
    }

    async refresh() {
        try {
            const payload = await this.orm.call("saas.metric.current", "dashboard_payload", [], {
                dashboard_code: this.state.selectedDashboard || null,
                scope_key: "global",
                filters: this.cleanFilters(),
            });
            this.state.sections = payload.sections;
            this.state.generatedAt = payload.generatedAt;
            this.state.freshnessSummary = payload.freshnessSummary;
            this.state.error = null;
        } catch (error) {
            this.state.error = error.message || String(error);
        } finally {
            this.state.loading = false;
        }
    }

    async selectDashboard(event) {
        this.state.selectedDashboard = event.target.value;
        await this.refresh();
    }

    async loadFilterOptions() {
        try {
            this.state.filterOptions = await this.orm.call(
                "saas.metric.current", "dashboard_filter_options", [], {}
            );
        } catch (error) {
            this.state.error = error.message || String(error);
        }
    }

    cleanFilters() {
        return Object.fromEntries(
            Object.entries(this.state.filters).filter(([, value]) => value !== "" && value !== null)
        );
    }

    async resetFilters() {
        for (const fieldName of Object.keys(this.state.filters)) {
            this.state.filters[fieldName] = fieldName === "period"
                ? "24h"
                : fieldName === "compare_previous"
                    ? true
                    : "";
        }
        await this.refresh();
    }

    kpis(section) {
        return section.rows.slice(0, 8);
    }

    freshnessFor(environment) {
        return this.state.freshnessSummary[environment] || {
            status: "missing", presentMetricCount: 0, expectedMetricCount: 0,
        };
    }

    freshnessLabel(environment) {
        const status = this.freshnessFor(environment).status;
        return { fresh: "FRESH", delayed: "DELAYED", stale: "STALE", missing: "NO DATA" }[status]
            || "UNKNOWN";
    }

    freshnessClass(environment) {
        return `o_arcigy_saas_freshness_card o_arcigy_saas_freshness_card--${this.freshnessFor(environment).status}`;
    }

    cadenceFor(environment) {
        const seconds = this.freshnessFor(environment).expectedRefreshSeconds;
        if (!seconds) return "—";
        if (seconds % 86400 === 0) return `${seconds / 86400} d`;
        if (seconds % 3600 === 0) return `${seconds / 3600} h`;
        if (seconds % 60 === 0) return `${seconds / 60} min`;
        return `${seconds} s`;
    }

    valueFor(row, environment) {
        const point = row[environment];
        if (!point) {
            return "—";
        }
        const value = point.value;
        if (row.unit === "%") {
            return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value)} %`;
        }
        if (row.unit === "EUR") {
            return new Intl.NumberFormat(undefined, { style: "currency", currency: "EUR" }).format(value);
        }
        if (row.unit === "bytes") {
            const units = ["B", "KB", "MB", "GB", "TB"];
            let normalized = Math.max(value, 0);
            let index = 0;
            while (normalized >= 1024 && index < units.length - 1) {
                normalized /= 1024;
                index += 1;
            }
            return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(normalized)} ${units[index]}`;
        }
        return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value)} ${row.unit}`.trim();
    }

    pointClass(row, environment) {
        const point = row[environment];
        return point ? `o_arcigy_saas_point o_arcigy_saas_point--${point.status}` : "o_arcigy_saas_point o_arcigy_saas_point--missing";
    }

    metadata(row, environment) {
        const point = row[environment];
        if (!point) {
            return "No data";
        }
        const parts = [point.freshness.toUpperCase(), point.measuredAt];
        if (point.release) {
            parts.push(`release ${point.release}`);
        }
        if (point.alerts && point.alerts.count) {
            parts.push(`${point.alerts.count} open alert(s), highest ${point.alerts.severity.toUpperCase()}`);
        }
        if (point.denominator) {
            parts.push(`n=${point.denominator}`);
        } else if (point.sampleCount) {
            parts.push(`samples=${point.sampleCount}`);
        }
        return parts.join(" · ");
    }

    thresholdMetadata(row) {
        const parts = [];
        if (row.target !== false && row.target !== null) parts.push(`target ${row.target}`);
        if (row.warning !== false && row.warning !== null) parts.push(`warning ${row.warning}`);
        if (row.critical !== false && row.critical !== null) parts.push(`critical ${row.critical}`);
        return parts.join(" · ");
    }

    comparisonMetadata(row, environment) {
        const comparison = row[environment] && row[environment].comparison;
        if (!comparison) return "No previous-period comparison";
        const delta = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3, signDisplay: "always" })
            .format(comparison.delta);
        const percent = comparison.percent === null
            ? ""
            : ` (${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2, signDisplay: "always" }).format(comparison.percent)} %)`;
        return `${delta}${percent} vs previous ${comparison.period}`;
    }

    sparklinePoints(row, environment) {
        const points = (row[environment] && row[environment].trend) || [];
        if (points.length < 2) return "";
        const values = points.map((point) => point.value);
        const minimum = Math.min(...values);
        const maximum = Math.max(...values);
        const span = maximum - minimum || 1;
        return values.map((value, index) => {
            const x = index / (values.length - 1) * 100;
            const y = 28 - ((value - minimum) / span * 24 + 2);
            return `${x.toFixed(2)},${y.toFixed(2)}`;
        }).join(" ");
    }
}

registry.category("actions").add("arcigy_saas_control_center.dashboard", ArcigySaasDashboard);
