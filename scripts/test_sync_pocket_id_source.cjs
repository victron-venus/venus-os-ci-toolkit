'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const https = require('node:https');
const net = require('node:net');
const cp = require('node:child_process');
const { EventEmitter } = require('node:events');
const helper = require('./sync_pocket_id_source.cjs');
const FIXTURE_DATA = {"recipe": "IyBzeW50YXg9ZG9ja2VyL2RvY2tlcmZpbGU6MS4yOQojIFJlYnVpbGQgdGhlIHJlbGVhc2VkIGFwcGxpY2F0aW9uLCBpbmNsdWRpbmcgaXRzIHNlY3VyaXR5IGRlcGVuZGVuY3kgZml4ZXMsIHdpdGggdGhlIHJldmlld2VkIGNvbXBpbGVyLgpGUk9NIC0tcGxhdGZvcm09JEJVSUxEUExBVEZPUk0gZ29sYW5nOjEuMjcuNC1ib29rd29ybUBzaGEyNTY6NGZhN2M5NjhiYTkzZjE2MzU2NzA1MjJkYmIwMWEyYzc4MTUwNDQzZTEzODU3NDY5MjQ1NjYwNzlmMzY1NzBlYSBBUyBzb3VyY2UKQUREIC0tY2hlY2tzdW09c2hhMjU2OmVlMjUyNWRiNzA2MWIzNTVkZWI1ZjEyZWI2YzE1NDY1ZmFmN2U3YjVmZmZkN2I5OTE1OTE5ZDQwY2Q4YWY1NGMgaHR0cHM6Ly9jb2RlbG9hZC5naXRodWIuY29tL3BvY2tldC1pZC9wb2NrZXQtaWQvdGFyLmd6L3JlZnMvdGFncy92Mi4xOS4zIC90bXAvc291cmNlLnRhci5negpXT1JLRElSIC9zcmMKUlVOIHRhciAteHpmIC90bXAvc291cmNlLnRhci5neiAtLXN0cmlwLWNvbXBvbmVudHM9MQoKRlJPTSAtLXBsYXRmb3JtPSRCVUlMRFBMQVRGT1JNIGdoY3IuaW8vdm9pZHplcm8tZGV2L3ZpdGUtcGx1czoxLjYuMEBzaGEyNTY6ZWVmZTg0ZjZiZjMyNzU2MzA3ZmUxNDUyYjQ5YzgxZTZiM2FmOThlZGIzMjhhODM4YWMxZTkwZDZmYWFhN2FjYyBBUyBmcm9udGVuZApFTlYgQ0k9dHJ1ZQpXT1JLRElSIC9hcHAKQ09QWSAtLWZyb209c291cmNlIC0tY2hvd249dnA6dnAgL3NyYy8gL2FwcC8KUlVOIHZwIC1DIGZyb250ZW5kIGluc3RhbGwgLS1mcm96ZW4tbG9ja2ZpbGUKUlVOIEJVSUxEX09VVFBVVF9QQVRIPWRpc3QgdnAgLUMgZnJvbnRlbmQgcnVuIGJ1aWxkCgpGUk9NIHNvdXJjZSBBUyBiYWNrZW5kCkFSRyBUQVJHRVRPUwpBUkcgVEFSR0VUQVJDSApFTlYgR09UT09MQ0hBSU49bG9jYWwgQ0dPX0VOQUJMRUQ9MCBHT01BWFBST0NTPTIKV09SS0RJUiAvc3JjL2JhY2tlbmQKUlVOIC0tbW91bnQ9dHlwZT1jYWNoZSx0YXJnZXQ9L2dvL3BrZy9tb2QgXAogICAgLS1tb3VudD10eXBlPWNhY2hlLHRhcmdldD0vcm9vdC8uY2FjaGUvZ28tYnVpbGQgXAogICAgZ28gbW9kIGRvd25sb2FkICYmIGdvIG1vZCB2ZXJpZnkgJiYgZ28gdGVzdCAtcD0yIGdpdGh1Yi5jb20vZ28tam9zZS9nby1qb3NlL3Y0IGdvbGFuZy5vcmcveC9uZXQvaHRtbApDT1BZIC0tZnJvbT1mcm9udGVuZCAvYXBwL2Zyb250ZW5kL2Rpc3QgLi9mcm9udGVuZC9kaXN0ClJVTiAtLW1vdW50PXR5cGU9Y2FjaGUsdGFyZ2V0PS9nby9wa2cvbW9kIFwKICAgIC0tbW91bnQ9dHlwZT1jYWNoZSx0YXJnZXQ9L3Jvb3QvLmNhY2hlL2dvLWJ1aWxkIFwKICAgIEdPT1M9JFRBUkdFVE9TIEdPQVJDSD0kVEFSR0VUQVJDSCBnbyBidWlsZCAtcD0yIC10cmltcGF0aCBcCiAgICAtbGRmbGFncz0nLXcgLXMgLWJ1aWxkaWQ9Mi4xOS4zLXNlY3VyaXR5LjEgLVggZ2l0aHViLmNvbS9wb2NrZXQtaWQvcG9ja2V0LWlkL2JhY2tlbmQvaW50ZXJuYWwvY29tbW9uLlZlcnNpb249Mi4xOS4zLXNlY3VyaXR5LjEnIFwKICAgIC1vIC9vdXQvcG9ja2V0LWlkIC4vY21kCgojIFByZXNlcnZlIHRoZSB1cHN0cmVhbSBkaXN0cm9sZXNzIGZpbGVzeXN0ZW0sIG5vbi1yb290IGlkZW50aXR5IGFuZCBtZXRhZGF0YS4KRlJPTSBnaGNyLmlvL3BvY2tldC1pZC9wb2NrZXQtaWQ6djIuMTkuMy1kaXN0cm9sZXNzQHNoYTI1Njo2YTAwNDVhODliODBmYTY2MjRjMDkxNzg2ZTdiZTEzMmE0ZGRlODgwY2E2NDcyN2E2YjAxOWMwNTZiMDkxOTgyClVTRVIgNjU1MzIKQ09QWSAtLWZyb209YmFja2VuZCAtLWNobW9kPTA3NTUgL291dC9wb2NrZXQtaWQgL2FwcC9wb2NrZXQtaWQKTEFCRUwgb3JnLm9wZW5jb250YWluZXJzLmltYWdlLnZlcnNpb249IjIuMTkuMy1zZWN1cml0eS4xIiBcCiAgICAgIG9yZy5vcGVuY29udGFpbmVycy5pbWFnZS5zb3VyY2U9Imh0dHBzOi8vZXhhbXBsZS5pbnZhbGlkL3BvY2tldC1pZC1zZWN1cml0eSIgXAogICAgICBvcmcub3BlbmNvbnRhaW5lcnMuaW1hZ2UuZGVzY3JpcHRpb249IlBvY2tldCBJRCAyLjE5LjMgc291cmNlL3J1bnRpbWUtY29oZXJlbnQgc2VjdXJpdHkgcmVidWlsZCIK", "archive": "H4sIAAAAAAAA/+3SQQ6CQAyFYdaeggswUwHxPMAQJIpDdOb+VCPRuHFD1MX/bdp085q0k2+PXcgGl+UmFyO2qXVwdrb3ZvQuWYOoqizvVb1XkV317G/zbSHFPklllfQP4jXUF43/RtYf0hvHU5f2QzjExrR+tNPyES/d4yk2v94WAAAAAAAAAAAAAAAAALCYARI5lZIAKAAA"};
const RECIPE = Buffer.from(FIXTURE_DATA.recipe, 'base64');
const ARCHIVE = Buffer.from(FIXTURE_DATA.archive, 'base64');
const ROOT = fs.mkdtempSync(path.join(os.tmpdir(), 'pocket-id-node-tests-'));
const OLD_CWD = process.cwd();
const original = {};
const ownedFds = new Map();
const originalSpawn = cp.spawnSync;
const CHILD = 'process.stdout.write("trusted-child-no-preload")';
function owned(value) {
  if (typeof value === 'number') return ownedFds.has(value);
  let resolved = String(value);
  const proc = resolved.match(/^\/proc\/self\/fd\/(\d+)(\/.*)?$/);
  if (proc) {
    const parent = ownedFds.get(Number(proc[1]));
    if (!parent) return false;
    resolved = parent + (proc[2] || '');
  }
  resolved = path.resolve(resolved);
  return resolved === ROOT || resolved.startsWith(ROOT + path.sep);
}
function resolveOwned(value) {
  const proc = String(value).match(/^\/proc\/self\/fd\/(\d+)(\/.*)?$/);
  if (proc) return path.resolve(ownedFds.get(Number(proc[1])) + (proc[2] || ''));
  return path.resolve(value);
}
// No real provider connections; all helper HTTPS calls below are mocked.
const originalConnect = net.Socket.prototype.connect;
net.Socket.prototype.connect = function () { throw new Error('TEST_NETWORK_DENIED'); };
original.openSync = fs.openSync;
fs.openSync = function (value, flags, ...rest) {
  const writing = typeof flags === 'string' ? /[wa+]/.test(flags)
    : Boolean(flags & (fs.constants.O_WRONLY | fs.constants.O_RDWR | fs.constants.O_CREAT | fs.constants.O_TRUNC | fs.constants.O_APPEND));
  if (writing) assert(owned(value), 'TEST_WRITE_OUTSIDE_OWNED_TEMP');
  const fd = original.openSync.call(fs, value, flags, ...rest);
  if (owned(value)) ownedFds.set(fd, resolveOwned(value));
  return fd;
};
original.closeSync = fs.closeSync;
fs.closeSync = function (fd) { ownedFds.delete(fd); return original.closeSync.call(fs, fd); };
for (const name of ['writeFileSync', 'appendFileSync', 'mkdirSync', 'rmSync', 'rmdirSync', 'unlinkSync', 'chmodSync', 'chownSync', 'truncateSync', 'utimesSync']) {
  original[name] = fs[name];
  fs[name] = function (value, ...rest) { assert(owned(value), 'TEST_WRITE_OUTSIDE_OWNED_TEMP'); return original[name].call(fs, value, ...rest); };
}
for (const name of ['renameSync', 'linkSync']) {
  original[name] = fs[name];
  fs[name] = function (a, b, ...rest) { assert(owned(a) && owned(b), 'TEST_RENAME_OUTSIDE_OWNED_TEMP'); return original[name].call(fs, a, b, ...rest); };
}
original.symlinkSync = fs.symlinkSync;
fs.symlinkSync = function (a, b, ...rest) { assert(owned(b), 'TEST_SYMLINK_OUTSIDE_OWNED_TEMP'); return original.symlinkSync.call(fs, a, b, ...rest); };
for (const name of ['writeSync', 'fchmodSync', 'fchownSync']) {
  original[name] = fs[name];
  fs[name] = function (fd, ...rest) { assert(owned(fd), 'TEST_FD_WRITE_OUTSIDE_OWNED_TEMP'); return original[name].call(fs, fd, ...rest); };
}
for (const name of ['spawn', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) {
  original['cp_' + name] = cp[name]; cp[name] = () => { throw new Error('TEST_PROCESS_DENIED'); };
}
cp.spawnSync = function (exe, args, options) {
  assert.equal(exe, '/usr/bin/env');
  assert.deepEqual(args, ['-u', 'NODE_OPTIONS', '-u', 'NODE_PATH', process.execPath, '--no-addons', '-e', CHILD]);
  return originalSpawn(exe, args, options);
};
let fixtureIndex = 0;
function temp() {
  const root = path.join(ROOT, String(++fixtureIndex));
  const image = path.join(root, 'deployments/pocket-id/image');
  fs.mkdirSync(image, { recursive: true });
  const file = path.join(image, 'Dockerfile'); fs.writeFileSync(file, RECIPE, { mode: 0o644 });
  return { root, image, file };
}
function runtime(version = '2.20.0') {
  return Buffer.from(RECIPE.toString().replace(':v2.19.3-distroless@', ':v' + version + '-distroless@'));
}
function digest(value = ARCHIVE) { return crypto.createHash('sha256').update(value).digest('hex'); }
function mockHttp(t, config = {}) {
  const calls = [], responses = [], requests = [];
  t.mock.method(https, 'get', (url, options, callback) => {
    const request = new EventEmitter(); request.destroyed = false;
    request.destroy = () => { request.destroyed = true; };
    request.setTimeout = (milliseconds, handler) => { request.idle = milliseconds; request.idleHandler = handler; };
    calls.push({ url, options }); requests.push(request);
    process.nextTick(() => {
      const response = new EventEmitter(); response.destroyed = false;
      response.destroy = () => { response.destroyed = true; };
      const row = config.sequence ? config.sequence[calls.length - 1] : config;
      response.statusCode = row.status === undefined ? 200 : row.status;
      const body = row.body === undefined ? ARCHIVE : row.body;
      response.headers = row.headers || (row.noLength ? {} : { 'content-length': String(body.length) });
      responses.push(response); callback(response);
      if (row.hang || response.destroyed) return;
      const blocks = row.blocks || [body];
      for (const block of blocks) { if (response.destroyed) break; response.emit('data', block); }
      if (row.beforeEnd) row.beforeEnd();
      if (!response.destroyed) response.emit(row.abort ? 'aborted' : 'end');
    });
    return request;
  });
  return { calls, responses, requests };
}
const isCode = code => error => error instanceof helper.Hold && error.message === code;

// Pure controls run on macOS as well as Linux. They never claim FD qualification.
test('pure: actual source layout and future release transformations preserve all other bytes', () => {
  assert.equal(helper.parseRecipe(RECIPE).oldVersion, '2.19.3');
  for (const version of ['2.20.0', '3.0.0']) {
    const before = runtime(version), after = helper.transform(before, digest());
    const expected = before.toString().replaceAll('2.19.3', version)
      .replace('ee2525db7061b355deb5f12eb6c15465faf7e7b5fffd7b9915919d40cd8af54c', digest());
    assert.equal(after.toString(), expected);
    assert.equal(helper.parseRecipe(after).oldVersion, version);
    assert.equal(helper.parseRecipe(after).newVersion, version);
  }
});
test('pure: malformed and duplicate identities, rollback, encoding and size fail', () => {
  const rows = [
    Buffer.concat([RECIPE, Buffer.from('\nLABEL org.opencontainers.image.version="2.19.3-security.1"\n')]),
    Buffer.from(RECIPE.toString().replace('common.Version=2.19.3-security.1', 'common.Version=2.19.2-security.1')),
    runtime('2.19.2'), runtime('2.20.0;bad'),
    Buffer.from(RECIPE.toString().replace('USER 65532', 'USER 0')),
    Buffer.concat([RECIPE, Buffer.from('\nFROM alpine:3.20\n')]),
    Buffer.from([0xff, 0xff]), Buffer.alloc(helper.LIMITS.MAX_RECIPE + 1),
  ];
  for (const value of rows) assert.throws(() => helper.parseRecipe(value), helper.Hold);
  assert.throws(() => helper.transform(runtime(), 'not-a-digest'), isCode('ARCHIVE_DIGEST_FORMAT'));
  assert.throws(() => helper.archiveUrl('999999999999999999.1.1'), isCode('VERSION_FORMAT'));
});
test('pure: canonical SemVer rejects leading zeros at every public transform seam', () => {
  for (const version of ['01.19.3', '2.019.3', '2.19.03']) {
    assert.throws(() => helper.archiveUrl(version), isCode('VERSION_FORMAT'));
    // Genuine runtime-only mismatch, plus a fully coherent but noncanonical source recipe.
    for (const input of [runtime(version), Buffer.from(RECIPE.toString().replaceAll('2.19.3', version))]) {
      assert.throws(() => helper.parseRecipe(input), helper.Hold);
      assert.throws(() => helper.transform(input, digest()), helper.Hold);
    }
  }
  assert.equal(helper.archiveUrl('2.0.0'), 'https://codeload.github.com/pocket-id/pocket-id/tar.gz/refs/tags/v2.0.0');
  const valid = Buffer.from(RECIPE.toString().replaceAll('2.19.3', '2.0.0'));
  assert.equal(helper.parseRecipe(valid).oldVersion, '2.0.0');
  assert.equal(helper.parseRecipe(valid).newVersion, '2.0.0');
  const oldDigest = 'ee2525db7061b355deb5f12eb6c15465faf7e7b5fffd7b9915919d40cd8af54c';
  assert.throws(() => helper.transform(valid, oldDigest), isCode('TRANSFORM_NO_VERSION_CHANGE'));
  const next = Buffer.from(valid.toString().replace(':v2.0.0-distroless@', ':v2.1.0-distroless@'));
  const result = helper.transform(next, digest());
  assert.equal(helper.parseRecipe(result).oldVersion, '2.1.0');
  assert.equal(helper.parseRecipe(result).newVersion, '2.1.0');
});
test('HTTPS: exact URL, TLS/auth boundaries and actual streamed gzip-byte SHA', async t => {
  const m = mockHttp(t); const result = await helper.fetchArchive('2.20.0');
  assert.deepEqual(result, { sha256: digest(), bytes: ARCHIVE.length });
  assert.equal(m.calls[0].url, 'https://codeload.github.com/pocket-id/pocket-id/tar.gz/refs/tags/v2.20.0');
  assert.equal(m.calls[0].options.rejectUnauthorized, true); assert.equal(m.calls[0].options.agent, false);
  assert.equal(m.calls[0].options.headers.Authorization, undefined);
  assert(m.requests.every(x => x.destroyed)); assert(m.responses.every(x => x.destroyed));
});
test('HTTPS: URL and redirect injection refused, no arbitrary archive host', async t => {
  const expected = helper.archiveUrl('2.20.0');
  for (const bad of [expected + '?token=bad', expected + '#bad', expected.replace('https:', 'http:'),
    expected.replace('codeload.github.com', 'codeload.github.com.evil.invalid'),
    expected.replace('https://', 'https://user:password@')]) assert.throws(() => helper.validateUrl(bad, expected), helper.Hold);
  for (const bad of ['../2.20.0', '2.20.0;bad', '2.20.0-rc.1']) assert.throws(() => helper.archiveUrl(bad), helper.Hold);
  const m = mockHttp(t, { status: 302, headers: { location: 'https://evil.invalid/archive' } });
  await assert.rejects(helper.fetchArchive('2.20.0'), isCode('DOWNLOAD_URL_REFUSED')); assert.equal(m.calls.length, 1);
});
test('HTTPS: one exact redirect allowed but bounded, status/gzip/length refused', async t => {
  const expected = helper.archiveUrl('2.20.0');
  const m = mockHttp(t, { sequence: [{ status: 302, headers: { location: expected } }, {}] });
  assert.equal((await helper.fetchArchive('2.20.0')).sha256, digest()); assert.equal(m.calls.length, 2);
  t.mock.restoreAll();
  for (const row of [{ status: 403 }, { body: Buffer.from('not gzip') },
    { headers: { 'content-length': String(ARCHIVE.length + 1) } },
    { headers: { 'content-length': String(helper.LIMITS.MAX_ARCHIVE + 1) } },
    { headers: { 'content-length': 'not a number' } }, { abort: true }]) {
    mockHttp(t, row); await assert.rejects(helper.fetchArchive('2.20.0'), helper.Hold); t.mock.restoreAll();
  }
});
test('HTTPS: streamed byte cap works without Content-Length', async t => {
  const block = Buffer.alloc(1024 * 1024, 1); block[0] = 0x1f; block[1] = 0x8b;
  mockHttp(t, { noLength: true, blocks: Array(33).fill(block) });
  await assert.rejects(helper.fetchArchive('2.20.0'), isCode('DOWNLOAD_SIZE'));
});
test('HTTPS: absolute and idle deadlines destroy request with fixed codes', async t => {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  let m = mockHttp(t, { hang: true });
  const check = assert.rejects(helper.fetchArchive('2.20.0'), isCode('DOWNLOAD_DEADLINE'));
  await new Promise(setImmediate); t.mock.timers.tick(helper.LIMITS.DEADLINE_MS + 1); await check;
  assert(m.requests[0].destroyed);
  t.mock.restoreAll();
  m = mockHttp(t, { hang: true });
  const idle = assert.rejects(helper.fetchArchive('2.20.0'), isCode('DOWNLOAD_IDLE_TIMEOUT'));
  await new Promise(setImmediate); m.requests[0].idleHandler(); await idle;
});
test('entrypoint: arbitrary argv refused; non-Linux FD execution honestly held', async () => {
  const argv = process.argv; process.argv = ['node', 'trusted.cjs', '--path=elsewhere'];
  try { await assert.rejects(helper.main(), isCode('NO_ARGUMENTS_ALLOWED')); } finally { process.argv = argv; }
  if (process.platform !== 'linux') await assert.rejects(helper.synchronize(), isCode('PLATFORM_UNQUALIFIED'));
});
test('preload wrapper: env clears NODE_OPTIONS and NODE_PATH BEFORE Node runs', () => {
  const f = temp(), marker = path.join(f.root, 'PRELOAD_MUST_NOT_RUN');
  const preload = path.join(f.root, 'hostile.cjs');
  fs.writeFileSync(preload, 'require("node:fs").writeFileSync(' + JSON.stringify(marker) + ',"bad");throw Error("PRELOAD_RAN")');
  const result = cp.spawnSync('/usr/bin/env', ['-u', 'NODE_OPTIONS', '-u', 'NODE_PATH', process.execPath,
    '--no-addons', '-e', CHILD], { encoding: 'utf8', timeout: 10000,
    env: { PATH: '/usr/bin:/bin', NODE_OPTIONS: '--require=' + preload, NODE_PATH: f.root } });
  assert.equal(result.status, 0); assert.equal(result.stdout, 'trusted-child-no-preload'); assert(!fs.existsSync(marker));
});
test('harness: no outside-temp writes or non-whitelisted child/network calls', () => {
  assert.throws(() => fs.writeFileSync(path.join(__dirname, 'must-not-exist'), 'bad'), /OUTSIDE_OWNED_TEMP/);
  assert.throws(() => cp.spawn('sh', ['-c', 'true']), /TEST_PROCESS_DENIED/);
  const socket = new net.Socket(); assert.throws(() => socket.connect(9, '127.0.0.1'), /TEST_NETWORK_DENIED/); socket.destroy();
});

const linuxOnly = { skip: process.platform !== 'linux' };
test('Linux FD: real atomic replacement/full-byte proof, source mode and no-op preserved', linuxOnly, async t => {
  const f = temp(); process.chdir(f.root); fs.writeFileSync(f.file, runtime()); fs.chmodSync(f.file, 0o600);
  const before = fs.statSync(f.file).ino; mockHttp(t);
  const result = await helper.synchronize(); assert(result.changed); assert.notEqual(fs.statSync(f.file).ino, before);
  assert.equal(fs.statSync(f.file).mode & 0o777, 0o600); assert.equal(fs.readFileSync(f.file).toString(), helper.transform(runtime(), digest()).toString());
  const calls = [];
  t.mock.restoreAll(); t.mock.method(https, 'get', () => { calls.push(1); throw Error('NOOP_NETWORK'); });
  const inode = fs.statSync(f.file).ino; assert.equal((await helper.synchronize()).changed, false);
  assert.equal(fs.statSync(f.file).ino, inode); assert.deepEqual(calls, []); assert.deepEqual(fs.readdirSync(f.image), ['Dockerfile']);
  const zero = Buffer.from(RECIPE.toString().replaceAll('2.19.3', '2.0.0'));
  fs.writeFileSync(f.file, zero); const zeroInode = fs.statSync(f.file).ino;
  assert.equal((await helper.synchronize()).changed, false);
  assert.equal(fs.statSync(f.file).ino, zeroInode); assert(fs.readFileSync(f.file).equals(zero)); assert.deepEqual(calls, []);
});
test('Linux FD: fetch failure never stages or changes source', linuxOnly, async t => {
  const f = temp(); process.chdir(f.root); const before = runtime(); fs.writeFileSync(f.file, before);
  mockHttp(t, { status: 403 }); await assert.rejects(helper.synchronize(), isCode('DOWNLOAD_STATUS'));
  assert(fs.readFileSync(f.file).equals(before)); assert.deepEqual(fs.readdirSync(f.image), ['Dockerfile']);
});
test('Linux FD: symlink file, hardlink file and symlink ancestor are refused', linuxOnly, async () => {
  const f = temp(); process.chdir(f.root); const outside = path.join(f.root, 'outside'); fs.writeFileSync(outside, RECIPE);
  fs.unlinkSync(f.file); fs.symlinkSync(outside, f.file); await assert.rejects(helper.synchronize(), helper.Hold);
  fs.unlinkSync(f.file); fs.linkSync(outside, f.file); await assert.rejects(helper.synchronize(), helper.Hold);
  fs.unlinkSync(f.file); fs.writeFileSync(f.file, RECIPE);
  const moved = path.join(f.root, 'moved'); fs.renameSync(f.image, moved); fs.symlinkSync(moved, f.image);
  await assert.rejects(helper.synchronize(), helper.Hold); assert(fs.readFileSync(outside).equals(RECIPE));
});
test('Linux FD: inode replacement during download is not overwritten', linuxOnly, async t => {
  const f = temp(); process.chdir(f.root); fs.writeFileSync(f.file, runtime());
  const replacement = path.join(f.image, 'replacement'); fs.writeFileSync(replacement, 'foreign replacement\n');
  mockHttp(t, { beforeEnd: () => fs.renameSync(replacement, f.file) });
  await assert.rejects(helper.synchronize(), helper.Hold); assert.equal(fs.readFileSync(f.file, 'utf8'), 'foreign replacement\n');
});
test('Linux FD: same-inode content drift is not overwritten', linuxOnly, async t => {
  const f = temp(); process.chdir(f.root); fs.writeFileSync(f.file, runtime());
  mockHttp(t, { beforeEnd: () => fs.writeFileSync(f.file, 'foreign changed bytes\n') });
  await assert.rejects(helper.synchronize(), helper.Hold); assert.equal(fs.readFileSync(f.file, 'utf8'), 'foreign changed bytes\n');
});
test('Linux FD: ancestor substitution is not followed', linuxOnly, async t => {
  const f = temp(); process.chdir(f.root); fs.writeFileSync(f.file, runtime());
  mockHttp(t, { beforeEnd: () => { fs.renameSync(f.image, path.join(f.root, 'old-image')); fs.mkdirSync(f.image); fs.writeFileSync(f.file, 'foreign directory\n'); } });
  await assert.rejects(helper.synchronize(), helper.Hold); assert.equal(fs.readFileSync(f.file, 'utf8'), 'foreign directory\n');
});
test('Linux FD: fsync failure before commit preserves original and removes only own stage', linuxOnly, async t => {
  const f = temp(); process.chdir(f.root); const before = runtime(); fs.writeFileSync(f.file, before); mockHttp(t);
  t.mock.method(fs, 'fsyncSync', () => { throw Error('TEST_ONLY_FSYNC'); });
  await assert.rejects(helper.synchronize(), helper.Hold); assert(fs.readFileSync(f.file).equals(before)); assert.deepEqual(fs.readdirSync(f.image), ['Dockerfile']);
});
test('Linux FD: source revalidated after staging, no descriptor leak', linuxOnly, async t => {
  const f = temp(); process.chdir(f.root); fs.writeFileSync(f.file, runtime()); mockHttp(t);
  const before = fs.readdirSync('/proc/self/fd').length, sync = fs.fsyncSync;
  t.mock.method(fs, 'fsyncSync', fd => { fs.writeFileSync(f.file, 'foreign staged edit\n'); sync(fd); });
  await assert.rejects(helper.synchronize(), helper.Hold); assert.equal(fs.readFileSync(f.file, 'utf8'), 'foreign staged edit\n');
  assert.deepEqual(fs.readdirSync(f.image), ['Dockerfile']); assert.equal(fs.readdirSync('/proc/self/fd').length, before);
});
test('Linux FD: oversized recipe and group/world-writable source refused', linuxOnly, async () => {
  const f = temp(); process.chdir(f.root); fs.writeFileSync(f.file, Buffer.alloc(helper.LIMITS.MAX_RECIPE + 1));
  await assert.rejects(helper.synchronize(), helper.Hold); fs.writeFileSync(f.file, RECIPE); fs.chmodSync(f.file, 0o666);
  await assert.rejects(helper.synchronize(), helper.Hold);
});

test.after(() => {
  process.chdir(OLD_CWD);
  for (const [name, fn] of Object.entries(original)) if (!name.startsWith('cp_')) fs[name] = fn;
  for (const name of ['spawn', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork']) cp[name] = original['cp_' + name];
  cp.spawnSync = originalSpawn; net.Socket.prototype.connect = originalConnect;
  // rmSync is restored only for the exact retained owned root, never user data.
  fs.rmSync(ROOT, { recursive: true, force: false });
  assert(!fs.existsSync(ROOT));
  console.log('owned_temp_retired=true; Linux FD qualification=' + (process.platform === 'linux' ? 'executed' : 'NOT_EXECUTED_UNQUALIFIED'));
});
