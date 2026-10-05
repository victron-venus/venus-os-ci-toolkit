// Exercise the actual pinned Renovate extractor and branch planner, without API writes.
import assert from 'node:assert/strict';
import { glob, mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const installation = resolve(process.argv[2]);
const runtime = JSON.parse(await readFile('.github/renovate-runtime.json', 'utf8'));
assert.equal(JSON.parse(await readFile(`${installation}/package.json`, 'utf8')).version, runtime.version);
const moduleAt = (name) => import(pathToFileURL(`${installation}/dist/${name}.js`));
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
assert.equal(pins.deps.length, 5);
for (const dep of pins.deps) {
  dep.updates = [{ updateType: 'digest', newValue: dep.currentValue, newDigest: next }];
}
const policyFile = '.release-policy.json';
const policyManager = preset.customManagers.find((manager) => manager.datasourceTemplate === 'git-refs');
assert.deepEqual(policyManager.managerFilePatterns, ['/^\\.release-policy\\.json$/']);
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
  for (const content of [
    policyContent,
    JSON.stringify({ ...policy, coverage: { reports: policy.coverage.reports, toolkit_ref: old } }),
  ]) {
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
assert.equal(updates.length, 13);
assert.deepEqual([...new Set(updates.map((update) => update.branchName))], ['renovate/ci-workflows']);
assert.ok(updates.every((update) => update.automerge === false));
for await (const filename of glob(['.github/workflows/*.{yml,yaml}', 'actions/**/action.{yml,yaml}'])) {
  const actual = await extractPackageFile(await readFile(filename, 'utf8'), filename, config);
  for (const dep of actual?.deps ?? []) {
    assert.notEqual(dep.skipReason, 'unversioned-reference', `${filename}: ${dep.depName} needs a version comment`);
  }
}
console.log('Renovate groups CodeQL majors, reusable workflow digests, action patches, generator pins and the coverage policy digest into one PR; the policy updater preserves its immutable SHA.');
