import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import test from "node:test";

async function pythonFiles(path) {
  const entries = await readdir(path, { withFileTypes: true });
  const nested = await Promise.all(entries.map(async (entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, path);
    if (entry.isDirectory()) return pythonFiles(child);
    return entry.name.endsWith(".py") ? [child] : [];
  }));
  return nested.flat();
}

test("custom addons use the Odoo 19 constraint descriptor", async () => {
  const addons = new URL("../addons/", import.meta.url);
  const legacy = [];
  for (const path of await pythonFiles(addons)) {
    const source = await readFile(path, "utf8");
    if (/^\s*_sql_constraints\s*=/m.test(source)) legacy.push(path.pathname);
  }
  assert.deepEqual(legacy, []);

  const driveModel = await readFile(new URL("../addons/geotherm_drive/models/drive_file.py", import.meta.url), "utf8");
  assert.match(driveModel, /_attachment_unique\s*=\s*models\.Constraint\(/);
  assert.match(driveModel, /UNIQUE\(attachment_id\)/);
});
