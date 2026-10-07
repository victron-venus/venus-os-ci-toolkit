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
for (const dep of actions) {
  const codeql = dep.depName === 'github/codeql-action';
  const toolkit = dep.depName === 'victron-venus/venus-os-ci-toolkit';
  dep.updates = [{
    updateType: !dep.currentDigest ? 'pinDigest' : codeql ? 'major' : toolkit ? 'digest' : 'patch',
    newValue: codeql ? 'v4.38.2' : toolkit ? 'main' : 'v4.2.1',
    newDigest: next,
    newMajor: 4,
  }];
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
const updates = await flattenUpdates(config, {
  'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: actions }],
  regex: [
    { packageFile: '.github/action-pins.json', deps: pins.deps },
    { packageFile: policyFile, deps: policyPins.deps },
  ],
});
assert.equal(updates.length, actions.length + pins.deps.length + policyPins.deps.length);
assert.deepEqual([...new Set(updates.map((update) => update.branchName))], ['renovate/ci-workflows']);
assert.ok(updates.every((update) => update.automerge === false));
for await (const filename of glob(['.github/workflows/*.{yml,yaml}', 'actions/**/action.{yml,yaml}'])) {
  const actual = await extractPackageFile(await readFile(filename, 'utf8'), filename, config);
  for (const dep of actual?.deps ?? []) {
    assert.notEqual(dep.skipReason, 'unversioned-reference', `${filename}: ${dep.depName} needs a version comment`);
  }
}
console.log('Renovate groups CodeQL majors, reusable workflow digests, action patches, generator pins and the coverage policy digest into one PR; the policy updater preserves its immutable SHA.');
