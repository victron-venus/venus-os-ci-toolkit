// Inert native-manager security regression. Intended for the official image
// with --network none, its Python interpreter, and pinned uv on PATH.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { registerHooks } from 'node:module';
import { dirname, isAbsolute, join, resolve, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const self = fileURLToPath(import.meta.url);
const fixtureMode = process.argv[2] === '--fixture';
const mode = fixtureMode ? process.argv[3] : undefined;
const installationArg = process.argv[fixtureMode ? 4 : 2];
assert.equal(process.argv.length, fixtureMode ? 5 : 3);
assert.ok(isAbsolute(installationArg), 'Use an absolute, trusted installation');
assert.ok(!installationArg.split(sep).some((part) => part === '.' || part === '..'));
let installation;
if (installationArg === '/usr/local/renovate') {
  installation = '/usr/local/renovate';
} else {
  assert.ok(process.env.RUNNER_TEMP && isAbsolute(process.env.RUNNER_TEMP));
  installation = resolve(realpathSync(process.env.RUNNER_TEMP), 'renovate/node_modules/renovate');
  assert.equal(installationArg, installation, 'Use the fixed RUNNER_TEMP installation');
}
assert.equal(realpathSync(installation), installation);
const metadata = JSON.parse(readFileSync(join(installation, 'package.json'), 'utf8'));
assert.equal(metadata.name, 'renovate');
assert.equal(metadata.version, '44.215.0');
const marker = 'PYTHON_BUILD_BOUNDARY_RESULT=';
const modules = [
  'dist/modules/manager/pip-compile/artifacts.js',
  'dist/modules/manager/pip-compile/common.js',
  'dist/modules/manager/pep621/processors/uv.js',
  'dist/workers/repository/update/branch/execute-post-upgrade-commands.js',
];
const hashes = () => Object.fromEntries(modules.map((file) => [file,
  createHash('sha256').update(readFileSync(join(installation, file))).digest('hex')]));

if (!fixtureMode) {
  const before = hashes();
  const results = {};
  for (const childMode of ['baseline', 'patched']) {
    const env = { PATH: process.env.PATH ?? '', LANG: 'C.UTF-8', TZ: 'UTC' };
    if (process.env.RUNNER_TEMP) env.RUNNER_TEMP = process.env.RUNNER_TEMP;
    if (process.env.RENOVATE_X_IGNORE_RE2 === 'true') env.RENOVATE_X_IGNORE_RE2 = 'true';
    const child = spawnSync(process.execPath, [self, '--fixture', childMode, installation], {
      env, encoding: 'utf8', timeout: 120_000, maxBuffer: 4 * 1024 * 1024,
    });
    assert.equal(child.error, undefined, String(child.error));
    assert.equal(child.status, 0, `${childMode}: ${child.stderr}\n${child.stdout}`);
    const lines = child.stdout.split('\n').filter((line) => line.startsWith(marker));
    assert.equal(lines.length, 1);
    results[childMode] = JSON.parse(lines[0].slice(marker.length));
  }
  assert.equal(results.baseline.sdist.backendExecuted, true, 'Reproduce the dependency build risk');
  assert.equal(results.baseline['dynamic-project'].backendExecuted, true, 'Reproduce first-party metadata risk');
  for (const name of ['sdist', 'dynamic-project', 'compile-project']) {
    assert.equal(results.patched[name].backendExecuted, false, name);
    assert.ok(results.patched[name].artifactErrors > 0, `${name} must fail closed`);
  }
  for (const name of ['wheel', 'static-project', 'nested-static-parent']) {
    assert.equal(results.patched[name].backendExecuted, false, name);
    assert.equal(results.patched[name].artifactErrors, 0, name);
  }
  for (const prefix of ['post-', 'post-nested-']) {
    const dynamic = prefix + 'dynamic-project';
    const staticProject = prefix + 'static-project';
    assert.equal(results.baseline[dynamic].backendExecuted, true, 'Reproduce export metadata execution despite prior artifact error');
    assert.equal(results.baseline[dynamic].outputCreated, true);
    assert.equal(results.patched[dynamic].backendExecuted, false);
    assert.equal(results.patched[dynamic].outputCreated, false);
    assert.equal(results.patched[dynamic].artifactErrors, 2);
    assert.match(results.patched[dynamic].diagnostics[1], /manual qualification: dynamic project metadata/);
    assert.equal(results.patched[staticProject].backendExecuted, false);
    assert.equal(results.patched[staticProject].outputCreated, true);
    assert.equal(results.patched[staticProject].artifactErrors, 1, 'Preserve the prior error without adding an export error');
  }
  assert.equal(results.patched['nested-static-parent'].childBackendExecuted, false);
  assert.equal(results.patched['nested-dynamic-parent'].backendExecuted, false);
  assert.equal(results.patched['nested-dynamic-parent'].childBackendExecuted, false);
  assert.ok(results.patched['nested-dynamic-parent'].artifactErrors > 0);
  assert.match(results.patched['nested-dynamic-parent'].diagnostics[0], /manual qualification:/);
  assert.equal(results.patched['post-static-project'].alternateLocationsRejected, true);
  assert.equal(results.patched.qualifiedToolConstraints, true, 'All native and post commands select qualified UV and preserve Python');
  assert.equal(results.patched.wheel.hashGenerated, true);
  assert.equal(results.patched.wheel.repeatHeaderRoundTrip, true);
  assert.deepEqual(hashes(), before, 'Installed package files must remain unchanged');
  const { installPythonBuildCompat } = await import('../scripts/renovate-security-compat.mjs');
  const invalid = mkdtempSync(join(tmpdir(), 'renovate-python-invalid-'));
  try {
    const fixture = (name) => {
      const root = join(invalid, name);
      mkdirSync(root);
      writeFileSync(join(root, 'package.json'), JSON.stringify(metadata));
      for (const file of modules) {
        mkdirSync(dirname(join(root, file)), { recursive: true });
        writeFileSync(join(root, file), readFileSync(join(installation, file)));
      }
      return root;
    };
    for (const [index, file] of modules.entries()) {
      const root = fixture(`changed-${index}`);
      writeFileSync(join(root, file), readFileSync(join(root, file), 'utf8') + '\n');
      assert.throws(() => installPythonBuildCompat(root), /Unreviewed Renovate Python module/);
    }
    const redirected = fixture('redirected');
    rmSync(join(redirected, modules[0]));
    symlinkSync(join(installation, modules[0]), join(redirected, modules[0]));
    assert.throws(() => installPythonBuildCompat(redirected), /symlinks/);
    const version = fixture('version');
    writeFileSync(join(version, 'package.json'), JSON.stringify({ ...metadata, version: '44.215.1' }));
    assert.throws(() => installPythonBuildCompat(version), /Unreviewed Renovate version/);
  } finally {
    rmSync(invalid, { recursive: true, force: true });
  }
  console.log(marker + JSON.stringify(results));
} else {
  assert.ok(['baseline', 'patched'].includes(mode));
  // Observe the real execution boundary without replacing or suppressing commands.
  globalThis.__renovateFixtureToolSelections = [];
  const execModuleUrl = pathToFileURL(join(installation, 'dist/util/exec/index.js')).href;
  const observer = registerHooks({ load(url, context, nextLoad) {
    const result = nextLoad(url, context);
    if (url !== execModuleUrl) return result;
    const source = typeof result.source === 'string' ? result.source : Buffer.from(result.source).toString('utf8');
    const entry = 'async function exec(cmd, opts = {}) {';
    assert.equal(source.split(entry).length, 2, 'Observe only the exact pinned execution entry');
    return { ...result, source: source.replace(entry, entry + '\n globalThis.__renovateFixtureToolSelections.push({ cmd, toolConstraints: structuredClone(opts.toolConstraints ?? []) });') };
  } });
  if (mode === 'patched') {
    const { installPythonBuildCompat } = await import('../scripts/renovate-security-compat.mjs');
    const compatibility = installPythonBuildCompat(installation);
    await compatibility.preload();
    assert.ok(compatibility.wasApplied(), 'Production compatibility hook was not loaded');
  }
  const imp = (relative) => import(pathToFileURL(join(installation, 'dist', relative + '.js')).href);
  const { GlobalConfig } = await imp('config/global');
  const { setCustomEnv, setUserEnv } = await imp('util/env');
  const { initRepo, syncGit } = await imp('util/git/index');
  const { updateArtifacts: compileArtifacts } = await imp('modules/manager/pip-compile/artifacts');
  const { getExecOptions: compileExecOptions } = await imp('modules/manager/pip-compile/common');
  const { updateArtifacts: projectArtifacts } = await imp('modules/manager/pep621/artifacts');
  const { default: postArtifacts } = await imp('workers/repository/update/branch/execute-post-upgrade-commands');
  const scratch = realpathSync(mkdtempSync(join(tmpdir(), 'renovate-python-builds-')));
  const results = {};
  const run = (program, args, cwd) => {
    const result = spawnSync(program, args, { cwd, encoding: 'utf8', timeout: 30_000 });
    assert.equal(result.error, undefined, String(result.error));
    assert.equal(result.status, 0, `${program}: ${result.stderr}`);
    return result.stdout.trim();
  };
  try {
    const python = run('python3', ['-c', 'import sys; print(sys.executable)']);
    assert.match(run('uv', ['--version']), /^uv 0\.13\.4(?:\s|$)/);
    for (const name of ['sdist', 'wheel', 'dynamic-project', 'static-project', 'compile-project', 'post-dynamic-project', 'post-static-project', 'post-nested-dynamic-project', 'post-nested-static-project', 'nested-static-parent', 'nested-dynamic-parent']) {
      const dir = join(scratch, name);
      const nestedParent = name.startsWith('nested-');
      const projectDir = nestedParent ? join(dir, 'research') : name.startsWith('post-nested-') ? join(dir, 'nested/project') : dir;
      if (nestedParent) {
        run(python, [join(dirname(self), 'renovate_python_fixtures.py'), dir,
          name === 'nested-static-parent' ? 'static-project' : 'dynamic-project']);
        run(python, [join(dirname(self), 'renovate_python_fixtures.py'), projectDir, 'static-project']);
      } else {
        run(python, [join(dirname(self), 'renovate_python_fixtures.py'), projectDir, name]);
      }
      run('git', ['init', '-b', 'main'], dir);
      run('git', ['add', '.'], dir);
      run('git', ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        '-c', 'commit.gpgsign=false', 'commit', '-m', 'Inert fixture'], dir);
      run('git', ['remote', 'add', 'origin', dir], dir);
      const nested = name.startsWith('post-nested-');
      const postCommand = 'uv export ' + (nested ? '--directory nested/project ' : '') + '--locked --offline --no-build --format=requirements-txt --output-file=export.txt';
      GlobalConfig.set({ localDir: dir, platform: 'github', binarySource: 'global',
        cacheDir: join(scratch, 'renovate-cache'), executionTimeout: 1, ignoreScripts: true,
        allowScripts: false, allowedEnv: [], allowShellExecutorForPostUpgradeCommands: false,
        allowedCommands: ['^' + postCommand.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$'] });
      setCustomEnv({ PATH: process.env.PATH ?? '', HOME: dir, UV_CACHE_DIR: join(dir, 'uv-cache'),
        UV_PYTHON: python, UV_PYTHON_DOWNLOADS: 'never', UV_OFFLINE: '1', UV_NO_BUILD: '1', PIP_NO_INDEX: '1' });
      setUserEnv({});
      await initRepo({ url: dir, defaultBranch: 'main', currentBranch: 'main' });
      await syncGit();
      if (name.startsWith('post-')) {
        const relativeProject = nested ? 'nested/project/pyproject.toml' : 'pyproject.toml';
        const recipe = { commands: [postCommand], executionMode: 'update', fileFilters: [nested ? 'nested/project/export.txt' : 'export.txt'], installTools: { uv: {}, python: {} } };
        const result = await postArtifacts({ manager: 'pep621', branchName: 'renovate/fixture',
          upgrades: [{ depName: 'fixture', packageFile: relativeProject, postUpgradeTasks: recipe, constraints: { uv: '99.0.0', python: '>=3.9' } }],
          postUpgradeTasks: { commands: [], executionMode: 'update' },
          updatedPackageFiles: [{ type: 'addition', path: relativeProject, contents: readFileSync(join(projectDir, 'pyproject.toml'), 'utf8') }],
          updatedArtifacts: [], artifactErrors: [{ stderr: 'Synthetic earlier artifact error' }] });
        results[name] = { backendExecuted: existsSync(join(projectDir, 'backend-executed.txt')),
          outputCreated: existsSync(join(projectDir, 'export.txt')), artifactErrors: result.artifactErrors.length,
          diagnostics: result.artifactErrors.map((error) => error.stderr) };
        if (existsSync(join(projectDir, 'export.txt'))) {
          assert.ok(result.updatedArtifacts.some((file) => file.path === (nested ? 'nested/project/export.txt' : 'export.txt')));
        }
        if (mode === 'patched' && name === 'post-static-project') {
          for (const location of ['--directory=other', '--directory one --directory two', '--project other', '--config-file other', '-C other', '-P other']) {
            const invalidCommand = postCommand.replace('uv export ', 'uv export ' + location + ' ');
            GlobalConfig.set({ ...GlobalConfig.get(), allowedCommands: ['^' + invalidCommand.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '$'] });
            const invalid = await postArtifacts({ manager: 'pep621', branchName: 'renovate/fixture',
              upgrades: [{ depName: 'fixture', packageFile: relativeProject, postUpgradeTasks: { ...recipe, commands: [invalidCommand] } }],
              postUpgradeTasks: { commands: [], executionMode: 'update' },
              updatedPackageFiles: [{ type: 'addition', path: relativeProject, contents: readFileSync(join(projectDir, 'pyproject.toml'), 'utf8') }],
              updatedArtifacts: [], artifactErrors: [] });
            assert.equal(invalid.artifactErrors.length, 1);
            assert.match(invalid.artifactErrors[0].stderr, /Unqualified UV export location option/);
          }
          results[name].alternateLocationsRejected = true;
        }
        continue;
      }
      const isProject = nestedParent || ['dynamic-project', 'static-project'].includes(name);
      const input = nestedParent ? 'research/pyproject.toml' : name.endsWith('project') ? 'pyproject.toml' : 'requirements.in';
      const update = isProject ? projectArtifacts : compileArtifacts;
      const args = { packageFileName: input, newPackageFileContent: readFileSync(join(dir, input), 'utf8'),
        updatedDeps: [], config: { lockFiles: [nestedParent ? 'research/uv.lock' : isProject ? 'uv.lock' : 'requirements.txt'], ignoreScripts: true, constraints: { uv: '99.0.0', python: '>=3.9' } } };
      const result = await update(args);
      const receipt = { backendExecuted: existsSync(join(dir, 'backend-executed.txt')),
        artifactErrors: (result ?? []).filter((item) => item.artifactError).length,
        diagnostics: (result ?? []).filter((item) => item.artifactError).map((item) => item.artifactError.stderr) };
      if (nestedParent) receipt.childBackendExecuted = existsSync(join(projectDir, 'backend-executed.txt'));
      if (name === 'wheel') {
        const first = readFileSync(join(dir, 'requirements.txt'), 'utf8');
        receipt.hashGenerated = /--hash=sha256:/.test(first);
        const repeat = await update(args);
        receipt.repeatHeaderRoundTrip = !(repeat ?? []).some((item) => item.artifactError) &&
          readFileSync(join(dir, 'requirements.txt'), 'utf8') === first;
      }
      results[name] = receipt;
    }
    if (mode === 'patched') {
      const options = await compileExecOptions({ constraints: { uv: '99.0.0', python: '>=3.9' } }, 'uv', '.', {}, '3.12');
      assert.deepEqual(options.toolConstraints, [{ toolName: 'python', constraint: '>=3.9' }, { toolName: 'uv', constraint: '0.13.4' }]);
      for (const prefix of ['uv pip compile ', 'uv lock', 'uv export ']) {
        const selected = globalThis.__renovateFixtureToolSelections.filter((entry) => typeof entry.cmd === 'string' && entry.cmd.startsWith(prefix));
        assert.ok(selected.length > 0, prefix + ' actual execution was not observed');
        for (const entry of selected) {
          assert.equal(entry.toolConstraints.find((tool) => tool.toolName === 'uv')?.constraint, '0.13.4');
          assert.equal(entry.toolConstraints.find((tool) => tool.toolName === 'python')?.constraint, '>=3.9');
        }
      }
      results.qualifiedToolConstraints = true;
    }
    console.log(marker + JSON.stringify(results));
  } finally {
    observer.deregister();
    rmSync(scratch, { recursive: true, force: true });
  }
}
