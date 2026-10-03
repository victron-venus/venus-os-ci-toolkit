// Exercise the actual pinned Renovate extractor and branch planner, without API writes.
import assert from 'node:assert/strict';
import { glob, readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

const installation = resolve(process.argv[2]);
const moduleAt = (name) => import(pathToFileURL(`${installation}/dist/${name}.js`));
const { getConfig } = await moduleAt('config/defaults');
const { GlobalConfig } = await moduleAt('config/global');
GlobalConfig.set({ localDir: process.cwd(), platform: 'github' });
const { mergeChildConfig } = await moduleAt('config/utils');
const { extractPackageFile } = await moduleAt('modules/manager/github-actions/extract');
const { extractPackageFile: extractPins } = await moduleAt('modules/manager/custom/regex/index');
const { flattenUpdates } = await moduleAt('workers/repository/updates/flatten');
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
assert.equal(pins.deps.length, 4);
for (const dep of pins.deps) {
  dep.updates = [{ updateType: 'digest', newValue: dep.currentValue, newDigest: next }];
}
const updates = await flattenUpdates(config, {
  'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: actions }],
  'custom.regex': [{ packageFile: '.github/action-pins.json', deps: pins.deps }],
});
assert.equal(updates.length, 11);
assert.deepEqual([...new Set(updates.map((update) => update.branchName))], ['renovate/ci-workflows']);
assert.ok(updates.every((update) => update.automerge === false));
for await (const filename of glob(['.github/workflows/*.{yml,yaml}', 'actions/**/action.{yml,yaml}'])) {
  const actual = await extractPackageFile(await readFile(filename, 'utf8'), filename, config);
  for (const dep of actual?.deps ?? []) {
    assert.notEqual(dep.skipReason, 'unversioned-reference', `${filename}: ${dep.depName} needs a version comment`);
  }
}
console.log('Renovate groups CodeQL majors, reusable workflow digests, action patches and generator pins into one PR.');
