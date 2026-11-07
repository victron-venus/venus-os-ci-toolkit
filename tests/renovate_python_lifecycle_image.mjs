// Credential-free, ephemeral official-image qualification. Never run in a live updater.
import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { createHash } from 'node:crypto';
import { isUtf8 } from 'node:buffer';
import { createServer } from 'node:http';
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  statfsSync,
  symlinkSync,
  writeFileSync,
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
const completion = '/config/renovate-completion-compat.mjs';
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
  completion,
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
const { init: initLogger, levels: loggerLevels } = await import(
  pathToFileURL('/usr/local/renovate/dist/logger/index.js')
);
await initLogger();
loggerLevels('stdout', 'debug');
memory.init();
const constraints = (version, uv = true, hashin = false) => [
  { toolName: 'python', constraint: version },
  ...(uv ? [{ toolName: 'uv', constraint: '0.13.4' }] : []),
  ...(hashin ? [{ toolName: 'hashin' }] : []),
];
async function install(version, uv = true, hashin = false) {
  const commands = await generateInstallCommands(
    constraints(version, uv, hashin),
    true,
  );
  assert.equal(commands.length, 2 + Number(uv) + Number(hashin));
  assert.equal(commands[0], `${node} ${helper} python-without-go ${version}`);
  runMeasuredPreparation(commands[0]);
  for (const command of commands.slice(1))
    run('/bin/bash', ['-e', '-c', command]);
  return commands;
}
function onlyPython(version) {
  const python = inventory().tools.find((t) => t.name === 'python');
  assert.deepEqual(python.versions, [{ version }]);
  assert.equal(python.version, version);
  assert(existsSync(`${toolRoot}/python/${version}/bin/python`));
}
async function installGo(version) {
  const commands = await generateInstallCommands(
    [{ toolName: 'golang', constraint: version }],
    true,
  );
  assert.deepEqual(commands, [
    `${node} ${helper} golang ${version}`,
    `install-tool golang ${version}`,
  ]);
  for (const command of commands) run('/bin/bash', ['-e', '-c', command]);
  const go = inventory().tools.find((tool) => tool.name === 'golang');
  assert.deepEqual(go.versions, [{ version }]);
  assert.equal(go.version, version);
  return commands;
}
receipt.initialInventory = inventory();
const goStarted = performance.now();
receipt.goInitial = await installGo('1.27.3');
receipt.goInitialMilliseconds = performance.now() - goStarted;
// Old -> new -> old covers real removal, leaf reinstallation and memo reuse.
receipt.initial = await install(A, true, true);
assertGoAbsent();
onlyPython(A);
const firstInode = statSync(`${toolRoot}/python/${A}`).ino;
await installGo('1.27.3');
const start = performance.now();
receipt.warm = await install(A, true, true);
receipt.warmMilliseconds = performance.now() - start;
assertGoAbsent();
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
receipt.new = await install(B, false);
onlyPython(B);
assert(!existsSync(`${toolRoot}/python/${A}`));
assert(!inventory().tools.some((t) => ['uv', 'hashin'].includes(t.name)));
receipt.back = await install(A, true, true);
onlyPython(A);
assert(!existsSync(`${toolRoot}/python/${B}`));
const uv = inventory().tools.find((t) => t.name === 'uv');
assert.deepEqual(uv.versions, [
  { version: '0.13.4', parent: { name: 'python', version: A } },
]);
receipt.uvReinstalledForCurrentParent = true;
const hashin = inventory().tools.find((t) => t.name === 'hashin');
assert.deepEqual(hashin.versions, [
  { version: '1.0.5', parent: { name: 'python', version: A } },
]);
receipt.hashinReinstalledForCurrentParent = true;
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
  // Exercise the real hashed-requirements manager after child removal/reinstall.
  // The loopback endpoint returns only synthetic release hashes, never a package.
  const fileName = 'requirements-hashin.txt';
  const oldHash = 'a'.repeat(64),
    newHash = 'b'.repeat(64);
  const beforeHashin = `lifecycle-fixture==2.0.0 --hash=sha256:${oldHash}\n`;
  writeFileSync(join(dir, fileName), beforeHashin);
  let requests = 0;
  const server = createServer((request, response) => {
    if (request.url !== '/pypi/lifecycle-fixture/json') {
      response.writeHead(404);
      response.end();
      return;
    }
    requests++;
    response.setHeader('Content-Type', 'application/json');
    response.end(
      JSON.stringify({
        info: { name: 'lifecycle-fixture' },
        releases: {
          '2.0.0': [
            {
              digests: { sha256: newHash },
              url: 'http://127.0.0.1/never-download',
            },
          ],
        },
      }),
    );
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  try {
    setCustomEnv({
      PATH: process.env.PATH,
      HOME: '/home/ubuntu',
      INDEX_URL: `http://127.0.0.1:${server.address().port}/`,
    });
    const { updateArtifacts: updateHashes } = await import(
      pathToFileURL(
        '/usr/local/renovate/dist/modules/manager/pip_requirements/artifacts.js',
      )
    );
    const hashes = await updateHashes({
      packageFileName: fileName,
      newPackageFileContent: beforeHashin,
      updatedDeps: [{ depName: 'lifecycle-fixture' }],
      config: { constraints: { python: A } },
    });
    assert(
      !(hashes ?? []).some((x) => x.artifactError),
      JSON.stringify(hashes),
    );
    assert.equal(requests, 1);
    const updated = readFileSync(join(dir, fileName), 'utf8');
    assert(updated.includes(newHash));
    assert(!updated.includes(oldHash));
    receipt.nativeHashinArtifactRegenerated = true;
  } finally {
    await new Promise((resolve, reject) =>
      server.close((error) => (error ? reject(error) : resolve())),
    );
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
// Fresh generic projects only: no private repository content or credentials.
// This tests native artifacts and lifecycle behavior, not the private disk quota.
receipt.goAndUv = await qualifyGoAndUv();
receipt.finalInventory = inventory();
console.log('PRIVATE_PYTHON_LIFECYCLE_RESULT=' + JSON.stringify(receipt));

async function qualifyGoAndUv() {
  const result = {};
  const started = performance.now();
  const before = canonicalInventory(inventory());
  const blocker = spawn(node, ['-e', 'setInterval(()=>{},1000)'], {
    stdio: 'ignore',
  });
  await once(blocker, 'spawn');
  try {
    const commands = await generateInstallCommands(
      [{ toolName: 'golang', constraint: '1.26.0' }],
      true,
    );
    const failed = spawnSync('/bin/bash', ['-e', '-c', commands[0]], {
      encoding: 'utf8',
      timeout: 30000,
    });
    assert.notEqual(failed.status, 0);
    assert.match(failed.stderr, /Unrelated process/);
    assert.equal(canonicalInventory(inventory()), before);
  } finally {
    blocker.kill('SIGTERM');
    await once(blocker, 'exit');
  }
  result.failedPlanRetry = await installGo('1.26.0');
  assert(!existsSync(`${toolRoot}/golang/1.27.3`));
  result.back = await installGo('1.27.3');
  assert(!existsSync(`${toolRoot}/golang/1.26.0`));
  const inode = statSync(`${toolRoot}/golang/1.27.3`).ino;
  const warmStarted = performance.now();
  result.warm = await installGo('1.27.3');
  result.warmMilliseconds = performance.now() - warmStarted;
  assert.equal(statSync(`${toolRoot}/golang/1.27.3`).ino, inode);
  result.transitionsMilliseconds = performance.now() - started;
  const root = realpathSync(
    mkdtempSync(join(temporaryRoot, 'renovate-go-uv-')),
  );
  try {
    const initialMod =
      'module example.invalid/lifecycle-fixture\n\ngo 1.26.0\n';
    const newMod = initialMod + '\nrequire github.com/google/uuid v1.6.0\n';
    writeFileSync(join(root, 'go.mod'), initialMod);
    // An empty tracked sum lets the actual manager return its newly populated file.
    writeFileSync(join(root, 'go.sum'), '\n');
    writeFileSync(
      join(root, 'fixture.go'),
      'package fixture\nimport _ "github.com/google/uuid"\n',
    );
    const specs = [
      {
        folder: 'python-one',
        project: 'lifecycle-one',
        name: 'idna',
        version: '3.10',
      },
      {
        folder: 'python-two',
        project: 'lifecycle-two',
        name: 'packaging',
        version: '25.0',
      },
    ];
    for (const spec of specs) {
      mkdirSync(join(root, spec.folder));
      const manifest = `[project]\nname = "${spec.project}"\nversion = "0.0.0"\nrequires-python = "==3.13.*"\ndependencies = []\n`;
      writeFileSync(join(root, spec.folder, 'pyproject.toml'), manifest);
      writeFileSync(
        join(root, spec.folder, 'uv.lock'),
        `version = 1\nrevision = 3\nrequires-python = "==3.13.*"\n\n[[package]]\nname = "${spec.project}"\nversion = "0.0.0"\nsource = { virtual = "." }\n`,
      );
    }
    mkdirSync(join(root, 'javascript'));
    writeFileSync(
      join(root, 'javascript/package.json'),
      JSON.stringify({
        name: 'lifecycle-npm-fixture',
        version: '0.0.0',
        private: true,
        dependencies: { 'is-number': '7.0.0' },
        scripts: { postinstall: 'node -e "process.exit(37)"' },
      }),
    );
    run('git', ['init', '-b', 'main'], { cwd: root });
    run('git', ['add', '.'], { cwd: root });
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
        'Inert native artifact fixtures',
      ],
      { cwd: root },
    );
    run('git', ['remote', 'add', 'origin', root], { cwd: root });
    const { GlobalConfig } = await import(
      pathToFileURL('/usr/local/renovate/dist/config/global.js')
    );
    const { setCustomEnv, setUserEnv } = await import(
      pathToFileURL('/usr/local/renovate/dist/util/env.js')
    );
    const { initRepo, syncGit } = await import(
      pathToFileURL('/usr/local/renovate/dist/util/git/index.js')
    );
    GlobalConfig.set({
      localDir: root,
      platform: 'github',
      binarySource: 'install',
      cacheDir: join(root, 'cache'),
      containerbaseDir: containerbaseTemporary,
      executionTimeout: 3,
      ignoreScripts: true,
      allowScripts: false,
    });
    setCustomEnv({
      PATH: process.env.PATH,
      HOME: '/home/ubuntu',
      UV_PYTHON_DOWNLOADS: 'never',
      UV_NO_BUILD: '1',
      GOTOOLCHAIN: 'local',
    });
    setUserEnv({});
    await initRepo({ url: root, defaultBranch: 'main', currentBranch: 'main' });
    await syncGit();
    const { updateArtifacts: updateGo } = await import(
      pathToFileURL(
        '/usr/local/renovate/dist/modules/manager/gomod/artifacts.js',
      )
    );
    const goStarted = performance.now();
    const go = await updateGo({
      packageFileName: 'go.mod',
      newPackageFileContent: newMod,
      updatedDeps: [
        { depName: 'github.com/google/uuid', newVersion: 'v1.6.0' },
      ],
      config: { updateType: 'minor' },
    });
    assert(
      !(go ?? []).some((entry) => entry.artifactError),
      JSON.stringify(go),
    );
    const sum = artifactText(
      go?.find((entry) => entry.file?.path === 'go.sum'),
      'go.sum',
    );
    assert.match(sum, /github\.com\/google\/uuid v1\.6\.0 h1:/);
    assert.deepEqual(
      inventory().tools.find((tool) => tool.name === 'golang').versions,
      [{ version: '1.26.0' }],
    );
    assert(!existsSync(`${toolRoot}/golang/1.27.3`));
    result.nativeGo = {
      sha256: createHash('sha256').update(sum).digest('hex'),
      milliseconds: performance.now() - goStarted,
    };
    const { updateArtifacts: updatePython } = await import(
      pathToFileURL(
        '/usr/local/renovate/dist/modules/manager/pep621/artifacts.js',
      )
    );
    result.nativeUv = [];
    for (const spec of specs) {
      // The second output exercises Go retirement with Python already installed.
      if (result.nativeUv.length) await installGo('1.26.0');
      const started = performance.now();
      const filename = `${spec.folder}/pyproject.toml`;
      const manifest = readFileSync(join(root, filename), 'utf8').replace(
        'dependencies = []',
        `dependencies = ["${spec.name}==${spec.version}"]`,
      );
      const updated = await updatePython({
        packageFileName: filename,
        newPackageFileContent: manifest,
        updatedDeps: [
          { depName: spec.name, packageName: spec.name, datasource: 'pypi' },
        ],
        config: { constraints: { python: '3.13.18', uv: '0.13.4' } },
      });
      assert(
        !(updated ?? []).some((entry) => entry.artifactError),
        JSON.stringify(updated),
      );
      const lockPath = `${spec.folder}/uv.lock`;
      const lock = artifactText(
        updated?.find((entry) => entry.file?.path === lockPath),
        lockPath,
      );
      assert(
        lock.includes(`name = "${spec.name}"\nversion = "${spec.version}"`),
      );
      assert.match(lock, /sha256:[a-f0-9]{64}/);
      onlyPython('3.13.18');
      assertGoAbsent();
      assert(!existsSync(`${toolRoot}/python/${A}`));
      result.nativeUv.push({
        name: spec.name,
        sha256: createHash('sha256').update(lock).digest('hex'),
        milliseconds: performance.now() - started,
      });
    }
    result.uvInventory = inventory().tools.find((tool) => tool.name === 'uv');
    assert.deepEqual(result.uvInventory.versions, [
      { version: '0.13.4', parent: { name: 'python', version: '3.13.18' } },
    ]);
    result.nativeNpm = await qualifyNpmAfterPython(root);
    assertGoAbsent();
    onlyPython('3.13.18');
    // Reuse the populated Go cache; do not prune it to make the test pass.
    writeFileSync(join(root, 'go.sum'), '\n');
    const repeated = await updateGo({
      packageFileName: 'go.mod',
      newPackageFileContent: newMod,
      updatedDeps: [
        { depName: 'github.com/google/uuid', newVersion: 'v1.6.0' },
      ],
      config: { updateType: 'minor' },
    });
    assert(
      !(repeated ?? []).some((entry) => entry.artifactError),
      JSON.stringify(repeated),
    );
    const repeatedSum = artifactText(
      repeated?.find((entry) => entry.file?.path === 'go.sum'),
      'go.sum',
    );
    assert.equal(repeatedSum, sum);
    assert.deepEqual(
      inventory().tools.find((tool) => tool.name === 'golang').versions,
      [{ version: '1.26.0' }],
    );
    onlyPython('3.13.18');
    result.nativeGoAfterPythonAndNpm = {
      sha256: createHash('sha256').update(repeatedSum).digest('hex'),
      reinstalled: true,
    };
    return result;
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function artifactText(artifact, expectedPath) {
  assert.equal(artifact?.file?.type, 'addition');
  assert.equal(artifact.file.path, expectedPath);
  const contents = artifact.file.contents;
  assert(
    typeof contents === 'string' || Buffer.isBuffer(contents),
    'Unsupported native artifact contents',
  );
  if (Buffer.isBuffer(contents)) {
    assert(isUtf8(contents), 'Native artifact is not valid UTF-8');
    return contents.toString('utf8');
  }
  return contents;
}

function assertGoAbsent() {
  assert(!inventory().tools.some((tool) => tool.name === 'golang'));
  for (const version of ['1.27.3', '1.26.0'])
    assert(!existsSync(`${toolRoot}/golang/${version}`));
}
async function qualifyNpmAfterPython(root) {
  const { generateLockFile } = await import(
    pathToFileURL(
      '/usr/local/renovate/dist/modules/manager/npm/post-update/npm.js',
    )
  );
  const started = performance.now();
  const result = await generateLockFile(
    'javascript',
    {
      NPM_CONFIG_CACHE: join(root, 'npm-cache'),
    },
    'package-lock.json',
    {
      constraints: { node: '24.22.2', npm: '12.2.2' },
      ignoreScripts: true,
    },
    [{ depName: 'is-number', newValue: '7.0.0' }],
  );
  assert.equal(result.error, false, result.stderr);
  assert.equal(typeof result.lockFile, 'string');
  const lock = JSON.parse(result.lockFile);
  assert.equal(lock.packages['node_modules/is-number'].version, '7.0.0');
  assert.match(lock.packages['node_modules/is-number'].integrity, /^sha512-/);
  assert(!existsSync(join(root, 'javascript/node_modules')));
  return {
    sha256: createHash('sha256').update(result.lockFile).digest('hex'),
    milliseconds: performance.now() - started,
    guarded: true,
  };
}

function runMeasuredPreparation(command) {
  const before = inventory();
  const go = before.tools.find((tool) => tool.name === 'golang');
  if (!go) {
    run('/bin/bash', ['-e', '-c', command]);
    return;
  }
  const goRoot = toolRoot + '/golang';
  assert.equal(realpathSync(goRoot), goRoot);
  const allocated = run('/usr/bin/du', ['-sk', goRoot]).match(/^(\d+)\s/);
  assert(allocated, 'Expected bounded Go tree allocation measurement');
  const beforeFs = statfsSync(toolRoot);
  const output = run('/bin/bash', ['-e', '-c', command]);
  const afterFs = statfsSync(toolRoot);
  const removed = JSON.parse(output).privatePythonLifecycle.removed;
  assertGoAbsent();
  receipt.goRetirementMeasurements ??= [];
  receipt.goRetirementMeasurements.push({
    versions: go.versions.map((v) => v.version),
    retiredTreeAllocatedBytes: Number(allocated[1]) * 1024,
    freeBytesBefore: beforeFs.bavail * beforeFs.bsize,
    freeBytesAfter: afterFs.bavail * afterFs.bsize,
    freeInodesBefore: beforeFs.ffree,
    freeInodesAfter: afterFs.ffree,
    removed,
    onlyGoRemoved: removed.every((step) => step.name === 'golang'),
    limitation:
      'Hosted filesystem, not the private 3 GiB quota; tree allocation and observed free-space delta are distinct measurements.',
  });
}
