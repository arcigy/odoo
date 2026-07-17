#!/usr/bin/env python3
"""Fail-closed static validation for the canonical Geotherm Odoo package."""

from __future__ import annotations

import ast
import csv
from pathlib import Path
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[1]
ADDONS = ROOT / "addons"
CONTROL_CENTER = ADDONS / "arcigy_saas_control_center"
HUMAN_GROUPS = {
    "group_saas_executive",
    "group_saas_finance",
    "group_saas_customer_success",
    "group_saas_support",
    "group_saas_engineering",
    "group_saas_security",
    "group_saas_administrator",
}
BOT_GROUP = "group_saas_integration_bot"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def parse_xml_files() -> dict[Path, ElementTree.Element]:
    parsed = {}
    for path in sorted(ADDONS.glob("**/*.xml")):
        parsed[path] = ElementTree.parse(path).getroot()
    require(parsed, "No addon XML files were found.")
    return parsed


def validate_manifests() -> int:
    manifests = sorted(ADDONS.glob("*/__manifest__.py"))
    require(manifests, "No addon manifests were found.")
    for path in manifests:
        manifest = ast.literal_eval(path.read_text(encoding="utf-8"))
        require(manifest.get("installable") is True, f"{path} is not installable.")
        require(manifest.get("version"), f"{path} has no version.")
        for relative_path in manifest.get("data", []):
            require(
                (path.parent / relative_path).is_file(),
                f"{path} references missing data file {relative_path}.",
            )
    return len(manifests)


def read_acl() -> tuple[list[dict[str, str]], dict[str, set[str]]]:
    path = CONTROL_CENTER / "security" / "ir.model.access.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    require(rows, "Control Center ACL is empty.")
    required_headers = {
        "id",
        "name",
        "model_id:id",
        "group_id:id",
        "perm_read",
        "perm_write",
        "perm_create",
        "perm_unlink",
    }
    require(set(rows[0]) == required_headers, "Control Center ACL headers changed.")
    ids = [row["id"] for row in rows]
    require(len(ids) == len(set(ids)), "Control Center ACL contains duplicate IDs.")
    valid_permission_values = {"0", "1"}
    readers: dict[str, set[str]] = {}
    for row in rows:
        permissions = {
            row["perm_read"],
            row["perm_write"],
            row["perm_create"],
            row["perm_unlink"],
        }
        require(
            permissions <= valid_permission_values,
            f"Invalid ACL permission value in {row['id']}.",
        )
        if row["perm_read"] == "1":
            readers.setdefault(row["model_id:id"], set()).add(row["group_id:id"])
        if row["group_id:id"] == BOT_GROUP:
            require(row["perm_read"] == "1", f"Bot cannot read {row['model_id:id']}.")
            require(
                row["perm_write"] == row["perm_create"] == row["perm_unlink"] == "0",
                f"Bot has direct mutation access in {row['id']}.",
            )
    return rows, readers


def validate_groups(xml_files: dict[Path, ElementTree.Element]) -> None:
    security_path = CONTROL_CENTER / "security" / "saas_security.xml"
    root = xml_files[security_path]
    group_ids = {
        record.attrib["id"]
        for record in root.findall(".//record[@model='res.groups']")
    }
    expected = HUMAN_GROUPS | {BOT_GROUP}
    require(expected <= group_ids, f"Missing SaaS groups: {sorted(expected - group_ids)}")


def validate_menu_acl_congruence(
    xml_files: dict[Path, ElementTree.Element], readers: dict[str, set[str]]
) -> int:
    action_models: dict[str, str] = {}
    for root in xml_files.values():
        for record in root.findall(".//record[@model='ir.actions.act_window']"):
            field = record.find("./field[@name='res_model']")
            if field is not None and field.text:
                action_models[record.attrib["id"]] = field.text.strip()

    menu_path = CONTROL_CENTER / "views" / "saas_menu_views.xml"
    menus = xml_files[menu_path].findall(".//menuitem")
    checked = 0
    for menu in menus:
        groups = set(filter(None, menu.attrib.get("groups", "").split(",")))
        action = menu.attrib.get("action")
        if not groups or action not in action_models:
            continue
        model_external_id = "model_" + action_models[action].replace(".", "_")
        missing = groups - readers.get(model_external_id, set())
        require(
            not missing,
            f"{menu.attrib['id']} declares roles without read ACL: {sorted(missing)}",
        )
        checked += 1

    root_menu = next(
        menu for menu in menus if menu.attrib.get("id") == "menu_saas_control_center_root"
    )
    root_groups = set(root_menu.attrib.get("groups", "").split(","))
    require(root_groups == HUMAN_GROUPS, "Control Center root must expose exactly human roles.")
    require(BOT_GROUP not in root_groups, "Integration Bot must not receive the UI root.")
    return checked


def validate_seed_catalogue(xml_files: dict[Path, ElementTree.Element]) -> tuple[int, int]:
    metric_path = CONTROL_CENTER / "data" / "saas.metric.definition.csv"
    with metric_path.open(encoding="utf-8", newline="") as handle:
        metrics = list(csv.DictReader(handle))
    require(len(metrics) == 376, f"Expected 376 metrics, found {len(metrics)}.")
    codes = [row["code"] for row in metrics]
    require(len(codes) == len(set(codes)), "Metric catalogue contains duplicate codes.")

    dashboard_path = CONTROL_CENTER / "data" / "saas_dashboard_data.xml"
    dashboards = xml_files[dashboard_path].findall(
        ".//record[@model='saas.dashboard']"
    )
    require(len(dashboards) == 24, f"Expected 24 dashboards, found {len(dashboards)}.")
    return len(metrics), len(dashboards)


def main() -> None:
    xml_files = parse_xml_files()
    manifest_count = validate_manifests()
    acl_rows, readers = read_acl()
    validate_groups(xml_files)
    menu_checks = validate_menu_acl_congruence(xml_files, readers)
    metric_count, dashboard_count = validate_seed_catalogue(xml_files)
    print(
        "validation=passed "
        f"manifests={manifest_count} xml_files={len(xml_files)} acl_rows={len(acl_rows)} "
        f"menu_acl_checks={menu_checks} metrics={metric_count} dashboards={dashboard_count}"
    )


if __name__ == "__main__":
    main()
