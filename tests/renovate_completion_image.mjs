// Credential-free native logger/wrapper contract inside the existing official image.
import assert from 'node:assert/strict';
import { randomBytes } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join } from 'node:path';
assert.equal(process.platform, 'linux');
const node = '/usr/local/renovate/node';
const root = `/tmp/renovate-log-contract-${randomBytes(16).toString('hex')}`;
mkdirSync(root, { mode: 0o700, recursive: false });
const roster = join(root, 'roster.json');
const proof = join(root, 'native-path.json');
const fixture = join(root, 'native-logger.mjs');
try {
  writeFileSync(roster, JSON.stringify(['fixture/repository']));
  writeFileSync(
    fixture,
    `
import {writeFileSync,statSync} from 'node:fs';
import {getEnv} from '/usr/local/renovate/dist/logger/utils.js';
import {init,addMeta,logger} from '/usr/local/renovate/dist/logger/index.js';
await init();
writeFileSync(${JSON.stringify(proof)},JSON.stringify({literal:process.env.LOG_FILE,selected:getEnv('LOG_FILE'),format:getEnv('LOG_FILE_FORMAT'),level:getEnv('LOG_FILE_LEVEL'),mode:statSync(process.env.LOG_FILE.slice(0,process.env.LOG_FILE.lastIndexOf('/'))).mode&0o777}));
addMeta({repository:'fixture/repository'});
logger.info({renovateVersion:'44.215.0'},'Repository started');
logger.info({result:'done',status:'activated',enabled:true,onboarded:true,exitCode:0},'Repository finished');
`,
  );
  const source = readFileSync(
    '/repo/scripts/renovate-security-runner.sh',
    'utf8',
  );
  const sourceWithFixture = source
    .replace('/usr/local/renovate/dist/renovate.js', fixture)
    .replace(
      'compat_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)',
      'compat_dir=/repo/scripts',
    )
    .replace('/github-action/renovate-repositories.json', roster);
  const env = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) => !key.startsWith('LOG_') && !key.startsWith('RENOVATE_LOG_'),
    ),
  );
  env.LOG_LEVEL = 'fatal';
  env.RENOVATE_DRY_RUN = 'false';
  const execute = (name, script, extra = {}) => {
    const file = join(root, name + '.sh');
    writeFileSync(file, script);
    return spawnSync('/bin/bash', [file], {
      encoding: 'utf8',
      timeout: 30000,
      maxBuffer: 1024 * 1024,
      env: { ...env, ...extra },
    });
  };
  // Use the real old mktemp operation with a deterministic uppercase prefix.
  // The native logger, filesystem, and verifier are not replaced or mocked.
  const creation =
    /completion_dir=\$\(\/usr\/local\/renovate\/node -e '[\s\S]+?\n'\)/;
  assert.equal((sourceWithFixture.match(creation) || []).length, 1);
  const original = sourceWithFixture.replace(
    creation,
    `completion_dir=$(umask 077; mktemp -d '${root}/Old.XXXXXXXX')`,
  );
  const old = execute('old', original);
  assert.equal(old.error, undefined);
  assert.equal(old.status, 1);
  assert.equal(JSON.parse(old.stderr.trim()).code, 'LOG_READ_FAILED');
  const oldProof = JSON.parse(readFileSync(proof, 'utf8'));
  assert.notEqual(oldProof.literal, oldProof.selected);
  assert.equal(oldProof.selected, oldProof.literal.toLowerCase());
  assert(!existsSync(oldProof.literal));
  assert(existsSync(oldProof.selected));
  const unrelated = join(root, 'unrelated.jsonl');
  writeFileSync(unrelated, 'foreign fixture stays unchanged');
  const current = execute('current', sourceWithFixture, {
    RENOVATE_LOG_FILE: unrelated,
    RENOVATE_LOG_FILE_FORMAT: 'pretty',
    RENOVATE_LOG_FILE_LEVEL: 'fatal',
  });
  assert.equal(current.error, undefined);
  assert.equal(current.status, 0, current.stderr);
  const receipt = JSON.parse(current.stdout.trim());
  assert.equal(receipt.qualified, true);
  assert.equal(receipt.startedCount, 1);
  assert.equal(receipt.finishedCount, 1);
  const currentProof = JSON.parse(readFileSync(proof, 'utf8'));
  assert.equal(currentProof.literal, currentProof.selected);
  assert.equal(currentProof.literal, currentProof.literal.toLowerCase());
  assert.match(
    currentProof.literal,
    /^\/tmp\/renovate-completion\.[0-9a-f]{32}\/run\.jsonl$/,
  );
  assert.equal(currentProof.mode, 0o700);
  assert.equal(currentProof.format, 'json');
  assert.equal(currentProof.level, 'info');
  assert(
    !existsSync(dirname(currentProof.literal)),
    'Owned evidence directory must be removed',
  );
  assert.equal(
    readFileSync(unrelated, 'utf8'),
    'foreign fixture stays unchanged',
  );
  console.log(
    JSON.stringify({
      nativeCompletionPathContract: true,
      oldMixedCaseRejected: true,
      lowercaseExclusiveOwnedPath: true,
      inheritedAliasesOverridden: true,
      fixtureReceipt: receipt,
    }),
  );
} finally {
  rmSync(root, { recursive: true, force: true });
}
