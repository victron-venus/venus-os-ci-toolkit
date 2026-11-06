// Real npm/Arborist regression against an isolated, inert loopback registry.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createServer } from 'node:http';
import { gzipSync } from 'node:zlib';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

const run = promisify(execFile);
function archive(files) {
  const chunks = [];
  for (const [name, data] of Object.entries(files)) {
    const bytes = Buffer.from(data), header = Buffer.alloc(512);
    header.write(name, 0, 100, 'utf8');
    header.write('0000644\0', 100); header.write('0000000\0', 108); header.write('0000000\0', 116);
    header.write(bytes.length.toString(8).padStart(11, '0') + '\0', 124);
    header.write('00000000000\0', 136); header.fill(32, 148, 156); header[156] = 48;
    header.write('ustar\0', 257); header.write('00', 263);
    const checksum = [...header].reduce((a, b) => a + b, 0);
    header.write(checksum.toString(8).padStart(6, '0') + '\0 ', 148);
    chunks.push(header, bytes, Buffer.alloc((512 - bytes.length % 512) % 512));
  }
  return gzipSync(Buffer.concat([...chunks, Buffer.alloc(1024)]));
}

export async function runRegistryProof({ before, upgrade, after }) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'renovate-npm-bundle-')));
  const bundle = { name: 'qualified-bundle-fixture', version: '1.0.0', dependencies: { 'qualified-included-fixture': '1.0.0' }, bundledDependencies: ['qualified-included-fixture'] };
  const included = { name: 'qualified-included-fixture', version: '1.0.0' };
  const tar = archive({ 'package/package.json': JSON.stringify(bundle), 'package/node_modules/qualified-included-fixture/package.json': JSON.stringify(included) });
  const tarHash = 'sha512-' + createHash('sha512').update(tar).digest('base64');
  let registry;
  const requests = [];
  const server = createServer((request, response) => {
    requests.push(request.url);
    if (request.url === '/qualified-bundle-fixture/-/qualified-bundle-fixture-1.0.0.tgz') {
      response.writeHead(200, { 'content-type': 'application/octet-stream' }); response.end(tar); return;
    }
    if (request.url === '/qualified-bundle-fixture') {
      const version = { ...bundle, dist: { tarball: registry + 'qualified-bundle-fixture/-/qualified-bundle-fixture-1.0.0.tgz', integrity: tarHash } };
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ name: bundle.name, 'dist-tags': { latest: '1.0.0' }, versions: { '1.0.0': version } })); return;
    }
    response.writeHead(404); response.end('fixture route not found');
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  registry = `http://127.0.0.1:${server.address().port}/`;
  const results = [];
  const fixture = (name, dependencies, scripts) => {
    const cwd = path.join(root, name); fs.mkdirSync(cwd);
    fs.writeFileSync(path.join(cwd, 'package.json'), JSON.stringify({ name: 'qualified-root-fixture', version: '1.0.0', dependencies, scripts }));
    fs.writeFileSync(path.join(cwd, 'user.npmrc'), ''); fs.writeFileSync(path.join(cwd, 'global.npmrc'), '');
    return { cwd, registry, env: { NPM_CONFIG_UPDATE_NOTIFIER: 'false', NPM_CONFIG_REGISTRY: registry, NPM_CONFIG_CACHE: path.join(cwd, 'cache'), NPM_CONFIG_USERCONFIG: path.join(cwd, 'user.npmrc'), NPM_CONFIG_GLOBALCONFIG: path.join(cwd, 'global.npmrc') } };
  };
  try {
    const baseline = fixture('before', { [bundle.name]: '1.0.0' });
    const error = await before(baseline).then(() => null, (err) => err);
    assert.ok(error); assert.match(error.stderr ?? '', /EALLOWREMOTE/);
    results.push('unpatched npm rejects the ordinary registry bundled archive');
    const positive = fixture('after', { [bundle.name]: '1.0.0' });
    await upgrade(positive);
    const lock = JSON.parse(fs.readFileSync(path.join(positive.cwd, 'package-lock.json'), 'utf8'));
    assert.equal(lock.packages['node_modules/' + bundle.name].version, '1.0.0');
    assert.equal(lock.packages['node_modules/' + bundle.name].integrity, tarHash);
    assert.equal(lock.packages['node_modules/' + bundle.name + '/node_modules/' + included.name].inBundle, true);
    results.push('native post-install guard permits the same verified registry bundle');
    for (const [name, spec, code] of [
      ['remote', 'https://example.invalid/never-fetch.tgz', /EALLOWREMOTE/],
      ['registry-url', registry + 'qualified-bundle-fixture/-/qualified-bundle-fixture-1.0.0.tgz', /EALLOWREMOTE/],
      ['git', 'git+https://github.com/example/never-fetch.git#main', /EALLOWGIT/],
    ]) {
      const test = fixture(name, { fixture: spec }), count = requests.length;
      const denied = await after(test).then(() => null, (err) => err);
      assert.ok(denied); assert.match(denied.stderr ?? '', code);
      assert.equal(requests.length, count, 'Rejected URL requests: ' + JSON.stringify(requests.slice(count)));
      results.push(`${name} remains denied before fetching`);
    }
    const marker = path.join(root, 'lifecycle-marker');
    const script = `node -e "require('node:fs').writeFileSync('${marker}', 'ran')"`;
    const control = fixture('script-control', {}, { install: script });
    await after(control, false); assert.ok(fs.existsSync(marker)); fs.rmSync(marker);
    const guarded = fixture('script-guarded', {}, { install: script });
    await after(guarded, true); assert.ok(!fs.existsSync(marker));
    results.push('the active lifecycle canary is blocked by ignore-scripts');
    return results;
  } finally {
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
}

async function toolGuardProof(prepareNpmBundle) {
  const actual = prepareNpmBundle();
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'renovate-npm-tool-guards-')));
  const paths = ['package.json', 'bin/npm-cli.js', 'lib/cli.js', 'node_modules/@npmcli/arborist/lib/arborist/build-ideal-tree.js', 'node_modules/@npmcli/arborist/lib/arborist/reify.js'];
  const results = [];
  const setup = (name) => {
    const installRoot = path.join(root, name), prefix = path.join(installRoot, 'tools/npm/12.2.2', actual.nodeVersion), packageDir = path.join(prefix, 'node_modules/npm');
    for (const file of paths) {
      const destination = path.join(packageDir, file); fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.copyFileSync(path.join(actual.packageDir, file), destination);
    }
    fs.mkdirSync(path.join(installRoot, 'bin'), { recursive: true });
    for (const tool of ['npm', 'node']) {
      const wrapper = fs.readFileSync('/opt/containerbase/bin/' + tool, 'utf8').replaceAll('/opt/containerbase', installRoot);
      fs.writeFileSync(path.join(installRoot, 'bin', tool), wrapper, { mode: 0o755 });
    }
    const node = path.join(installRoot, 'tools/node', actual.nodeVersion, 'bin/node');
    fs.mkdirSync(path.dirname(node), { recursive: true }); fs.writeFileSync(node, 'inert metadata fixture', { mode: 0o755 });
    fs.mkdirSync(path.join(prefix, 'node_modules/.bin'), { recursive: true });
    fs.symlinkSync('../npm/bin/npm-cli.js', path.join(prefix, 'node_modules/.bin/npm'));
    fs.symlinkSync(path.join(prefix, 'node_modules/.bin'), path.join(prefix, 'bin'));
    return { installRoot, packageDir, env: { PATH: path.join(installRoot, 'bin') + ':' + process.env.PATH } };
  };
  try {
    const positive = setup('positive'); prepareNpmBundle(positive); prepareNpmBundle(positive);
    results.push('verified cached tools remain idempotent');
    for (const file of paths) {
      const fixture = setup('tamper-' + results.length); fs.appendFileSync(path.join(fixture.packageDir, file), '\nchanged');
      assert.throws(() => prepareNpmBundle(fixture), /Unreviewed/); results.push('reject altered ' + file);
    }
    for (const tool of ['npm', 'node']) {
      const fixture = setup('shadow-' + tool), shadow = path.join(fixture.installRoot, 'repository'); fs.mkdirSync(shadow);
      fs.writeFileSync(path.join(shadow, tool), '#!/bin/sh\nexit 0\n', { mode: 0o755 }); fixture.env.PATH = shadow + ':' + fixture.env.PATH;
      assert.throws(() => prepareNpmBundle(fixture), /shadowed/); results.push('reject repository shadow ' + tool);
    }
    const symlink = setup('symlink'), target = path.join(symlink.packageDir, paths[3]); fs.renameSync(target, target + '.other'); fs.symlinkSync(target + '.other', target);
    assert.throws(() => prepareNpmBundle(symlink), /symlink/); results.push('reject redirected module');
    return results;
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  assert.equal(process.argv.length, 4, 'Pass the fixed runtime and one qualified mount layout');
  assert.equal(process.argv[2], '/usr/local/renovate');
  const runtime = '/usr/local/renovate';
  assert.equal(fs.realpathSync(runtime), runtime);
  const compatPath = process.argv[3] === '/config/renovate-security-compat.mjs'
    ? '/config/renovate-security-compat.mjs' : '/renovate-security-compat.mjs';
  assert.equal(process.argv[3], compatPath, 'Unknown helper mount layout');
  const cli = (test, ignoreScripts = true, lockOnly = false) => run('/opt/containerbase/bin/npm', ['install', ...(lockOnly ? ['--package-lock-only'] : []), '--no-audit', '--no-fund', '--allow-remote=none', '--allow-git=none', ...(ignoreScripts ? ['--ignore-scripts'] : [])], { cwd: test.cwd, env: { ...process.env, ...test.env }, timeout: 60000, maxBuffer: 1024 * 1024 });
  const { installNpmCompat } = await import(pathToFileURL(compatPath));
  const hook = installNpmCompat(runtime); await hook.preload(); assert.ok(hook.wasApplied());
  const { GlobalConfig } = await import(pathToFileURL(path.join(runtime, 'dist/config/global.js')));
  const { setCustomEnv } = await import(pathToFileURL(path.join(runtime, 'dist/util/env.js')));
  const { generateLockFile } = await import(pathToFileURL(path.join(runtime, 'dist/modules/manager/npm/post-update/npm.js')));
  const { init } = await import(pathToFileURL(path.join(runtime, 'dist/logger/index.js'))); await init();
  const results = await runRegistryProof({
    before: (test) => cli(test, true, true),
    after: cli,
    upgrade: async (test) => {
      GlobalConfig.set({ localDir: test.cwd, cacheDir: path.join(test.cwd, 'renovate-cache'), containerbaseDir: '/tmp/renovate/cache/containerbase', binarySource: 'install', executionTimeout: 2, allowScripts: false });
      setCustomEnv(test.env);
      const result = await generateLockFile('.', test.env, 'package-lock.json', { constraints: { node: '24.22.2', npm: '>=7' }, ignoreScripts: false }, [{ depName: 'qualified-bundle-fixture', newValue: '1.0.0' }]);
      assert.equal(result.error, false, result.stderr);
      assert.ok(result.lockFile);
      // Repeat the actual helper via native generation to prove idempotent cached-tool reuse.
      const repeat = await generateLockFile('.', test.env, 'package-lock.json', { constraints: { node: '24.22.2', npm: '>=7' } }, [{ depName: 'qualified-bundle-fixture', newValue: '1.0.0' }]);
      assert.equal(repeat.error, false, repeat.stderr);
    },
  });
  const { prepareNpmBundle } = await import(pathToFileURL(path.join(path.dirname(compatPath), 'prepare-renovate-npm.mjs')));
  const toolGuards = await toolGuardProof(prepareNpmBundle);
  console.log(JSON.stringify({ compatPath, results, toolGuards }, null, 2));
}
