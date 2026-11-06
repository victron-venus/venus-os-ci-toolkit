// Compatibility for the reviewed Renovate 44.215.0 branch-leader defect.
// Only the in-memory source of one exact upstream module is changed.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { dirname, isAbsolute, join, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

const version = '44.215.0';
const originalSha256 = 'd180fc73931d6ece3f125e202ba77695cd43f8b28fe8f52cb4a7e0cbdb3b37d8';
const before = '...config.upgrades[0],';
const after = '...(config.upgrades.find((upgrade) => upgrade.isVulnerabilityAlert) ?? config.upgrades[0]),';

export function transformSecurityLeader(source) {
  const text = typeof source === 'string' ? source : Buffer.from(source).toString('utf8');
  assert.equal(createHash('sha256').update(text).digest('hex'), originalSha256,
    'Unreviewed Renovate branch planner; requalify or retire the compatibility fix');
  assert.equal(text.split(before).length, 2, 'Expected exactly one branch-leader expression');
  return text.replace(before, after);
}

export function installSecurityCompat(packageDirectory) {
  assert.ok(isAbsolute(packageDirectory), 'Renovate package path must be absolute');
  assert.ok(!packageDirectory.split(sep).includes('..'), 'No path traversal');
  const root = realpathSync(packageDirectory);
  const checkedPath = (relative) => {
    const expected = join(root, relative);
    assert.equal(realpathSync(expected), expected, 'Renovate runtime files must not redirect through symlinks');
    return expected;
  };
  const metadata = JSON.parse(readFileSync(checkedPath('package.json'), 'utf8'));
  assert.equal(metadata.name, 'renovate', 'Expected the official Renovate package');
  assert.equal(metadata.version, version, 'Unreviewed Renovate version');
  const target = checkedPath('dist/workers/repository/updates/generate.js');
  // Validate before importing the CLI, loading credentials or contacting GitHub.
  transformSecurityLeader(readFileSync(target));
  const targetUrl = pathToFileURL(target).href;
  let applied = false;
  const hook = registerHooks({
    load(url, context, nextLoad) {
      const result = nextLoad(url, context);
      if (url !== targetUrl) return result;
      assert.equal(result.format, 'module', 'Expected the reviewed ESM planner');
      const source = transformSecurityLeader(result.source);
      applied = true;
      return { ...result, source };
    },
  });
  return { wasApplied: () => applied, deregister: () => hook.deregister() };
}

// The official image has a fixed, root-owned CLI installation. Tests explicitly
// install the same hook against their isolated, verified npm installation.
if (process.argv[1] === '/usr/local/renovate/dist/renovate.js') {
  assert.equal(realpathSync(process.argv[1]), process.argv[1], 'CLI path must be canonical');
  const compatibility = installSecurityCompat(dirname(dirname(process.argv[1])));
  // Importing the planner here guarantees the hook is exercised before the CLI
  // can perform API writes; dependencies are the same official runtime modules.
  await import(pathToFileURL('/usr/local/renovate/dist/workers/repository/updates/generate.js').href);
  assert.ok(compatibility.wasApplied(), 'Security branch compatibility was not loaded');
  console.info('Verified Renovate 44.215.0 security branch compatibility');
}
