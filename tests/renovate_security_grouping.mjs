// Reproduce the pinned upstream bug and exercise the guarded compatibility hook.
// Each planner runs in a fresh process: an ESM module already loaded by the
// negative control must never mask whether the production hook was installed.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const self = fileURLToPath(import.meta.url);
const fixtureMode = process.argv[2] === '--fixture';
const mode = fixtureMode ? process.argv[3] : undefined;
const installationArg = process.argv[fixtureMode ? 4 : 2];
assert.equal(process.argv.length, fixtureMode ? 5 : 3, 'Pass exactly one installed Renovate package directory');
assert.ok(isAbsolute(installationArg), 'Renovate installation must be absolute');
assert.ok(!installationArg.split(sep).some((part) => part === '.' || part === '..'), 'No installation traversal');
// Derive file reads from one of two fixed trusted installation locations,
// never from the caller-provided path after checking it.
let installation;
if (installationArg === '/usr/local/renovate') {
  installation = '/usr/local/renovate';
} else {
  assert.ok(process.env.RUNNER_TEMP && isAbsolute(process.env.RUNNER_TEMP), 'RUNNER_TEMP must be absolute');
  const runnerTemp = await realpath(process.env.RUNNER_TEMP);
  installation = resolve(runnerTemp, 'renovate/node_modules/renovate');
  assert.equal(installationArg, installation, 'Use the fixed RUNNER_TEMP installation');
}
assert.equal(await realpath(installation), installation, 'Installation must have a canonical path');
const targetRelative = 'dist/workers/repository/updates/generate.js';
const target = join(installation, targetRelative);
assert.equal(await realpath(target), target, 'Planner module must have a canonical path');
const originalSource = await readFile(target, 'utf8');
const hash = (value) => createHash('sha256').update(value).digest('hex');
const originalHash = hash(originalSource);
const marker = 'SECURITY_GROUPING_RESULT=';

if (!fixtureMode) {
  const results = {};
  for (const childMode of ['baseline', 'patched']) {
    // The proof may not inherit NODE_OPTIONS, NODE_PATH or an earlier loader.
    const env = { PATH: process.env.PATH ?? '', TZ: 'UTC', LANG: 'C.UTF-8' };
    if (process.env.RUNNER_TEMP) env.RUNNER_TEMP = process.env.RUNNER_TEMP;
    // The npm smoke job intentionally skips native install scripts; the official
    // Docker job leaves this unset and exercises its packaged native RE2.
    if (process.env.RENOVATE_X_IGNORE_RE2 === 'true') env.RENOVATE_X_IGNORE_RE2 = 'true';
    const child = spawnSync(process.execPath, [self, '--fixture', childMode, installation], {
      cwd: process.cwd(), env, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 120_000,
    });
    assert.equal(child.error, undefined, `${childMode}: ${child.error}`);
    assert.equal(child.status, 0, `${childMode}: ${child.stderr}\n${child.stdout}`);
    const lines = child.stdout.split('\n').filter((line) => line.startsWith(marker));
    assert.equal(lines.length, 1, `${childMode}: one fixture receipt required`);
    results[childMode] = JSON.parse(lines[0].slice(marker.length));
  }
  for (const [name, baseline] of Object.entries(results.baseline.cases)) {
    const patched = results.patched.cases[name];
    assert.ok(patched, `Missing patched fixture ${name}`);
    assert.equal(patched.upgradesHash, baseline.upgradesHash, `${name}: updater order/content changed`);
    if (name.startsWith('ordinary-')) {
      assert.equal(patched.fullHash, baseline.fullHash, `${name}: ordinary branch changed`);
      continue;
    }
    assert.equal(patched.isVulnerabilityAlert, true, `${name}: security classification lost`);
    assert.equal(patched.branchConfigLeader, patched.alert);
    assert.deepEqual(patched.schedule, []);
    assert.equal(patched.scheduledNow, true);
    assert.equal(patched.prCreation, 'immediate');
    assert.match(patched.prTitle, /\[SECURITY\]/);
    assert.equal(patched.calculatedPrLimit, 1);
    assert.equal(patched.ordinaryBudgetBlocks, false);
    assert.equal(patched.securityBudgetBlocks, true);
    assert.equal(patched.sortedFirst, true);
    assert.equal(patched.minimumGroupSize, name.startsWith('foss-') ? 2 : 1);
  }
  for (const name of ['ci-later', 'ci-runtime', 'ci-positioned', 'foss-cargo']) {
    assert.equal(results.baseline.cases[name].isVulnerabilityAlert, false, `${name}: negative control must reproduce upstream bug`);
    assert.equal(results.baseline.cases[name].ordinaryBudgetBlocks, true);
  }
  assert.equal(results.patched.manualApprovalPreserved, true);
  assert.equal(results.patched.automergeFalse, true);
  assert.equal(await readFile(target, 'utf8'), originalSource, 'Compatibility hook must not write installed sources');

  const { installSecurityCompat } = await import('../scripts/renovate-security-compat.mjs');
  await assert.rejects(async () => installSecurityCompat('relative/renovate'));
  await assert.rejects(async () => installSecurityCompat(installation + '/..'));
  const scratch = await mkdtemp(join(tmpdir(), 'renovate-security-negative-'));
  try {
    const fixturePackage = async (name, metadata, source) => {
      const root = join(scratch, name);
      await mkdir(dirname(join(root, targetRelative)), { recursive: true });
      await writeFile(join(root, 'package.json'), JSON.stringify(metadata));
      await writeFile(join(root, targetRelative), source);
      return root;
    };
    const wrongVersion = await fixturePackage('wrong-version', { name: 'renovate', version: '44.215.1' }, originalSource);
    const wrongName = await fixturePackage('wrong-name', { name: 'different-package', version: '44.215.0' }, originalSource);
    const wrongSource = await fixturePackage('wrong-source', { name: 'renovate', version: '44.215.0' }, originalSource + '\n// unexpected source\n');
    await assert.rejects(async () => installSecurityCompat(wrongVersion));
    await assert.rejects(async () => installSecurityCompat(wrongName));
    await assert.rejects(async () => installSecurityCompat(wrongSource));
    const escaping = await fixturePackage('escaping-module', { name: 'renovate', version: '44.215.0' }, originalSource);
    const outside = join(scratch, 'outside-generate.js');
    await writeFile(outside, originalSource);
    await rm(join(escaping, targetRelative));
    await symlink(outside, join(escaping, targetRelative));
    await assert.rejects(async () => installSecurityCompat(escaping));
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
  assert.equal(hash(await readFile(target, 'utf8')), originalHash);
  console.log(`Security branch compatibility passed: ${Object.keys(results.patched.cases).length} planner cases, manual guards and six invalid installations.`);
} else {
  assert.ok(['baseline', 'patched'].includes(mode));
  let compatibility;
  if (mode === 'patched') {
    const { installSecurityCompat } = await import('../scripts/renovate-security-compat.mjs');
    compatibility = installSecurityCompat(installation);
  }
  const allowed = new Set([
    'config/defaults', 'config/utils', 'modules/platform/api', 'modules/platform/index',
    'workers/repository/init/vulnerability', 'workers/repository/updates/branchify',
    'workers/global/limits', 'workers/repository/update/branch/schedule', 'workers/repository/process/sort',
  ]);
  const moduleAt = async (name) => {
    assert.ok(allowed.has(name), 'Only fixed planner modules may be imported');
    const filename = await realpath(join(installation, 'dist', name + '.js'));
    assert.ok(filename.startsWith(installation + sep + 'dist' + sep), 'Module escapes installation');
    return import(pathToFileURL(filename).href);
  };
  const { getConfig } = await moduleAt('config/defaults');
  const { mergeChildConfig } = await moduleAt('config/utils');
  const { branchifyUpgrades } = await moduleAt('workers/repository/updates/branchify');
  const { setCount, isLimitReached, calcLimit } = await moduleAt('workers/global/limits');
  const { isScheduledNow } = await moduleAt('workers/repository/update/branch/schedule');
  const { sortBranches } = await moduleAt('workers/repository/process/sort');
  if (mode === 'patched') assert.equal(compatibility.wasApplied(), true, 'Production hook must handle the actual planner import');
  const { default: apis } = await moduleAt('modules/platform/api');
  const { setPlatformApi } = await moduleAt('modules/platform/index');
  const { detectVulnerabilityAlerts } = await moduleAt('workers/repository/init/vulnerability');
  let alerts = [];
  apis.set('github', { getVulnerabilityAlerts: async () => alerts });
  setPlatformApi('github');
  let base = getConfig();
  for (const file of ['renovate-ci.json', 'renovate-fleet.json']) {
    base = mergeChildConfig(base, JSON.parse(await readFile(resolve(file), 'utf8')));
  }
  Object.assign(base, { baseBranch: 'main', semanticCommits: 'disabled', errors: [], warnings: [], repoIsOnboarded: true });
  const versioningByDatasource = { crate: 'cargo', npm: 'npm', 'github-tags': 'semver' };
  const dependency = (name, datasource = 'github-tags') => ({
    depName: name, packageName: name, currentValue: '2.12.0', currentVersion: '2.12.0', datasource,
    versioning: versioningByDatasource[datasource],
    updates: [{ newValue: '2.12.1', newVersion: '2.12.1', newMajor: 2, updateType: 'patch' }],
  });
  const ciFiles = { 'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: [dependency('actions/checkout'), dependency('zzz/security-action')] }] };
  const runtimeFiles = { 'github-actions': [{ packageFile: '.github/workflows/ci.yml', deps: [dependency('actions/checkout')] }], 'custom.regex': [{ packageFile: '.github/renovate-runtime.json', deps: [dependency('renovate', 'npm')] }] };
  const positioned = structuredClone(ciFiles);
  positioned['github-actions'][0].deps[0].fileReplacePosition = 200;
  positioned['github-actions'][0].deps[1].fileReplacePosition = 100;
  const scheduled = mergeChildConfig(base, { packageRules: [{ matchManagers: ['github-actions'], schedule: ['* * * 2 *'] }] });
  // Reproduce the cross-manager pair contract without importing another repo.
  const foss = mergeChildConfig(base, {
    packageRules: [{ matchPackageNames: ['@tauri-apps/plugin-updater', 'tauri-plugin-updater'], groupName: 'Tauri updater bindings', groupSlug: 'tauri-updater', minimumGroupSize: 2, separateMajorMinor: false }],
    vulnerabilityAlerts: { groupName: 'Tauri updater bindings', groupSlug: 'tauri-updater' },
  });
  const fossFiles = { npm: [{ packageFile: 'package.json', deps: [dependency('@tauri-apps/plugin-updater', 'npm')] }], cargo: [{ packageFile: 'src-tauri/Cargo.toml', deps: [dependency('tauri-plugin-updater', 'crate')] }] };
  const fixtures = [
    ['ordinary-ci', base, ciFiles], ['ordinary-scheduled-ci', scheduled, ciFiles], ['ordinary-foss', foss, fossFiles],
    ['ci-later', scheduled, ciFiles, 'actions', 'zzz/security-action'],
    ['ci-first', base, ciFiles, 'actions', 'actions/checkout'],
    ['ci-runtime', base, runtimeFiles, 'npm', 'renovate'],
    ['ci-positioned', base, positioned, 'actions', 'zzz/security-action'],
    ['foss-cargo', foss, fossFiles, 'rust', 'tauri-plugin-updater'],
    ['foss-npm', foss, fossFiles, 'npm', '@tauri-apps/plugin-updater'],
  ];
  const cases = {};
  // Run sequentially because each fixture replaces the shared mocked platform's alerts.
  for (const [name, config, files, ecosystem, depName] of fixtures) {
    alerts = depName ? [{ security_vulnerability: { package: { ecosystem, name: depName }, first_patched_version: { identifier: '2.12.1' }, severity: 'high' }, security_advisory: { ghsa_id: 'GHSA-test-test-test', identifiers: [], summary: 'Synthetic', description: 'Synthetic', references: [], severity: 'high' } }] : [];
    const planned = await branchifyUpgrades(await detectVulnerabilityAlerts(config), structuredClone(files));
    assert.equal(planned.branches.length, 1, `${name}: atomic branch lost`);
    const branch = planned.branches[0];
    assert.equal(branch.branchName, name.includes('foss') ? 'renovate/tauri-updater' : 'renovate/ci-workflows');
    for (const [key, count] of [['ConcurrentPRs', 9], ['Branches', 9], ['HourlyPRs', 9], ['VulnerabilityConcurrentPRs', 0], ['VulnerabilityBranches', 0]]) setCount(key, count);
    const ordinaryBudgetBlocks = isLimitReached('ConcurrentPRs', branch) || isLimitReached('Branches', branch);
    setCount('VulnerabilityConcurrentPRs', 1); setCount('VulnerabilityBranches', 1);
    const securityBudgetBlocks = isLimitReached('ConcurrentPRs', branch) && isLimitReached('Branches', branch);
    const sorted = [{ isVulnerabilityAlert: false, prPriority: 999, updateType: 'patch', prTitle: 'ordinary' }, branch];
    sortBranches(sorted);
    cases[name] = {
      alert: depName, fullHash: hash(JSON.stringify(planned)), upgradesHash: hash(JSON.stringify(branch.upgrades)),
      branchConfigLeader: branch.depName, isVulnerabilityAlert: branch.isVulnerabilityAlert ?? false,
      schedule: branch.schedule, scheduledNow: isScheduledNow(branch), prCreation: branch.prCreation,
      prTitle: branch.prTitle, calculatedPrLimit: calcLimit(branch.upgrades, 'prConcurrentLimit'),
      ordinaryBudgetBlocks, securityBudgetBlocks, sortedFirst: sorted[0] === branch, minimumGroupSize: branch.minimumGroupSize,
    };
  }
  alerts = [{ security_vulnerability: { package: { ecosystem: 'actions', name: 'zzz/security-action' }, first_patched_version: { identifier: '2.12.1' }, severity: 'high' }, security_advisory: { ghsa_id: 'GHSA-test-test-test', identifiers: [], summary: 'Synthetic', description: 'Synthetic', references: [], severity: 'high' } }];
  const manual = mergeChildConfig(base, { packageRules: [{ matchPackageNames: ['actions/checkout'], dependencyDashboardApproval: true, automerge: false }] });
  const branch = (await branchifyUpgrades(await detectVulnerabilityAlerts(manual), structuredClone(ciFiles))).branches[0];
  console.log(marker + JSON.stringify({ cases, manualApprovalPreserved: branch.dependencyDashboardApproval === true, automergeFalse: branch.automerge === false }));
}
