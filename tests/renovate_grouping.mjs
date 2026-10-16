// Exercise the actual pinned Renovate extractor and branch planner, without API writes.
import assert from 'node:assert/strict';
import { glob, mkdtemp, readFile, realpath, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { isAbsolute, resolve, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

assert.equal(process.argv.length, 3, 'Pass exactly one installed Renovate package directory');
assert.ok(isAbsolute(process.argv[2]), 'Renovate installation must be an absolute path');
assert.ok(!process.argv[2].split(sep).some((part) => part === '..' || part === '.'), 'No path traversal');
assert.ok(process.env.RUNNER_TEMP && isAbsolute(process.env.RUNNER_TEMP), 'RUNNER_TEMP must be an absolute directory');
const runnerTemp = await realpath(process.env.RUNNER_TEMP);
const installation = resolve(runnerTemp, 'renovate/node_modules/renovate');
assert.equal(await realpath(process.argv[2]), installation, 'Renovate must use the fixed RUNNER_TEMP installation');
const runtime = JSON.parse(await readFile('.github/renovate-runtime.json', 'utf8'));
const packageFile = await realpath(resolve(installation, 'package.json'));
if (!packageFile.startsWith(installation + sep)) throw new Error('Renovate package metadata escapes installation');
const installed = JSON.parse(await readFile(packageFile, 'utf8'));
assert.equal(installed.name, 'renovate');
assert.equal(installed.version, runtime.version);
const allowedModules = new Set([
  'config/defaults',
  'config/global',
  'config/utils',
  'modules/manager/github-actions/extract',
  'modules/manager/custom/regex/index',
  'modules/manager/custom/jsonata/index',
  'workers/repository/updates/flatten',
  'workers/repository/update/branch/auto-replace',
]);
async function moduleAt(name) {
  assert.ok(allowedModules.has(name), 'Only fixed Renovate test modules may be loaded');
  const file = await realpath(resolve(installation, 'dist', name + '.js'));
  if (!file.startsWith(installation + sep + 'dist' + sep)) throw new Error('Renovate module escapes installation');
  return import(pathToFileURL(file).href);
}
await assert.rejects(moduleAt('../package.json'), /Only fixed Renovate test modules/);
await assert.rejects(moduleAt('config/../../outside'), /Only fixed Renovate test modules/);
const { getConfig } = await moduleAt('config/defaults');
const { GlobalConfig } = await moduleAt('config/global');
GlobalConfig.set({ localDir: process.cwd(), platform: 'github' });
const { mergeChildConfig } = await moduleAt('config/utils');
const { extractPackageFile } = await moduleAt('modules/manager/github-actions/extract');
const { extractPackageFile: extractPins } = await moduleAt('modules/manager/custom/regex/index');
const { extractPackageFile: extractRuntime } = await moduleAt('modules/manager/custom/jsonata/index');
const { flattenUpdates } = await moduleAt('workers/repository/updates/flatten');
const { doAutoReplace } = await moduleAt('workers/repository/update/branch/auto-replace');
const preset = JSON.parse(await readFile('renovate-ci.json', 'utf8'));
const config = mergeChildConfig(getConfig(), {
  ...preset,
  semanticCommits: 'disabled',
  baseBranch: 'main',
});
const old = 'a'.repeat(40);
const next = 'b'.repeat(40);
const workflow = `name: Coupled actions
on: push
jobs:
  codeql:
    runs-on: ubuntu-latest
    steps:
      - uses: github/codeql-action/init@${old} # v3.28.0
      - uses: github/codeql-action/autobuild@${old} # v3.28.0
      - uses: github/codeql-action/analyze@${old} # v3.28.0
      - uses: actions/checkout@${old} # v4.2.0
      - uses: actions/upload-artifact@v4.6.2
  ci:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/python-ci.yml@${old} # main
  release:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/release.yml@${old} # main
`;
const extracted = await extractPackageFile(workflow, '.github/workflows/ci.yml', config);
const actions = extracted.deps.filter((dep) => dep.depType !== 'github-runner');
assert.equal(actions.length, 7);
assert.equal(actions.filter((dep) => dep.depName === 'github/codeql-action').length, 3);
assert.ok(actions.every((dep) => !dep.skipReason));
assert.equal(actions.filter((dep) => dep.depType === 'workflow').length, 2);
function actionUpdate(dep) {
  let updateType = 'patch';
  let newValue = 'v4.2.1';
  if (dep.depName === 'github/codeql-action') {
    updateType = 'major';
    newValue = 'v4.38.2';
  } else if (dep.depName === 'victron-venus/venus-os-ci-toolkit') {
    updateType = 'digest';
    newValue = 'main';
  }
  if (!dep.currentDigest) updateType = 'pinDigest';
  return { updateType, newValue, newDigest: next, newMajor: 4 };
}
for (const dep of actions) {
  dep.updates = [actionUpdate(dep)];
}
const manifest = await readFile('.github/action-pins.json', 'utf8');
const pins = extractPins(manifest, '.github/action-pins.json', preset.customManagers[0]);
assert.deepEqual(pins.deps.map((dep) => ({
  packageName: dep.depName,
  version: dep.currentValue,
  digest: dep.currentDigest,
})), JSON.parse(manifest));
assert.ok(pins.deps.some((dep) => dep.depName === 'step-security/harden-runner'));
for (const dep of pins.deps) {
  dep.updates = [{ updateType: 'digest', newValue: dep.currentValue, newDigest: next }];
}
const policyFile = '.release-policy.json';
const policyManager = preset.customManagers.find((manager) => manager.datasourceTemplate === 'git-refs');
assert.deepEqual(policyManager.managerFilePatterns, [String.raw`/^\.release-policy\.json$/`]);
const policy = {
  repo: 'example/consumer',
  coverage: { toolkit_ref: old, reports: [{ name: 'python', path: 'coverage.xml' }] },
  unrelated_ref: old,
};
const policyContent = JSON.stringify(policy, null, 2) + '\n';
const policyPins = extractPins(policyContent, policyFile, policyManager);
assert.equal(policyPins.deps.length, 1);
const [policyDep] = policyPins.deps;
assert.equal(policyDep.depName, 'victron-venus/venus-os-ci-toolkit');
assert.equal(policyDep.packageName, 'https://github.com/victron-venus/venus-os-ci-toolkit');
assert.equal(policyDep.datasource, 'git-refs');
assert.equal(policyDep.currentValue, 'main');
assert.equal(policyDep.currentDigest, old);
assert.ok(!policyDep.skipReason);
policyDep.updates = [{ updateType: 'digest', newValue: 'main', newDigest: next }];

// The actual updater writes files: confine it to a disposable fixture directory.
const scratch = await mkdtemp(resolve(tmpdir(), 'renovate-policy-'));
try {
  GlobalConfig.set({ localDir: scratch, platform: 'github' });
  async function assertPolicyUpdate(content) {
    const extractedPolicy = extractPins(content, policyFile, policyManager);
    const updated = await doAutoReplace({
      ...config,
      ...extractedPolicy,
      ...extractedPolicy.deps[0],
      manager: 'regex',
      packageFile: policyFile,
      depIndex: 0,
      newValue: 'main',
      newDigest: next,
      updateType: 'digest',
    }, content, false);
    assert.deepEqual(JSON.parse(updated), { ...policy, coverage: { ...policy.coverage, toolkit_ref: next } });
    assert.equal(extractPins(updated, policyFile, policyManager).deps[0].currentDigest, next);
    assert.equal(await readFile(resolve(scratch, policyFile), 'utf8'), updated);
  }
  // Both cases mutate the same file and GlobalConfig; deliberately run in sequence.
  await assertPolicyUpdate(policyContent);
  await assertPolicyUpdate(JSON.stringify({ ...policy, coverage: { reports: policy.coverage.reports, toolkit_ref: old } }));
} finally {
  GlobalConfig.set({ localDir: process.cwd(), platform: 'github' });
  await rm(scratch, { recursive: true, force: true });
}
for (const content of [
  '{}',
  JSON.stringify({ unrelated_ref: old }),
  JSON.stringify({ coverage: { toolkit_ref: 'main' } }),
  JSON.stringify({ coverage: { toolkit_ref: old.slice(1) } }),
  JSON.stringify({ coverage: { toolkit_ref: old + 'a' } }),
]) {
  assert.equal(extractPins(content, policyFile, policyManager), null);
}
// Parse YAML structurally: foreign inputs and run-block examples are not dependencies.
const runtimeManager = preset.customManagers.find((manager) => manager.customType === 'jsonata');
assert.deepEqual(runtimeManager.managerFilePatterns, [String.raw`/^\.github/workflows/[^/]+\.ya?ml$/`]);
const runtimeFile = '.github/workflows/release-pipeline.yml';
const runtimeWorkflow = `jobs:
  foreign:
    uses: another/team/.github/workflows/fuzz.yml@${old}
    with:
      toolkit-ref: ${old}
  examples:
    runs-on: ubuntu-latest
    steps:
      - run: |
          uses: victron-venus/venus-os-ci-toolkit/.github/workflows/fuzz.yml@${old}
          with:
            toolkit-ref: ${old}
  fuzz:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/fuzz.yml@${old}
    with:
      toolkit-ref: ${old} # runtime one
      unrelated-ref: ${old}
  coverage:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/coverage-upload.yml@${old}
    with: {toolkit-ref: "${old}"} # runtime two
`;
const runtimePins = await extractRuntime(runtimeWorkflow, runtimeFile, runtimeManager);
assert.equal(runtimePins.deps.length, 2);
for (const dep of runtimePins.deps) {
  assert.equal(dep.currentDigest, old);
  assert.equal(dep.depName, policyDep.depName);
  assert.equal(dep.datasource, policyDep.datasource);
  assert.equal(dep.currentValue, 'main');
  dep.updates = [{ updateType: 'digest', newValue: 'main', newDigest: next }];
}
const runtimeScratch = await mkdtemp(resolve(tmpdir(), 'renovate-runtime-'));
try {
  GlobalConfig.set({ localDir: runtimeScratch, platform: 'github' });
  for (const content of [runtimeWorkflow, runtimeWorkflow.replaceAll('\n', '\r\n'), runtimeWorkflow.trimEnd()]) {
    let updated = content;
    for (let depIndex = 0; depIndex < 2; depIndex += 1) {
      const result = await extractRuntime(updated, runtimeFile, runtimeManager);
      updated = await doAutoReplace({
        ...config, ...result, ...result.deps[depIndex], manager: 'jsonata',
        packageFile: runtimeFile, depIndex, newValue: 'main', newDigest: next,
        updateType: 'digest',
      }, updated, false);
    }
    const expected = content.replace(`toolkit-ref: ${old} # runtime one`, `toolkit-ref: ${next} # runtime one`)
      .replace(`toolkit-ref: "${old}"`, `toolkit-ref: "${next}"`);
    assert.equal(updated, expected);
    const after = await extractRuntime(updated, runtimeFile, runtimeManager);
    assert.ok(after.deps.every((dep) => dep.currentDigest === next));
  }
} finally {
  GlobalConfig.set({ localDir: process.cwd(), platform: 'github' });
  await rm(runtimeScratch, { recursive: true, force: true });
}
for (const content of [
  runtimeWorkflow.replaceAll(old, 'main'),
  runtimeWorkflow.replaceAll(old, old + 'a'),
  runtimeWorkflow.replaceAll(old, old.slice(1)),
  runtimeWorkflow.replaceAll('victron-venus/venus-os-ci-toolkit/', 'another/team/'),
  'jobs: {caller: {with: {toolkit-ref: "' + old + '"}}}',
]) assert.equal(await extractRuntime(content, runtimeFile, runtimeManager), null);
const updates = await flattenUpdates(config, {
  'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: actions }],
  regex: [
    { packageFile: '.github/action-pins.json', deps: pins.deps },
    { packageFile: policyFile, deps: policyPins.deps },
  ],
  jsonata: [{ packageFile: runtimeFile, deps: runtimePins.deps }],
});
assert.equal(updates.length, actions.length + pins.deps.length + policyPins.deps.length + runtimePins.deps.length);
assert.deepEqual([...new Set(updates.map((update) => update.branchName))], ['renovate/ci-workflows']);
assert.ok(updates.every((update) => update.automerge === false));
for await (const filename of glob(['.github/workflows/*.{yml,yaml}', 'actions/**/action.{yml,yaml}'])) {
  const actual = await extractPackageFile(await readFile(filename, 'utf8'), filename, config);
  for (const dep of actual?.deps ?? []) {
    assert.notEqual(dep.skipReason, 'unversioned-reference', `${filename}: ${dep.depName} needs a version comment`);
  }
}
console.log('Renovate groups CodeQL majors, reusable workflow digests, action patches, generator pins, release policy digests and toolkit runtime inputs into one PR; custom updaters preserve immutable SHAs.');
