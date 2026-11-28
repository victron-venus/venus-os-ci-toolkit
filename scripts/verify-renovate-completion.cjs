#!/usr/bin/env node
'use strict';

// Qualify one completed Renovate 44.215.0 producer, not a whole workflow.
// The caller owns the immutable selected roster, production-mode/run binding,
// a fresh LOG_FILE, LOG_FILE_FORMAT=json, LOG_FILE_LEVEL=info and process exit.
const fs = require('node:fs');
const crypto = require('node:crypto');
const { TextDecoder } = require('node:util');

const VERSION = '44.215.0';
const LIMITS = Object.freeze({ bytes: 256 * 1024 * 1024, line: 4 * 1024 * 1024,
  records: 2_000_000, repositories: 256, depth: 64, fields: 100_000 });
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

class CompletionError extends Error {
  constructor(code, record = 0) {
    super(code);
    this.code = code;
    this.record = record;
  }
}
function reject(code, record = 0) { throw new CompletionError(code, record); }

function validateRoster(repositories) {
  if (!Array.isArray(repositories) || repositories.length === 0 ||
      repositories.length > LIMITS.repositories) reject('INVALID_ROSTER');
  const names = new Set();
  for (const repository of repositories) {
    if (typeof repository !== 'string' ||
        !/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9_.-]{1,100}$/.test(repository) ||
        names.has(repository)) reject('INVALID_ROSTER');
    names.add(repository);
  }
  return names;
}

// Inspect fields, never arbitrary message substrings: e.g. a configured
// suppressNotifications:["artifactErrors"] is not an actual artifact error.
function inspectFields(record, number) {
  const pending = [[record, 0]];
  let fields = 0;
  while (pending.length) {
    const [value, depth] = pending.pop();
    if (value === null || typeof value !== 'object') continue;
    if (depth > LIMITS.depth) reject('RECORD_DEPTH_LIMIT', number);
    for (const [key, child] of Object.entries(value)) {
      if (++fields > LIMITS.fields) reject('RECORD_FIELD_LIMIT', number);
      if (key === 'artifactErrors' && child !== null &&
          (!Array.isArray(child) || child.length !== 0)) reject('ARTIFACT_ERRORS', number);
      pending.push([child, depth + 1]);
    }
  }
}

function validateRecords(records, repositories) {
  const expected = validateRoster(repositories);
  if (records == null || typeof records === 'string' ||
      typeof records[Symbol.iterator] !== 'function') reject('INVALID_RECORDS');
  const started = new Set();
  const finished = new Set();
  let count = 0;
  let warnings = 0;
  for (const record of records) {
    if (++count > LIMITS.records) reject('RECORD_COUNT_LIMIT');
    if (!record || typeof record !== 'object' || Array.isArray(record) ||
        record.name !== 'renovate' || record.v !== 0 ||
        ![10, 20, 30, 40, 50, 60].includes(record.level) ||
        typeof record.msg !== 'string' || typeof record.time !== 'string' ||
        !Number.isFinite(Date.parse(record.time))) reject('INVALID_RECORD', count);
    if (record.level >= 50 || ['error', 'fatal'].includes(record.oldLevel)) {
      reject('ERROR_OR_FATAL', count);
    }
    if (record.level === 40) warnings++;
    inspectFields(record, count);
    if (record.msg === 'artifactErrors') reject('ARTIFACT_ERRORS', count);
    if (record.msg === 'Artifact generation failed before PR decision' ||
        (own(record, 'artifactErrorCount') && record.artifactErrorCount !== 0)) {
      reject('ARTIFACT_ERRORS', count);
    }
    if (record.msg === 'Error updating branch' ||
        record.msg === 'Error updating branch: update failure') reject('BRANCH_ERROR', count);
    if (record.msg.startsWith('DRY-RUN:')) reject('DRY_RUN', count);
    if (own(record, 'repository') && !expected.has(record.repository)) {
      reject('UNEXPECTED_REPOSITORY', count);
    }
    if (record.msg === 'Repository started') {
      if (!expected.has(record.repository)) reject('MISSING_REPOSITORY', count);
      if (started.has(record.repository)) reject('DUPLICATE_START', count);
      if (record.renovateVersion !== VERSION) reject('VERSION_MISMATCH', count);
      started.add(record.repository);
    } else if (record.msg === 'Repository finished') {
      if (!expected.has(record.repository)) reject('MISSING_REPOSITORY', count);
      if (!started.has(record.repository)) reject('FINISH_WITHOUT_START', count);
      if (finished.has(record.repository)) reject('DUPLICATE_FINISH', count);
      // "done" can coexist with warn-level artifactErrors; both are checked.
      // Disabled/onboarding/error/unknown/missing result never earn full credit.
      if (record.result !== 'done' ||
          !['activated', 'onboarded'].includes(record.status) ||
          record.enabled !== true || record.onboarded !== true ||
          record.exitCode !== 0) reject('UNSUCCESSFUL_FINISH', count);
      finished.add(record.repository);
    }
  }
  if (started.size !== expected.size || finished.size !== expected.size) {
    reject('INCOMPLETE_ROSTER');
  }
  return { qualified: true, renovateVersion: VERSION, expectedCount: expected.size,
    startedCount: started.size, finishedCount: finished.size, recordCount: count,
    warningCount: warnings, repositories: [...repositories] };
}

function verifyFile(file, repositories) {
  validateRoster(repositories);
  let fd;
  try {
    // O_NONBLOCK also avoids waiting forever on a FIFO before fstat rejects it.
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW |
      fs.constants.O_NONBLOCK);
    const before = fs.fstatSync(fd);
    if (!before.isFile() || before.size === 0) reject('INVALID_LOG_FILE');
    if (before.size > LIMITS.bytes) reject('FILE_SIZE_LIMIT');
    const digest = crypto.createHash('sha256');
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let bytes = 0;
    function* records() {
      const chunk = Buffer.alloc(64 * 1024);
      let pending = Buffer.alloc(0);
      let number = 0;
      function parse(line) {
        if (line.length === 0 || line.length > LIMITS.line) reject('LINE_SIZE_LIMIT', number + 1);
        let text;
        try { text = decoder.decode(line); } catch { reject('INVALID_UTF8', number + 1); }
        try { return JSON.parse(text); } catch { reject('INVALID_JSON', number + 1); }
      }
      for (;;) {
        const length = fs.readSync(fd, chunk, 0, chunk.length, null);
        if (length === 0) break;
        bytes += length;
        if (bytes > LIMITS.bytes) reject('FILE_SIZE_LIMIT');
        digest.update(chunk.subarray(0, length));
        pending = Buffer.concat([pending, chunk.subarray(0, length)]);
        let offset = 0;
        for (;;) {
          const end = pending.indexOf(10, offset);
          if (end < 0) break;
          yield parse(pending.subarray(offset, end));
          number++;
          offset = end + 1;
        }
        pending = pending.subarray(offset);
        if (pending.length > LIMITS.line) reject('LINE_SIZE_LIMIT', number + 1);
      }
      // Bunyan's synchronous stream writes a complete JSON line plus newline.
      // Even syntactically valid unterminated trailing data is not a receipt.
      if (pending.length !== 0) reject('TRUNCATED_LOG', number + 1);
      const after = fs.fstatSync(fd);
      if (bytes !== before.size || after.size !== before.size ||
          after.mtimeMs !== before.mtimeMs || after.ctimeMs !== before.ctimeMs) {
        reject('LOG_CHANGED_DURING_READ');
      }
    }
    const result = validateRecords(records(), repositories);
    return { ...result, logBytes: bytes, logSha256: digest.digest('hex') };
  } catch (error) {
    if (error instanceof CompletionError) throw error;
    // Never echo a path, JSON fragment, exception stack or logged credentials.
    reject('LOG_READ_FAILED');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

module.exports = { validateRecords, verifyFile, CompletionError, LIMITS };

function readRosterFile(file) {
  let fd;
  try {
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
    const before = fs.fstatSync(fd);
    if (!before.isFile() || before.size === 0 || before.size > 64 * 1024) reject('INVALID_ROSTER_FILE');
    const bytes = Buffer.alloc(before.size + 1);
    const length = fs.readSync(fd, bytes, 0, bytes.length, null);
    const after = fs.fstatSync(fd);
    if (length !== before.size || before.size !== after.size ||
        before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) reject('ROSTER_CHANGED_DURING_READ');
    const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes.subarray(0, length));
    const roster = JSON.parse(text);
    validateRoster(roster);
    return roster;
  } catch (error) {
    if (error instanceof CompletionError) throw error;
    reject('ROSTER_READ_FAILED');
  } finally {
    if (fd !== undefined) fs.closeSync(fd);
  }
}

if (require.main === module) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 2) reject('USAGE_LOG_FILE_AND_ROSTER_JSON_FILE');
    process.stdout.write(JSON.stringify(verifyFile(args[0], readRosterFile(args[1]))) + '\n');
  } catch (error) {
    process.stderr.write(JSON.stringify({ qualified: false,
      code: error instanceof CompletionError ? error.code : 'VERIFICATION_FAILED',
      record: error instanceof CompletionError ? error.record : 0 }) + '\n');
    process.exitCode = 1;
  }
}
