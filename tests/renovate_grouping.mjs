// Exercise the actual pinned Renovate extractor and branch planner, without API writes.
import assert from 'node:assert/strict';
import { glob, mkdtemp, readFile, realpath, rm, writeFile } from 'node:fs/promises';
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
  'config/index',
  'modules/manager/index',
  'modules/datasource/common',
  'workers/repository/process/lookup/index',
  'util/package-rules/index',
  'modules/platform/api',
  'modules/platform/index',
  'workers/repository/init/vulnerability',
  'modules/manager/github-actions/extract',
  'modules/manager/pep621/extract',
  'modules/manager/pip-compile/extract',
  'workers/repository/process/fetch',
  'modules/manager/custom/regex/index',
  'modules/manager/custom/jsonata/index',
  'workers/repository/updates/flatten',
  'workers/repository/updates/branchify',
  'workers/repository/update/pr/labels',
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
const { prepareLabels, shouldUpdateLabels, getChangedLabels } = await moduleAt('workers/repository/update/pr/labels');
const { setPlatformApi } = await moduleAt('modules/platform/index');
setPlatformApi('github');
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
assert.ok(updates.every((update) => prepareLabels(update).includes('automerge')));
for await (const filename of glob(['.github/workflows/*.{yml,yaml}', 'actions/**/action.{yml,yaml}'])) {
  const actual = await extractPackageFile(await readFile(filename, 'utf8'), filename, config);
  for (const dep of actual?.deps ?? []) {
    assert.notEqual(dep.skipReason, 'unversioned-reference', `${filename}: ${dep.depName} needs a version comment`);
  }
}
console.log('Renovate groups CodeQL majors, reusable workflow digests, action patches, generator pins, release policy digests and toolkit runtime inputs into one PR; custom updaters preserve immutable SHAs.');

// Full-fleet ownership must keep the CI unit while exposing real uv/PEP 621 deps.
const fleetPreset = JSON.parse(await readFile('renovate-fleet.json', 'utf8'));
const fleetConfig = mergeChildConfig(config, fleetPreset);
const { extractPackageFile: extractPython } = await moduleAt('modules/manager/pep621/extract');
const python = await extractPython('[project]\nname = "migration-fixture"\nversion = "1.0.0"\nrequires-python = ">=3.11"\ndependencies = ["httpx==0.28.0"]\n', 'pyproject.toml', fleetConfig);
assert.ok(python.deps.some((dep) => dep.depName === 'httpx' || dep.packageName === 'httpx'));
const fleetUpdates = await flattenUpdates(fleetConfig, {
  'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: actions }],
  npm: [{ packageFile: 'package.json', deps: [{ depName: 'fixture-library', datasource: 'npm', currentValue: '1.0.0', updates: [{ updateType: 'patch', newValue: '1.0.1', newMajor: 1 }] }] }],
});
const ciUpdates = fleetUpdates.filter((update) => update.manager === 'github-actions');
const applicationUpdates = fleetUpdates.filter((update) => update.manager === 'npm');
assert.ok(ciUpdates.length > 0 && applicationUpdates.length > 0);
assert.ok(ciUpdates.every((update) => update.branchName === 'renovate/ci-workflows'));
assert.ok(applicationUpdates.every((update) => update.branchName.includes('application-dependencies')));
assert.ok(applicationUpdates.every((update) => update.branchName !== 'renovate/ci-workflows'));
assert.ok(fleetUpdates.every((update) => update.automerge === false));
assert.ok(fleetUpdates.every((update) => prepareLabels(update).includes('automerge')));
assert.equal(Object.hasOwn(fleetConfig.vulnerabilityAlerts, 'enabled'), false);
assert.equal(fleetConfig.vulnerabilityAlerts.dependencyDashboardApproval, false);
console.log('Full-fleet policy extracts Python application dependencies and keeps application updates separate from atomic CI updates.');

// The shared merger, not Renovate itself, owns approval and CI enforcement.
// Exercise actual grouped branch configuration, including application majors.
const { branchifyUpgrades } = await moduleAt('workers/repository/updates/branchify');
const allUpdateTypes = ['major', 'minor', 'patch', 'pin', 'digest'];
const applicationCases = allUpdateTypes.map((updateType, index) => ({
  depName: `fixture-${updateType}`, datasource: 'npm', currentValue: '1.0.0',
  updates: [{ updateType, newValue: updateType === 'major' ? '2.0.0' : `1.1.${index}`, newMajor: updateType === 'major' ? 2 : 1 }],
}));
const planned = await branchifyUpgrades({ ...fleetConfig, errors: [], warnings: [] }, {
  'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: actions }],
  npm: [{ packageFile: 'package.json', deps: applicationCases }],
});
assert.ok(planned.branches.some((branch) => branch.upgrades.some((update) => update.manager === 'npm' && update.updateType === 'major')));
for (const branch of planned.branches) {
  assert.ok(prepareLabels(branch).includes('automerge'), branch.branchName);
  assert.equal(branch.automerge, false, 'Only the independently reviewed shared workflow may merge');
}

// A policy change may label an untouched existing PR, but never undo a user's
// withdrawal or overwrite other manual edits. Missing historical metadata fails closed.
const configuredLabels = prepareLabels({ ...fleetConfig, labels: ['dependencies'] });
assert.deepEqual(configuredLabels, ['automerge', 'dependencies']);
assert.equal(shouldUpdateLabels(['dependencies'], ['dependencies'], configuredLabels), true);
assert.deepEqual(getChangedLabels(['dependencies'], configuredLabels), [['automerge'], []]);
assert.equal(shouldUpdateLabels(configuredLabels.slice(), ['dependencies'], configuredLabels.slice()), false);
assert.equal(shouldUpdateLabels(['dependencies'], ['dependencies', 'manual-hold'], configuredLabels.slice()), false);
assert.equal(shouldUpdateLabels(undefined, [], configuredLabels.slice()), false);
assert.equal(shouldUpdateLabels(configuredLabels.slice(), configuredLabels.slice(), configuredLabels.slice()), false);
console.log('All enabled update types opt into the shared merger; actual Renovate label reconciliation preserves manual withdrawal.');

// Lookup must propose a lock-only update even when the newer release already
// satisfies the manifest. Supplying a prebuilt update would miss this regression.
const { getManagerConfig } = await moduleAt('config/index');
const { getRangeStrategy } = await moduleAt('modules/manager/index');
const { getDatasourceFor } = await moduleAt('modules/datasource/common');
const { lookupUpdates } = await moduleAt('workers/repository/process/lookup/index');
const { applyPackageRules } = await moduleAt('util/package-rules/index');
// The same pyproject runtime field is also extracted through pip-compile.
// It must stop before release lookup while ordinary package updates stay enabled.
const { extractPackageFile: extractCompiledPython } = await moduleAt('modules/manager/pip-compile/extract');
const { fetchUpdates } = await moduleAt('workers/repository/process/fetch');
const runtimeSource = '[project]\nname = "runtime-policy-fixture"\nversion = "1.0.0"\nrequires-python = "==3.11.*"\ndependencies = ["httpx==0.28.0"]\n';
const pythonReleases = getDatasourceFor('python-version');
const originalPythonReleases = pythonReleases.getReleases;
pythonReleases.getReleases = async () => { throw new Error('A manual Python runtime must not request releases'); };
try {
  for (const [manager, extract] of [['pep621', extractPython], ['pip-compile', extractCompiledPython]]) {
    const managerConfig = getManagerConfig(fleetConfig, manager);
    const extracted = await extract(runtimeSource, 'pyproject.toml', managerConfig);
    const runtimeDependency = extracted.deps.find((dep) => dep.depType === 'requires-python');
    assert.ok(runtimeDependency, manager + ' must exercise the native runtime descriptor');
    const packages = { [manager]: [{ packageFile: 'pyproject.toml', deps: [{ ...runtimeDependency }] }] };
    await fetchUpdates(fleetConfig, packages);
    assert.equal(packages[manager][0].deps[0].skipReason, 'disabled', manager + ' runtime needs explicit review');
    assert.deepEqual(packages[manager][0].deps[0].updates, []);
    const application = extracted.deps.find((dep) => dep.depName === 'httpx' || dep.packageName === 'httpx');
    assert.ok(application);
    const effectiveApplication = await applyPackageRules({ ...managerConfig, ...application,
      manager, packageFile: 'pyproject.toml', depName: 'httpx', packageName: 'httpx' }, 'lookup');
    assert.notEqual(effectiveApplication.enabled, false, manager + ' package updates must remain enabled');
  }
} finally {
  pythonReleases.getReleases = originalPythonReleases;
}
console.log('Both native pyproject extractors keep Python runtime changes manual without disabling application updates.');

const lockScratch = await mkdtemp(resolve(tmpdir(), 'renovate-lockfiles-'));
const pypi = getDatasourceFor('pypi');
const originalReleases = pypi.getReleases;
const lockCases = [
  { name: 'fixture-wide', range: '>=1.0', versions: ['1.0.0', '1.1.0', '1.2.0'] },
  { name: 'fixture-bounded', range: '~=1.0', versions: ['1.0.0', '1.1.0', '1.2.0', '2.0.0'] },
  { name: 'fixture-exact', range: '==1.1.0', versions: ['1.1.0', '1.2.0'] },
  { name: 'fixture-without-lock', range: '>=1.0', versions: ['1.0.0', '1.1.0', '1.2.0'] },
];
try {
  GlobalConfig.set({ localDir: lockScratch, platform: 'github' });
  pypi.getReleases = async ({ packageName }) => {
    const fixture = lockCases.find((item) => item.name === packageName);
    assert.ok(fixture, 'Only synthetic package release metadata may be queried');
    return { releases: fixture.versions.map((version) => ({ version, releaseTimestamp: '2025-01-01T00:00:00.000Z' })) };
  };
  await writeFile(resolve(lockScratch, 'uv.lock'), 'version = 1\nrequires-python = ">=3.11"\n' +
    lockCases.filter((item) => item.name !== 'fixture-without-lock').map((item) =>
      `\n[[package]]\nname = "${item.name}"\nversion = "1.1.0"\nsource = { registry = "https://pypi.org/simple" }\n`).join(''));
  const manifest = '[project]\nname = "lock-fixture"\nversion = "1.0.0"\nrequires-python = ">=3.11"\ndependencies = [' +
    lockCases.map((item) => JSON.stringify(item.name + item.range)).join(', ') + ']\n';
  const managerConfig = getManagerConfig(fleetConfig, 'pep621');
  const extracted = await extractPython(manifest, 'pyproject.toml', managerConfig);
  for (const fixture of lockCases) {
    const dependency = extracted.deps.find((item) => item.depName === fixture.name);
    assert.ok(dependency);
    assert.equal(dependency.lockedVersion, fixture.name === 'fixture-without-lock' ? undefined : '1.1.0');
    const candidate = await applyPackageRules({ ...managerConfig, ...dependency,
      manager: 'pep621', packageFile: 'pyproject.toml', depName: fixture.name, packageName: fixture.name }, 'lookup');
    assert.equal(getRangeStrategy(candidate), 'update-lockfile');
    const actual = (await lookupUpdates(structuredClone(candidate))).unwrap();
    // Lookup applies package rules again per release; override the strategy there
    // too so the negative control really exercises the previous behavior.
    const baseline = (await lookupUpdates({ ...structuredClone(candidate), rangeStrategy: 'replace',
      packageRules: [...structuredClone(candidate.packageRules), { matchPackageNames: [fixture.name], rangeStrategy: 'replace' }],
    })).unwrap();
    assert.equal(actual.err, undefined);
    assert.equal(baseline.err, undefined);
    const updates = actual.val.updates;
    if (fixture.name === 'fixture-wide' || fixture.name === 'fixture-bounded') {
      const withinRange = updates.find((item) => item.newVersion === '1.2.0');
      assert.ok(withinRange, 'The declared range must not suppress a newer lockfile version');
      assert.equal(withinRange.newValue, fixture.range);
      assert.equal(withinRange.isLockfileUpdate, true);
      assert.ok(!baseline.val.updates.some((item) => item.newVersion === '1.2.0'), JSON.stringify({ fixture, baseline }));
    }
    assert.deepEqual(updates.filter((item) => !item.isLockfileUpdate), baseline.val.updates,
      'Exact pins, absent locks and out-of-range proposals keep replacement behavior');
  }
} finally {
  pypi.getReleases = originalReleases;
  GlobalConfig.set({ localDir: process.cwd(), platform: 'github' });
  await rm(lockScratch, { recursive: true, force: true });
}
console.log('Native Python lookup refreshes in-range locks without changing manifest bounds; exact and out-of-range updates retain replacement behavior.');

// Advisory rules force their own grouping; CI security fixes must stay atomic.
const { default: platformApis } = await moduleAt('modules/platform/api');
const { detectVulnerabilityAlerts } = await moduleAt('workers/repository/init/vulnerability');
const advisory = (ecosystem, name, fixed) => ({
  security_vulnerability: { package: { ecosystem, name }, first_patched_version: { identifier: fixed }, severity: 'high' },
  security_advisory: { ghsa_id: 'GHSA-test-test-test', identifiers: [], summary: 'Synthetic grouping test', description: 'Synthetic data only', references: [], severity: 'high' },
});
platformApis.set('github', { getVulnerabilityAlerts: async () => [
  advisory('actions', 'example/action', '1.1.0'), advisory('pip', 'example-package', '1.0.1'),
] });
setPlatformApi('github');
const vulnerableAction = {
  depName: 'example/action', packageName: 'example/action', datasource: 'github-tags', versioning: 'github-actions',
  currentValue: 'v1.0.0', currentVersion: '1.0.0', currentDigest: old,
  updates: [{ updateType: 'minor', newValue: 'v1.1.0', newVersion: '1.1.0', newDigest: next, newMajor: 1, newMinor: 1 }],
};
const securityPackages = {
  'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: [structuredClone(vulnerableAction), {
    ...structuredClone(vulnerableAction), depName: 'example/ordinary-action', packageName: 'example/ordinary-action',
    updates: [{ updateType: 'major', newValue: 'v2.0.0', newVersion: '2.0.0', newDigest: next, newMajor: 2, newMinor: 0 }],
  }] }],
  'custom.regex': [{ packageFile: '.github/action-pins.json', deps: [structuredClone(vulnerableAction)] }],
  'custom.jsonata': [{ packageFile: '.github/workflows/ci.yml', deps: [{
    depName: 'victron-venus/venus-os-ci-toolkit', packageName: 'https://github.com/victron-venus/venus-os-ci-toolkit',
    datasource: 'git-refs', currentValue: 'main', currentDigest: old,
    updates: [{ updateType: 'digest', newValue: 'main', newDigest: next }],
  }] }],
  pip_requirements: [{ packageFile: 'requirements.txt', deps: [{
    depName: 'example-package', packageName: 'example-package', datasource: 'pypi', versioning: 'pep440',
    currentValue: '1.0.0', currentVersion: '1.0.0',
    updates: [{ updateType: 'patch', newValue: '1.0.1', newVersion: '1.0.1', newMajor: 1, newMinor: 0 }],
  }] }],
};
const advisoryConfig = await detectVulnerabilityAlerts(fleetConfig);
assert.equal(advisoryConfig.packageRules.filter((rule) => rule.isVulnerabilityAlert).length, 2);
assert.ok(advisoryConfig.packageRules.filter((rule) => rule.isVulnerabilityAlert)
  .every((rule) => !Object.hasOwn(rule.force, 'enabled')));
const securityUpdates = await flattenUpdates(advisoryConfig, structuredClone(securityPackages));
assert.equal(securityUpdates.length, 5);
assert.equal(securityUpdates.filter((update) => update.isVulnerabilityAlert).length, 3);
assert.ok(securityUpdates.every((update) => prepareLabels(update).includes('automerge')));
assert.ok(securityUpdates.filter((update) => update.manager !== 'pip_requirements')
  .every((update) => update.branchName === 'renovate/ci-workflows'));
assert.ok(securityUpdates.filter((update) => update.manager === 'pip_requirements')
  .every((update) => update.branchName === 'renovate/pypi-example-package-vulnerability'));
console.log('Synthetic advisories preserve atomic CI grouping and separate application security fixes.');

// Security detection stays active without forcing disabled consumer dependencies on.
const heldConfig = mergeChildConfig(fleetConfig, { packageRules: [{
  matchPackageNames: ['example/action', 'example-package'], enabled: false,
}] });
const heldUpdates = await flattenUpdates(await detectVulnerabilityAlerts(heldConfig), structuredClone(securityPackages));
assert.equal(heldUpdates.length, 2);
assert.ok(heldUpdates.every((update) => !['example/action', 'example-package'].includes(update.depName)));
const ciSecurityPackages = Object.fromEntries(Object.entries(structuredClone(securityPackages))
  .filter(([manager]) => manager !== 'pip_requirements'));
const ciSecurityUpdates = await flattenUpdates(await detectVulnerabilityAlerts(config), ciSecurityPackages);
assert.equal(ciSecurityUpdates.length, 4);
assert.ok(ciSecurityUpdates.some((update) => update.isVulnerabilityAlert));
assert.ok(ciSecurityUpdates.every((update) => update.branchName === 'renovate/ci-workflows'));
console.log('Default security detection respects dependency holds and keeps the CI-only preset atomic.');
