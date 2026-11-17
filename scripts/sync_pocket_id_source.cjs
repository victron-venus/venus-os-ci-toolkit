'use strict';
// Trusted read-only seed helper. No arguments or consumer code are executed.
const fs = require('node:fs');
const crypto = require('node:crypto');
const https = require('node:https');
const { performance } = require('node:perf_hooks');

const MAX_RECIPE = 65536;
const MAX_ARCHIVE = 32 * 1024 * 1024;
const DEADLINE_MS = 60000;
const IDLE_MS = 10000;
const COMPONENTS = ['deployments', 'pocket-id', 'image'];
const FILE = 'Dockerfile';
const VERSION = '([1-9][0-9]*\\.(?:0|[1-9][0-9]*)\\.(?:0|[1-9][0-9]*))';

class Hold extends Error {
  constructor(code) { super(code); this.name = 'Hold'; }
}
function need(value, code) { if (!value) throw new Hold(code); }
function versionTuple(version) {
  need(new RegExp('^' + VERSION + '$').test(version) && version.length <= 48, 'VERSION_FORMAT');
  const parts = version.split('.').map(value => BigInt(value));
  need(parts.every(value => value <= 9007199254740991n), 'VERSION_FORMAT');
  return parts;
}
function compareVersions(left, right) {
  const a = versionTuple(left), b = versionTuple(right);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1;
  return 0;
}
function archiveUrl(version) {
  versionTuple(version);
  return 'https://codeload.github.com/pocket-id/pocket-id/tar.gz/refs/tags/v' + version;
}
function validateUrl(value, expected) {
  let parsed;
  try { parsed = new URL(value); } catch { throw new Hold('DOWNLOAD_URL_REFUSED'); }
  need(value === expected && parsed.protocol === 'https:' && parsed.host === 'codeload.github.com'
    && parsed.username === '' && parsed.password === '' && parsed.port === ''
    && parsed.search === '' && parsed.hash === '', 'DOWNLOAD_URL_REFUSED');
}
async function fetchArchive(version) {
  const expected = archiveUrl(version);
  const deadline = performance.now() + DEADLINE_MS;
  let url = expected;
  for (let redirects = 0; redirects <= 1; redirects++) {
    validateUrl(url, expected);
    const remaining = deadline - performance.now();
    need(remaining > 0, 'DOWNLOAD_DEADLINE');
    const result = await new Promise((resolve, reject) => {
      let request, response, settled = false;
      const timer = setTimeout(() => finish(new Hold('DOWNLOAD_DEADLINE')), remaining);
      function finish(error, value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (response) response.destroy();
        if (request) request.destroy();
        if (error) reject(error); else resolve(value);
      }
      try {
        request = https.get(url, {
          agent: false,
          rejectUnauthorized: true,
          maxHeaderSize: 16384,
          highWaterMark: 65536,
          headers: { Accept: 'application/octet-stream', 'User-Agent': 'toolkit-pocket-id-source-sync/1' },
        }, incoming => {
          response = incoming;
          try {
            if ([301, 302, 303, 307, 308].includes(response.statusCode)) {
              need(redirects < 1 && typeof response.headers.location === 'string', 'DOWNLOAD_REDIRECT');
              const next = new URL(response.headers.location, url).href;
              validateUrl(next, expected);
              finish(null, { redirect: next });
              return;
            }
            need(response.statusCode === 200, 'DOWNLOAD_STATUS');
            const rawLength = response.headers['content-length'];
            let length;
            if (rawLength !== undefined) {
              need(typeof rawLength === 'string' && /^[0-9]+$/.test(rawLength), 'DOWNLOAD_LENGTH');
              length = Number(rawLength);
              need(Number.isSafeInteger(length) && length > 0 && length <= MAX_ARCHIVE, 'DOWNLOAD_SIZE');
            }
            let count = 0, prefix = Buffer.alloc(0);
            const hash = crypto.createHash('sha256');
            response.on('data', block => {
              if (settled) return;
              try {
                need(performance.now() < deadline, 'DOWNLOAD_DEADLINE');
                need(Buffer.isBuffer(block), 'DOWNLOAD_BYTES');
                count += block.length;
                need(count <= MAX_ARCHIVE, 'DOWNLOAD_SIZE');
                if (prefix.length < 2) prefix = Buffer.concat([prefix, block.subarray(0, 2 - prefix.length)]);
                hash.update(block);
              } catch (error) { finish(error instanceof Hold ? error : new Hold('DOWNLOAD_FAILED')); }
            });
            response.on('end', () => {
              if (settled) return;
              try {
                need(performance.now() < deadline, 'DOWNLOAD_DEADLINE');
                need(count > 0 && prefix.equals(Buffer.from([0x1f, 0x8b])), 'DOWNLOAD_GZIP_HEADER');
                need(length === undefined || count === length, 'DOWNLOAD_LENGTH_MISMATCH');
                finish(null, { sha256: hash.digest('hex'), bytes: count });
              } catch (error) { finish(error instanceof Hold ? error : new Hold('DOWNLOAD_FAILED')); }
            });
            response.on('aborted', () => finish(new Hold('DOWNLOAD_TRUNCATED')));
            response.on('error', () => finish(new Hold('DOWNLOAD_FAILED')));
          } catch (error) { finish(error instanceof Hold ? error : new Hold('DOWNLOAD_FAILED')); }
        });
        request.on('error', () => finish(new Hold('DOWNLOAD_FAILED')));
        request.setTimeout(Math.min(IDLE_MS, Math.max(1, deadline - performance.now())),
          () => finish(new Hold('DOWNLOAD_IDLE_TIMEOUT')));
      } catch { finish(new Hold('DOWNLOAD_FAILED')); }
    });
    if (!result.redirect) return result;
    url = result.redirect;
  }
  throw new Hold('DOWNLOAD_REDIRECT');
}
function escapeRegex(value) { return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function parseRecipe(raw) {
  need(Buffer.isBuffer(raw) && raw.length > 0 && raw.length <= MAX_RECIPE, 'RECIPE_SIZE');
  const text = raw.toString('utf8');
  need(Buffer.from(text, 'utf8').equals(raw) && !text.includes('\r') && !text.includes('\0'), 'RECIPE_ENCODING');
  function one(pattern, marker, code) {
    const matches = [...text.matchAll(new RegExp(pattern, 'gmd'))];
    need(text.split(marker).length === 2 && matches.length === 1, code);
    return matches[0];
  }
  const source = one('^ADD --checksum=sha256:([0-9a-f]{64}) '
    + 'https://codeload\\.github\\.com/pocket-id/pocket-id/tar\\.gz/refs/tags/v'
    + VERSION + ' /tmp/source\\.tar\\.gz$',
  'https://codeload.github.com/pocket-id/pocket-id/', 'SOURCE_LAYOUT');
  need([...text.matchAll(/^ADD\s/gm)].length === 1, 'SOURCE_LAYOUT');
  const runtime = one('^FROM ghcr\\.io/pocket-id/pocket-id:v' + VERSION
    + '-distroless@sha256:[0-9a-f]{64}$', 'FROM ghcr.io/pocket-id/pocket-id:', 'RUNTIME_LAYOUT');
  const froms = [...text.matchAll(/^FROM .+$/gm)].map(value => value[0]);
  need(froms.at(-1) === runtime[0], 'FINAL_STAGE');
  const final = text.slice(runtime.indices[0][1]);
  need(JSON.stringify([...final.matchAll(/^USER (.+)$/gm)].map(value => value[1]))
    === JSON.stringify(['65532']), 'FINAL_USER');
  const build = one('-buildid=([^\\s\'\"]+)', '-buildid=', 'BUILD_ID_LAYOUT');
  const compiled = one('-X github\\.com/pocket-id/pocket-id/backend/internal/common\\.Version=([^\\s\'\"]+)',
    'github.com/pocket-id/pocket-id/backend/internal/common.Version=', 'COMPILED_LAYOUT');
  const label = one('org\\.opencontainers\\.image\\.version="([^"\\n]+)"',
    'org.opencontainers.image.version=', 'LABEL_LAYOUT');
  const description = one('org\\.opencontainers\\.image\\.description="Pocket ID ' + VERSION + ' [^"\\n]+"',
    'org.opencontainers.image.description=', 'DESCRIPTION_LAYOUT');
  const oldVersion = source[2], newVersion = runtime[1], identity = build[1];
  versionTuple(oldVersion); versionTuple(newVersion);
  need(new RegExp('^' + escapeRegex(oldVersion) + '-security\\.[1-9][0-9]*$').test(identity), 'OLD_BUILD_ID_MISMATCH');
  need(compiled[1] === identity && label[1] === identity && description[1] === oldVersion, 'OLD_IDENTITY_MISMATCH');
  need(compareVersions(newVersion, oldVersion) >= 0, 'ROLLBACK_REFUSED');
  return { text, oldVersion, newVersion, spans: {
    digest: source.indices[1], source: source.indices[2], build: build.indices[1],
    compiled: compiled.indices[1], label: label.indices[1], description: description.indices[1],
  } };
}
function transform(raw, digest) {
  const parsed = parseRecipe(raw);
  need(parsed.oldVersion !== parsed.newVersion, 'TRANSFORM_NO_VERSION_CHANGE');
  need(typeof digest === 'string' && /^[0-9a-f]{64}$/.test(digest), 'ARCHIVE_DIGEST_FORMAT');
  const next = parsed.newVersion + '-security.1';
  const values = { digest, source: parsed.newVersion, build: next, compiled: next, label: next, description: parsed.newVersion };
  let text = parsed.text;
  for (const key of Object.keys(parsed.spans).sort((a, b) => parsed.spans[b][0] - parsed.spans[a][0])) {
    const [start, end] = parsed.spans[key];
    text = text.slice(0, start) + values[key] + text.slice(end);
  }
  const output = Buffer.from(text, 'utf8'), verified = parseRecipe(output);
  need(verified.oldVersion === verified.newVersion && verified.newVersion === parsed.newVersion, 'RESULT_IDENTITY');
  return output;
}
function inode(st) { return [st.dev, st.ino].map(String).join(':'); }
function identity(st) {
  return [st.dev, st.ino, st.uid, st.gid, st.mode, st.nlink, st.size, st.mtimeNs, st.ctimeNs].map(String).join(':');
}
function directory(st) {
  need(st.isDirectory() && st.uid === BigInt(process.geteuid()) && (st.mode & 0o022n) === 0n, 'DIRECTORY_CUSTODY');
}
function file(st) {
  need(st.isFile() && st.uid === BigInt(process.geteuid()) && st.nlink === 1n
    && (st.mode & 0o7111n) === 0n && (st.mode & 0o022n) === 0n, 'FILE_CUSTODY');
  need(st.size > 0n && st.size <= BigInt(MAX_RECIPE), 'RECIPE_SIZE');
}
function at(fd, name) { return '/proc/self/fd/' + fd + '/' + name; }
function fstat(fd) { return fs.fstatSync(fd, { bigint: true }); }
function lstat(path) { return fs.lstatSync(path, { bigint: true }); }
function readBounded(fd) {
  const buffer = Buffer.alloc(MAX_RECIPE + 1);
  let count = 0;
  while (count < buffer.length) {
    const read = fs.readSync(fd, buffer, count, buffer.length - count, count);
    if (!read) break;
    count += read;
  }
  need(count > 0 && count <= MAX_RECIPE, 'RECIPE_SIZE');
  return buffer.subarray(0, count);
}
async function synchronize() {
  need(process.platform === 'linux', 'PLATFORM_UNQUALIFIED');
  need(Number.isInteger(fs.constants.O_NOFOLLOW) && Number.isInteger(fs.constants.O_DIRECTORY), 'FD_INTERFACE');
  const dirs = [], links = [], flags = fs.constants;
  let source, stage, stageName, stageInode, replaced = false;
  try {
    const root = fs.openSync('.', flags.O_RDONLY | flags.O_DIRECTORY | flags.O_NOFOLLOW);
    dirs.push(root); directory(fstat(root));
    for (const name of COMPONENTS) {
      const parent = dirs.at(-1), fd = fs.openSync(at(parent, name), flags.O_RDONLY | flags.O_DIRECTORY | flags.O_NOFOLLOW);
      dirs.push(fd); directory(fstat(fd)); links.push({ parent, name, fd, inode: inode(fstat(fd)) });
    }
    const image = dirs.at(-1), target = at(image, FILE);
    source = fs.openSync(target, flags.O_RDONLY | flags.O_NOFOLLOW);
    const before = fstat(source); file(before);
    const original = readBounded(source);
    need(identity(fstat(source)) === identity(before), 'SOURCE_READ_DRIFT');
    const parsed = parseRecipe(original);
    function revalidate() {
      directory(fstat(root));
      for (const link of links) {
        const entry = lstat(at(link.parent, link.name)); directory(entry);
        need(inode(entry) === link.inode && inode(fstat(link.fd)) === link.inode, 'DIRECTORY_DRIFT');
      }
      const current = lstat(target); file(current);
      need(identity(current) === identity(before) && identity(fstat(source)) === identity(before), 'SOURCE_IDENTITY_DRIFT');
      need(readBounded(source).equals(original), 'SOURCE_BYTES_DRIFT');
    }
    revalidate();
    if (parsed.oldVersion === parsed.newVersion) return { status: 'already-coherent', version: parsed.newVersion, changed: false };
    const archive = await fetchArchive(parsed.newVersion);
    const updated = transform(original, archive.sha256);
    revalidate();
    stageName = '.pocket-id-sync.' + crypto.randomBytes(12).toString('hex');
    const stagePath = at(image, stageName);
    stage = fs.openSync(stagePath, flags.O_RDWR | flags.O_CREAT | flags.O_EXCL | flags.O_NOFOLLOW, 0o600);
    stageInode = inode(fstat(stage));
    fs.fchmodSync(stage, Number(before.mode & 0o777n));
    if (fstat(stage).gid !== before.gid) fs.fchownSync(stage, -1, Number(before.gid));
    for (let offset = 0; offset < updated.length;) {
      const count = fs.writeSync(stage, updated, offset, updated.length - offset, offset);
      need(count > 0, 'STAGE_WRITE'); offset += count;
    }
    fs.fsyncSync(stage);
    const staged = fstat(stage); file(staged);
    need(readBounded(stage).equals(updated), 'STAGE_BYTES');
    revalidate();
    need(identity(lstat(stagePath)) === identity(staged) && inode(staged) === stageInode, 'STAGE_IDENTITY_DRIFT');
    fs.renameSync(stagePath, target); replaced = true;
    fs.fsyncSync(image);
    const after = lstat(target); file(after);
    need(inode(after) === stageInode && after.size === BigInt(updated.length), 'RESULT_FILE_IDENTITY');
    const resultFd = fs.openSync(target, flags.O_RDONLY | flags.O_NOFOLLOW);
    try { need(readBounded(resultFd).equals(updated), 'RESULT_FILE_BYTES'); } finally { fs.closeSync(resultFd); }
    return { status: 'synchronized', old_version: parsed.oldVersion, version: parsed.newVersion,
      archive_sha256: archive.sha256, archive_bytes: archive.bytes, changed: true };
  } catch (error) {
    if (error instanceof Hold) throw error;
    throw new Hold('FILESYSTEM_REFUSED');
  } finally {
    let cleanupFailed = false;
    if (stageName && !replaced && dirs.length) {
      try {
        const path = at(dirs.at(-1), stageName);
        if (stageInode && inode(lstat(path)) === stageInode) fs.unlinkSync(path);
      } catch (error) { if (error.code !== 'ENOENT') cleanupFailed = true; }
    }
    for (const fd of [stage, source, ...dirs.reverse()]) if (fd !== undefined) {
      try { fs.closeSync(fd); } catch { cleanupFailed = true; }
    }
    if (cleanupFailed) throw new Hold('CLEANUP_FAILED');
  }
}
async function main() {
  need(process.argv.length === 2, 'NO_ARGUMENTS_ALLOWED');
  return synchronize();
}
module.exports = { Hold, archiveUrl, validateUrl, parseRecipe, transform, fetchArchive, synchronize, main,
  LIMITS: Object.freeze({ MAX_RECIPE, MAX_ARCHIVE, DEADLINE_MS, IDLE_MS }) };
if (require.main === module) main().then(value => {
  process.stdout.write(JSON.stringify(value) + '\n');
}).catch(error => {
  process.stderr.write(JSON.stringify({ status: 'held', code: error instanceof Hold ? error.message : 'UNEXPECTED_FAILURE' }) + '\n');
  process.exitCode = 1;
});
