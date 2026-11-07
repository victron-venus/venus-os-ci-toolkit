'use strict';
const assert = require('node:assert/strict');
const { test } = require('node:test');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const { validateRecords, verifyFile, LIMITS } = require('./verify-renovate-completion.cjs');

const roster = ['owner/first', 'owner/second'];
const record = (fields = {}) => ({ name: 'renovate', v: 0, level: 30,
  time: '2026-11-07T18:00:00.000Z', msg: 'Global notice', ...fields });
const start = (repository) => record({ repository, msg: 'Repository started', renovateVersion: '44.215.0' });
const finish = (repository) => record({ repository, msg: 'Repository finished',
  result: 'done', status: 'activated', enabled: true, onboarded: true, exitCode: 0 });
const success = () => roster.flatMap((repository) => [start(repository), finish(repository)]);
function rejected(records, code, selected = roster) {
  assert.throws(() => validateRecords(records, selected), (error) => error.code === code);
}
function fileCase(callback) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'renovate-completion-test-'));
  try { callback(path.join(directory, 'log.jsonl'), directory); }
  finally { fs.rmSync(directory, { recursive: true }); }
}
const encode = (records) => records.map((value) => JSON.stringify(value) + '\n').join('');

test('exact production roster completes with bounded metadata only', () => {
  const value = validateRecords([record(), ...success()], roster);
  assert.deepEqual(value, { qualified: true, renovateVersion: '44.215.0', expectedCount: 2,
    startedCount: 2, finishedCount: 2, recordCount: 5, warningCount: 0, repositories: roster });
});
test('intentional warnings and config strings do not masquerade as artifact errors', () => {
  const values = success();
  values.splice(1, 0, record({ repository: roster[0], level: 40, msg: 'Policy warning',
    config: { suppressNotifications: ['artifactErrors'], artifactErrors: [] } }));
  values[2].status = 'onboarded';
  assert.equal(validateRecords(values, roster).warningCount, 1);
});
test('targeted selection qualifies only that exact roster', () => {
  const values = [start(roster[0]), finish(roster[0])];
  assert.equal(validateRecords(values, [roster[0]]).expectedCount, 1);
  rejected(values, 'INCOMPLETE_ROSTER');
});
test('exit zero / done never hides the observed warn-level lockfile error', () => {
  const values = success();
  values.splice(1, 0, record({ repository: roster[0], level: 40, msg: 'artifactErrors',
    artifactErrors: [{ fileName: 'research/uv.lock', stderr: 'sensitive command output' }] }));
  rejected(values, 'ARTIFACT_ERRORS');
});
test('nested and malformed artifact errors fail, without interpreting arbitrary strings', () => {
  for (const value of [[{}], 'not an array', {}, true, 1]) {
    rejected([record({ details: { artifactErrors: value } }), ...success()], 'ARTIFACT_ERRORS');
  }
  rejected([record({ msg: 'artifactErrors' }), ...success()], 'ARTIFACT_ERRORS');
});
test('pre-PR count-only hook rejects a failed lockfile even without a PR', () => {
  rejected([record({ level: 40, artifactErrorCount: 1,
    msg: 'Artifact generation failed before PR decision' }), ...success()], 'ARTIFACT_ERRORS');
  rejected([record({ artifactErrorCount: '0' }), ...success()], 'ARTIFACT_ERRORS');
});
test('global, repository and trailing error/fatal records fail closed', () => {
  for (const level of [50, 60]) {
    rejected([record({ level }), ...success()], 'ERROR_OR_FATAL');
    rejected([...success(), record({ level, repository: roster[0] })], 'ERROR_OR_FATAL');
  }
  for (const oldLevel of ['error', 'fatal']) {
    rejected([record({ level: 30, oldLevel }), ...success()], 'ERROR_OR_FATAL');
  }
});
test('known warn-level branch failures do not earn completion credit', () => {
  for (const msg of ['Error updating branch', 'Error updating branch: update failure']) {
    rejected([record({ level: 40, msg }), ...success()], 'BRANCH_ERROR');
  }
});
test('dry-run records fail even if a synthetic finish says done', () => {
  rejected([record({ msg: 'DRY-RUN: Would create PR' }), ...success()], 'DRY_RUN');
});
test('missing repository, unknown repository and missing finish fail', () => {
  rejected([], 'INCOMPLETE_ROSTER');
  rejected(success().slice(0, -1), 'INCOMPLETE_ROSTER');
  rejected([record({ repository: 'attacker/other' }), ...success()], 'UNEXPECTED_REPOSITORY');
  const value = start(roster[0]); delete value.repository;
  rejected([value, ...success()], 'MISSING_REPOSITORY');
});
test('duplicates, restart, finish-before-start and concatenated logs fail', () => {
  rejected([start(roster[0]), ...success()], 'DUPLICATE_START');
  rejected([...success(), finish(roster[0])], 'DUPLICATE_FINISH');
  rejected([finish(roster[0]), ...success()], 'FINISH_WITHOUT_START');
  rejected([...success(), ...success()], 'DUPLICATE_START');
});
test('every unsuccessful or omitted finish field fails', () => {
  const mutations = { result: [undefined, 'error', 'automerged', 'disabled-by-config', 'onboarding', 'unknown'],
    status: [undefined, 'disabled', 'onboarding', 'unknown'], enabled: [undefined, false],
    onboarded: [undefined, false], exitCode: [undefined, '0', 1, 7] };
  for (const [key, values] of Object.entries(mutations)) for (const value of values) {
    const records = success(); records[1][key] = value;
    rejected(records, 'UNSUCCESSFUL_FINISH');
  }
});
test('wrong or missing installed version fails', () => {
  for (const renovateVersion of [undefined, '44.214.0', '44.216.0']) {
    const values = success(); values[0].renovateVersion = renovateVersion;
    rejected(values, 'VERSION_MISMATCH');
  }
});
test('malformed roster and schema fail without caller-controlled error detail', () => {
  for (const selected of [null, [], [roster[0], roster[0]], ['bad\nsecret'], [3]]) {
    rejected(success(), 'INVALID_ROSTER', selected);
  }
  for (const value of [null, [], 'secret', {}, record({ name: 'forged' }),
    record({ level: 'error' }), record({ time: 'bad' }), record({ v: 1 })]) {
    rejected([value, ...success()], 'INVALID_RECORD');
  }
});
test('deep structured payload is bounded', () => {
  const root = {}; let current = root;
  for (let i = 0; i < 70; i++) current = current.child = {};
  rejected([record({ root }), ...success()], 'RECORD_DEPTH_LIMIT');
});
test('file verifier hashes actual bytes and accepts chunk-spanning valid JSON', () => fileCase((file) => {
  const body = encode([record({ padding: 'x'.repeat(70_000) }), ...success()]);
  fs.writeFileSync(file, body);
  const value = verifyFile(file, roster);
  assert.equal(value.logSha256, crypto.createHash('sha256').update(body).digest('hex'));
  assert.equal(value.logBytes, Buffer.byteLength(body));
  assert.equal(value.finishedCount, 2);
}));
test('empty, malformed, truncated and internal blank input cannot qualify', () => fileCase((file) => {
  for (const [body, code] of [['', 'INVALID_LOG_FILE'], ['secret\n', 'INVALID_JSON'],
    [encode(success()).slice(0, -1), 'TRUNCATED_LOG'], [encode(success()) + '\n', 'LINE_SIZE_LIMIT'],
    [encode(success()) + '{', 'TRUNCATED_LOG']]) {
    fs.writeFileSync(file, body);
    assert.throws(() => verifyFile(file, roster), (error) => error.code === code);
  }
}));
test('non-UTF8 data and oversized line or file fail with fixed codes', () => fileCase((file) => {
  fs.writeFileSync(file, Buffer.from([0xff, 10]));
  assert.throws(() => verifyFile(file, roster), { code: 'INVALID_UTF8' });
  fs.writeFileSync(file, 'x'.repeat(LIMITS.line + 1) + '\n');
  assert.throws(() => verifyFile(file, roster), { code: 'LINE_SIZE_LIMIT' });
  fs.truncateSync(file, LIMITS.bytes + 1);
  assert.throws(() => verifyFile(file, roster), { code: 'FILE_SIZE_LIMIT' });
}));
test('symlinks, directories and nonexistent paths fail closed', () => fileCase((file, directory) => {
  fs.writeFileSync(file, encode(success()));
  const link = path.join(directory, 'link'); fs.symlinkSync(file, link);
  for (const value of [link, directory, path.join(directory, 'absent')]) {
    assert.throws(() => verifyFile(value, roster));
  }
}));
test('CLI emits bounded structured metadata and never raw payloads on failure', () => fileCase((file, directory) => {
  const script = path.join(__dirname, 'verify-renovate-completion.cjs');
  const selected = path.join(directory, 'selected.json'); fs.writeFileSync(selected, JSON.stringify(roster));
  fs.writeFileSync(file, encode(success()));
  const valid = spawnSync(process.execPath, [script, file, selected], { encoding: 'utf8' });
  assert.equal(valid.status, 0); assert.equal(JSON.parse(valid.stdout).qualified, true);
  const sentinel = 'DO_NOT_PRINT_token_or_command_payload';
  fs.writeFileSync(file, encode([...success(), record({ level: 50, msg: sentinel })]));
  const invalid = spawnSync(process.execPath, [script, file, selected], { encoding: 'utf8' });
  assert.equal(invalid.status, 1); assert.equal(invalid.stdout, '');
  assert.deepEqual(JSON.parse(invalid.stderr), { qualified: false, code: 'ERROR_OR_FATAL', record: 5 });
  assert.ok(!invalid.stderr.includes(sentinel)); assert.ok(!invalid.stderr.includes(file));
}));
test('CLI roster must be a bounded regular JSON array file, never literal JSON or symlink', () => fileCase((file, directory) => {
  const script = path.join(__dirname, 'verify-renovate-completion.cjs');
  const selected = path.join(directory, 'selected.json'); const link = path.join(directory, 'selected-link');
  fs.writeFileSync(file, encode(success())); fs.writeFileSync(selected, JSON.stringify(roster)); fs.symlinkSync(selected, link);
  for (const input of [JSON.stringify(roster), link, directory]) {
    assert.equal(spawnSync(process.execPath, [script, file, input]).status, 1);
  }
  for (const body of ['{}', '[]', 'not-json', JSON.stringify([roster[0], roster[0]]), 'x'.repeat(64 * 1024 + 1)]) {
    fs.writeFileSync(selected, body);
    const result = spawnSync(process.execPath, [script, file, selected], { encoding: 'utf8' });
    assert.equal(result.status, 1); assert.equal(result.stdout, ''); assert.ok(!result.stderr.includes(selected));
  }
}));
