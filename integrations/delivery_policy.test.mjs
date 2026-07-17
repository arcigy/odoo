import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const workflow = readFileSync(
  resolve(repoRoot, '.github', 'workflows', 'deploy-caprover.yml'),
  'utf8',
);
const archiveScript = readFileSync(
  resolve(repoRoot, 'integrations', 'create_odoo_deploy_archive.sh'),
  'utf8',
);
const codeowners = readFileSync(resolve(repoRoot, '.github', 'CODEOWNERS'), 'utf8');

test('Odoo delivery policy keeps validation ahead of production deploys', () => {
  assert.match(workflow, /^  pull_request:\n    branches: \[main\]$/m);
  assert.match(workflow, /^  workflow_dispatch:$/m);
  assert.doesNotMatch(workflow, /pull_request_target/);
  assert.match(workflow, /^permissions:\n  contents: read$/m);

  const validateStart = workflow.indexOf('\n  validate:');
  const deployStart = workflow.indexOf('\n  deploy:');
  assert.ok(validateStart > -1 && deployStart > validateStart);
  const validateJob = workflow.slice(validateStart, deployStart);
  const deployJob = workflow.slice(deployStart);

  assert.match(validateJob, /name: Validate Odoo/);
  assert.match(validateJob, /python integrations\/validate_odoo_addons\.py/);
  assert.match(validateJob, /node --test integrations\/\*\.test\.mjs/);
  assert.match(validateJob, /--test-tags=\/arcigy_saas_control_center/);
  assert.match(validateJob, /grep -Eq '0 failed, 0 error\\\(s\\\) of \[1-9\]\[0-9\]\* tests'/);
  assert.doesNotMatch(validateJob, /\$\{\{\s*secrets\./);

  assert.match(deployJob, /needs: validate/);
  assert.match(
    deployJob,
    /if: github\.event_name == 'push' && github\.ref == 'refs\/heads\/main'/,
  );
  assert.match(deployJob, /CAPROVER_PASSWORD: \$\{\{ secrets\.CAPROVER_PASSWORD \}\}/);
  assert.doesNotMatch(workflow, /continue-on-error:\s*true/);

  const actionRefs = workflow
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.startsWith('- uses:'))
    .map((line) => line.slice('- uses:'.length).trim().split(/\s+#/, 1)[0]);
  assert.ok(actionRefs.length > 0);
  for (const actionRef of actionRefs) {
    const separator = actionRef.lastIndexOf('@');
    assert.ok(separator > 0, `Action is not pinned: ${actionRef}`);
    assert.match(actionRef.slice(separator + 1), /^[0-9a-f]{40}$/);
  }

  assert.equal((workflow.match(/persist-credentials: false/g) || []).length, 2);
  assert.equal(
    (workflow.match(/bash integrations\/create_odoo_deploy_archive\.sh/g) || []).length,
    2,
  );
});

test('immutable Odoo archive policy excludes environment files and requires all addons', () => {
  assert.match(archiveScript, /git -C "\$repo_root" archive --format=tar/);
  assert.match(archiveScript, /captain-definition/);
  assert.match(archiveScript, /addons\/geotherm_chatbot\/__manifest__\.py/);
  assert.match(archiveScript, /addons\/geotherm_drive\/__manifest__\.py/);
  assert.match(archiveScript, /addons\/arcigy_saas_control_center\/__manifest__\.py/);
  assert.match(archiveScript, /Deployment archive contains an environment file/);
});

test('critical Odoo delivery surfaces have an explicit Arcigy owner', () => {
  const rules = new Set(
    codeowners
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line && !line.startsWith('#')),
  );
  assert.deepEqual(
    rules,
    new Set(['* @arcigy', '/.github/ @arcigy', '/addons/ @arcigy', '/integrations/ @arcigy', '/ops/ @arcigy']),
  );
});
