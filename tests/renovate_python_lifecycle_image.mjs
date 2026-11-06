// Credential-free, ephemeral official-image qualification. Never run in a live updater.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import {
  existsSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
} from 'node:fs';
import { performance } from 'node:perf_hooks';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';
assert.equal(process.argv.length, 2);
assert.equal(process.platform, 'linux');
assert.equal(realpathSync('/usr/local/renovate'), '/usr/local/renovate');
assert.equal(realpathSync('/config'), '/config');
const cli = '/usr/local/containerbase/bin/containerbase-cli';
const node = '/usr/local/renovate/node';
const helper = '/config/prepare-renovate-python.mjs';
const lifecycle = '/config/renovate-python-lifecycle-compat.mjs';
const security = '/config/renovate-security-compat.mjs';
const A = '3.12.16',
  B = '3.15.2';
const toolRoot = '/opt/containerbase/tools';
const temporaryRoot = realpathSync(tmpdir());
assert.equal(temporaryRoot, '/tmp');
const containerbaseTemporary = join(temporaryRoot, 'containerbase');
const run = (program, args, options = {}) => {
  const r = spawnSync(program, args, {
    encoding: 'utf8',
    timeout: 180000,
    maxBuffer: 4 * 1024 * 1024,
    ...options,
  });
  assert.equal(r.error, undefined, String(r.error));
  assert.equal(r.status, 0, `${program}: ${r.stderr}\n${r.stdout}`);
  return r.stdout;
};
const inventory = () => JSON.parse(run(cli, ['list', 'tools', '--json']));
// Match the private launcher's conditional official initialization.
if (!existsSync(containerbaseTemporary)) run(cli, ['init', 'tool', 'all']);
const receipt = {};
// Exercise the exact production import order at the real CLI entry point.
const startup = run(node, [
  '--use-openssl-ca',
  '--import',
  lifecycle,
  '--import',
  security,
  '/usr/local/renovate/dist/renovate.js',
  '--version',
]);
for (const marker of [
  'Verified Renovate 44.215.0 private Python tool lifecycle',
  'Verified Renovate 44.215.0 security branch compatibility',
  'Verified Renovate 44.215.0 Python build boundary',
  'Verified Renovate 44.215.0 npm registry bundle compatibility',
])
  assert.equal(
    startup.split('\n').filter((x) => x === marker).length,
    1,
    marker,
  );
receipt.combinedCliPreload = true;
const { installPythonLifecycleCompat } = await import(pathToFileURL(lifecycle));
const guard = installPythonLifecycleCompat('/usr/local/renovate');
await guard.preload();
const other = await import(pathToFileURL(security));
const guards = [
  other.installSecurityCompat('/usr/local/renovate'),
  other.installPythonBuildCompat('/usr/local/renovate'),
  other.installNpmCompat('/usr/local/renovate'),
];
await import(
  pathToFileURL(
    '/usr/local/renovate/dist/workers/repository/updates/generate.js',
  )
);
await guards[1].preload();
await guards[2].preload();
assert(guard.wasApplied() && guards.every((x) => x.wasApplied()));
const { canonicalInventory } = await import(
  pathToFileURL('/config/prepare-renovate-python.mjs')
);
const { generateInstallCommands } = await import(
  pathToFileURL('/usr/local/renovate/dist/util/exec/containerbase.js')
);
const memory = await import(
  pathToFileURL('/usr/local/renovate/dist/util/cache/memory/index.js')
);
memory.init();
const constraints = (version, uv = true) => [
  { toolName: 'python', constraint: version },
  ...(uv ? [{ toolName: 'uv', constraint: '0.13.4' }] : []),
];
async function install(version, uv = true) {
  const commands = await generateInstallCommands(
    constraints(version, uv),
    true,
  );
  assert.equal(commands.length, uv ? 3 : 2);
  assert.equal(commands[0], `${node} ${helper} ${version}`);
  for (const command of commands) run('/bin/bash', ['-e', '-c', command]);
  return commands;
}
function onlyPython(version) {
  const python = inventory().tools.find((t) => t.name === 'python');
  assert.deepEqual(python.versions, [{ version }]);
  assert.equal(python.version, version);
  assert(existsSync(`${toolRoot}/python/${version}/bin/python`));
}
receipt.initialInventory = inventory();
// Old -> new -> old covers real removal, leaf reinstallation and memo reuse.
receipt.initial = await install(A);
onlyPython(A);
const firstInode = statSync(`${toolRoot}/python/${A}`).ino;
const start = performance.now();
receipt.warm = await install(A);
receipt.warmMilliseconds = performance.now() - start;
assert.equal(
  statSync(`${toolRoot}/python/${A}`).ino,
  firstInode,
  'Warm install must reuse its directory',
);
const beforeNegative = canonicalInventory(inventory());
const orphan = spawn(node, ['-e', 'setInterval(()=>{},1000)'], {
  stdio: 'ignore',
});
await once(orphan, 'spawn');
try {
  const failedCommands = await generateInstallCommands(constraints(B), true);
  assert.equal(failedCommands.length, 3);
  const result = spawnSync('/bin/bash', ['-e', '-c', failedCommands[0]], {
    encoding: 'utf8',
    timeout: 30000,
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Unrelated process/);
  assert.equal(canonicalInventory(inventory()), beforeNegative);
} finally {
  orphan.kill('SIGTERM');
  await once(orphan, 'exit');
}
receipt.unrelatedProcessRejected = true;
// A redirected version path is rejected before official uninstall can run.
const path = `${toolRoot}/python/${A}`,
  saved = `${toolRoot}/python/${A}.qualification`;
renameSync(path, saved);
symlinkSync(saved, path);
try {
  const result = spawnSync(node, [helper, B], {
    encoding: 'utf8',
    timeout: 30000,
  });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Redirected tool path/);
  assert.equal(canonicalInventory(inventory()), beforeNegative);
  assert(existsSync(saved));
} finally {
  rmSync(path);
  renameSync(saved, path);
}
receipt.redirectedPathRejected = true;
receipt.new = await install(B);
onlyPython(B);
assert(!existsSync(`${toolRoot}/python/${A}`));
receipt.back = await install(A);
onlyPython(A);
assert(!existsSync(`${toolRoot}/python/${B}`));
const uv = inventory().tools.find((t) => t.name === 'uv');
assert.deepEqual(uv.versions, [
  { version: '0.13.4', parent: { name: 'python', version: A } },
]);
receipt.uvReinstalledForCurrentParent = true;
receipt.failedNativePlanRetryReinstalled = true;
// A real unsupported leaf must stop the complete plan before removing UV.
run(cli, ['install', 'tool', 'pip-tools', '7.5.1']);
const unknown = canonicalInventory(inventory());
const rejected = spawnSync(node, [helper, B], {
  encoding: 'utf8',
  timeout: 30000,
});
assert.notEqual(rejected.status, 0);
assert.match(rejected.stderr, /Unknown Python child/);
assert.equal(canonicalInventory(inventory()), unknown);
run(cli, ['uninstall', 'tool', 'pip-tools', '7.5.1']);
receipt.unknownChildRejected = true;
// Actual manager execution after A -> B -> A, with a local inert wheel only.
const scratch = realpathSync(
  mkdtempSync(join(temporaryRoot, 'renovate-lifecycle-')),
);
const dir = join(scratch, 'fixture');
try {
  run('python', ['/repo/tests/renovate_python_fixtures.py', dir, 'wheel']);
  run('git', ['init', '-b', 'main'], { cwd: dir });
  run('git', ['add', '.'], { cwd: dir });
  run(
    'git',
    [
      '-c',
      'user.name=Fixture',
      '-c',
      'user.email=fixture@example.invalid',
      '-c',
      'commit.gpgsign=false',
      'commit',
      '-m',
      'Inert fixture',
    ],
    { cwd: dir },
  );
  run('git', ['remote', 'add', 'origin', dir], { cwd: dir });
  const { GlobalConfig } = await import(
    pathToFileURL('/usr/local/renovate/dist/config/global.js')
  );
  const { setCustomEnv, setUserEnv } = await import(
    pathToFileURL('/usr/local/renovate/dist/util/env.js')
  );
  const { initRepo, syncGit } = await import(
    pathToFileURL('/usr/local/renovate/dist/util/git/index.js')
  );
  const { updateArtifacts } = await import(
    pathToFileURL(
      '/usr/local/renovate/dist/modules/manager/pip-compile/artifacts.js',
    )
  );
  GlobalConfig.set({
    localDir: dir,
    platform: 'github',
    binarySource: 'install',
    cacheDir: join(dir, 'cache'),
    containerbaseDir: containerbaseTemporary,
    executionTimeout: 3,
    ignoreScripts: true,
    allowScripts: false,
  });
  setCustomEnv({
    PATH: process.env.PATH,
    HOME: '/home/ubuntu',
    UV_PYTHON_DOWNLOADS: 'never',
    UV_OFFLINE: '1',
    UV_NO_BUILD: '1',
    PIP_NO_INDEX: '1',
  });
  setUserEnv({});
  await initRepo({ url: dir, defaultBranch: 'main', currentBranch: 'main' });
  await syncGit();
  const result = await updateArtifacts({
    packageFileName: 'requirements.in',
    newPackageFileContent: readFileSync(join(dir, 'requirements.in'), 'utf8'),
    updatedDeps: [],
    config: {
      lockFiles: ['requirements.txt'],
      constraints: { python: A, uv: '0.13.4' },
    },
  });
  assert(!(result ?? []).some((x) => x.artifactError), JSON.stringify(result));
  const output = readFileSync(join(dir, 'requirements.txt'), 'utf8');
  assert.match(output, /--hash=sha256:/);
  assert.match(output, /--no-build/);
  receipt.nativeArtifactRegenerated = true;
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
receipt.finalInventory = inventory();
console.log('PRIVATE_PYTHON_LIFECYCLE_RESULT=' + JSON.stringify(receipt));
