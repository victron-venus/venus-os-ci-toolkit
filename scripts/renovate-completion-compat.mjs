// Register visibility evidence before other compatibility modules preload Renovate.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { dirname, isAbsolute, join, sep } from 'node:path';
import { pathToFileURL } from 'node:url';
const version = '44.215.0';

// Count-only completion evidence before an artifact failure can return no-work.
// Preserve critical evidence even if repository log remaps lower its severity.
const completionPatches = [
  {
    file: 'dist/workers/repository/update/branch/index.js',
    sha256: '3238fb0f87b7a32da4f54ba610f7b33f844241c50e7724a2e5a63c493949db1d',
    before: '\t\t\tawait bumpVersions(config);\n\t\t\tremoveMeta(["dep"]);\n\t\t\tif (config.artifactErrors?.length) {',
    after: '\t\t\tawait bumpVersions(config);\n\t\t\tremoveMeta(["dep"]);\n\t\t\tif (config.artifactErrors?.length) {\n' +
      '\t\t\t\tlogger.warn({ artifactErrorCount: config.artifactErrors.length }, "Artifact generation failed before PR decision");',
  },
  {
    file: 'dist/logger/renovate-logger.js',
    sha256: '7de39b2044ba5b4515d7be467488c379d633f62cf1f06404798bb489b78d2e94',
    before: '\t\t\t\tconst remappedLevel = getRemappedLevel(msg);',
    after: '\t\t\t\tconst requestedLevel = getRemappedLevel(msg);\n' +
      '\t\t\t\tconst completionEvidence = ["warn", "error", "fatal"].includes(_level) || ["Repository started", "Repository finished"].includes(msg);\n' +
      '\t\t\t\tconst remappedLevel = completionEvidence && ["trace", "debug"].includes(requestedLevel) ? null : requestedLevel;',
  },
];

export function transformCompletionModule(source, relative) {
  const patch = completionPatches.find(value => value.file === relative);
  assert.ok(patch, 'Unexpected completion-evidence module');
  const text = typeof source === 'string' ? source : Buffer.from(source).toString('utf8');
  assert.equal(createHash('sha256').update(text).digest('hex'), patch.sha256,
    'Unreviewed Renovate completion module; requalify evidence visibility');
  assert.equal(text.split(patch.before).length, 2, 'Expected one completion-evidence anchor');
  return text.replace(patch.before, patch.after);
}

export function installCompletionCompat(packageDirectory) {
  assert.ok(isAbsolute(packageDirectory), 'Renovate package path must be absolute');
  assert.ok(!packageDirectory.split(sep).includes('..'), 'No path traversal');
  const root = realpathSync(packageDirectory);
  const metadataPath = join(root, 'package.json');
  assert.equal(realpathSync(metadataPath), metadataPath, 'Renovate metadata must be canonical');
  const metadata = JSON.parse(readFileSync(metadataPath, 'utf8'));
  assert.equal(metadata.name, 'renovate'); assert.equal(metadata.version, version);
  const targets = new Map(completionPatches.map(patch => {
    const filename = join(root, patch.file);
    assert.equal(realpathSync(filename), filename, 'Completion modules must not redirect through symlinks');
    transformCompletionModule(readFileSync(filename), patch.file);
    return [pathToFileURL(filename).href, patch.file];
  }));
  const applied = new Set();
  const hook = registerHooks({ load(url, context, next) {
    const result = next(url, context);
    const relative = targets.get(url);
    if (!relative) return result;
    assert.equal(result.format, 'module', 'Expected reviewed ESM completion module');
    applied.add(url);
    return {...result, source: transformCompletionModule(result.source, relative)};
  }});
  return { wasApplied: () => applied.size === targets.size,
    preload: async () => { for (const url of targets.keys()) await import(url); },
    deregister: () => hook.deregister() };
}

export let activeCompletionCompat = null;
if (process.argv[1] === '/usr/local/renovate/dist/renovate.js') {
  assert.equal(realpathSync(process.argv[1]), process.argv[1], 'CLI path must be canonical');
  // Register only. Lifecycle/security guards must install before their modules load.
  activeCompletionCompat = installCompletionCompat(dirname(dirname(process.argv[1])));
}
