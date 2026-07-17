import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(
  new URL("../addons/geotherm_chatbot/views/crm_lead_views.xml", import.meta.url),
  "utf8",
);

function recordBody(id, model) {
  const escapedId = id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const escapedModel = model.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = source.match(
    new RegExp(
      `<record\\s+id="${escapedId}"\\s+model="${escapedModel}">([\\s\\S]*?)<\\/record>`,
    ),
  );
  assert.ok(match, `Missing ${model} record ${id}`);
  return match[1];
}

const views = [
  ["kanban", "crm.crm_case_kanban_view_leads"],
  ["list", "crm.crm_case_tree_view_leads"],
  ["pivot", "crm.crm_lead_view_pivot"],
  ["graph", "crm.crm_lead_view_graph"],
];

test("Geotherm lead action disables sample data only in its own primary views", () => {
  for (const [mode, parent] of views) {
    const body = recordBody(`crm_lead_view_${mode}_geotherm`, "ir.ui.view");
    assert.match(body, new RegExp(`<field name="inherit_id" ref="${parent.replaceAll(".", "\\.")}"\\s*\\/>`));
    assert.match(body, /<field name="mode">primary<\/field>/);
    assert.match(body, new RegExp(`<xpath expr="\\/\\/${mode}" position="attributes">`));
    assert.match(body, /<attribute name="sample">0<\/attribute>/);
  }

  const inheritedViews = [...source.matchAll(/<record\s+id="[^"]+"\s+model="ir\.ui\.view">([\s\S]*?)<\/record>/g)];
  for (const match of inheritedViews) {
    if (/<attribute name="sample">0<\/attribute>/.test(match[1])) {
      assert.match(match[1], /<field name="mode">primary<\/field>/);
    }
  }
});

test("Geotherm lead action binds every sample-capable view and keeps the real-lead domain", () => {
  const action = recordBody("action_geotherm_crm_leads", "ir.actions.act_window");
  assert.match(action, /<field name="view_mode">kanban,list,form,calendar,pivot,graph,activity<\/field>/);
  assert.match(action, /\[\('geotherm_external_lead_id', '!=', False\)\]/);

  for (const [mode] of views) {
    const binding = recordBody(`action_geotherm_crm_leads_view_${mode}`, "ir.actions.act_window.view");
    assert.match(binding, new RegExp(`<field name="view_mode">${mode}<\\/field>`));
    assert.match(binding, new RegExp(`<field name="view_id" ref="crm_lead_view_${mode}_geotherm"\\s*\\/>`));
    assert.match(binding, /<field name="act_window_id" ref="action_geotherm_crm_leads"\s*\/>/);
  }
});
