// Offline CLI/lockfile/installation probe for the reviewed npm distribution.
// Run during image construction; no credentials, registry or lifecycle hooks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

const npmRoot = process.argv[2] || '/usr/local/lib/node_modules/npm';
assert.equal(require(path.join(npmRoot, 'package.json')).version, '11.21.1');
assert.equal(require(path.join(npmRoot, 'node_modules/undici/package.json')).version, '6.29.1');
assert.equal(require(path.join(npmRoot, 'node_modules/ip-address/package.json')).version, '10.7.4');
const work = fs.mkdtempSync(path.join(os.tmpdir(), 'runner-npm-probe-'));
const env = {
  PATH: process.env.PATH,
  HOME: work,
  npm_config_cache: path.join(work, 'cache'),
  npm_config_userconfig: path.join(work, 'empty.npmrc'),
  npm_config_globalconfig: path.join(work, 'empty-global.npmrc'),
  npm_config_offline: 'true',
  npm_config_audit: 'false',
  npm_config_fund: 'false',
  npm_config_ignore_scripts: 'true',
};
const npm = (cwd, args) => execFileSync(process.execPath,
  [path.join(npmRoot, 'bin/npm-cli.js'), ...args],
  { cwd, env, timeout: 60000, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
try {
  assert.equal(npm(work, ['--version']).trim(), '11.21.1');
  const dependency = path.join(work, 'dependency');
  const consumer = path.join(work, 'consumer');
  fs.mkdirSync(dependency);
  fs.mkdirSync(consumer);
  fs.writeFileSync(path.join(dependency, 'package.json'), JSON.stringify({
    name: 'runner-npm-probe', version: '1.0.0', bin: { 'runner-npm-probe': 'cli.js' },
    scripts: { postinstall: 'exit 97' },
  }));
  fs.writeFileSync(path.join(dependency, 'cli.js'),
    '#!/usr/bin/env node\nprocess.stdout.write("npm-probe-ok\\n");\n', { mode: 0o755 });
  const packedResult = JSON.parse(npm(dependency, ['pack', '--json', '--ignore-scripts']));
  assert.ok(Array.isArray(packedResult) && packedResult.length === 1, JSON.stringify(packedResult));
  const packed = packedResult[0];
  assert.equal(packed.filename, 'runner-npm-probe-1.0.0.tgz');
  fs.writeFileSync(path.join(consumer, 'package.json'), JSON.stringify({
    name: 'runner-npm-consumer', version: '1.0.0', private: true,
    dependencies: { 'runner-npm-probe': `file:../dependency/${packed.filename}` },
  }));
  npm(consumer, ['install', '--ignore-scripts']);
  const lock = fs.readFileSync(path.join(consumer, 'package-lock.json'));
  fs.rmSync(path.join(consumer, 'node_modules'), { recursive: true });
  npm(consumer, ['ci', '--ignore-scripts']);
  assert.deepEqual(fs.readFileSync(path.join(consumer, 'package-lock.json')), lock);
  assert.equal(npm(consumer, ['exec', '--offline', '--', 'runner-npm-probe']).trim(), 'npm-probe-ok');
  const installed = JSON.parse(npm(consumer, ['ls', '--all', '--json']));
  assert.equal(installed.dependencies['runner-npm-probe'].version, '1.0.0');
  process.stdout.write('PASS: npm pack, install, ci, exec and unchanged lockfile (offline)\n');
} finally {
  fs.rmSync(work, { recursive: true, force: true });
}
