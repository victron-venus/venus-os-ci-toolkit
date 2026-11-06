// Explicit private-only preload; the normal public launcher never imports this.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { isAbsolute, join, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
const relative = 'dist/util/exec/containerbase.js';
const originalHash =
  '56ab95ca7e0a8f09400fa9c1958b613a325658c738cb61fe90f6b114279e0935';
export function transformPythonLifecycle(source, helperPath) {
  assert(
    isAbsolute(helperPath) && !helperPath.includes('\n'),
    'Expected fixed absolute helper path',
  );
  let text = Buffer.from(source).toString('utf8');
  assert.equal(
    createHash('sha256').update(text).digest('hex'),
    originalHash,
    'Unreviewed containerbase command generator',
  );
  const replacements = [
    [
      '\tlet constraint = toolConstraint.constraint;',
      '\tlet constraint = toolConstraint.constraint;\n' +
        '\tif (toolName === "hashin") {\n' +
        '\t\tif (constraint && (!versioning.isValid(constraint) || !versioning.matches("1.0.5", constraint))) throw new Error("Unqualified private hashin constraint");\n' +
        '\t\treturn "1.0.5";\n' +
        '\t}',
    ],
    [
      '\t\t\t\tif (get(cacheKey) === active) continue;',
      '\t\t\t\tif (toolName !== "python" && toolName !== "uv" && toolName !== "hashin" && get(cacheKey) === active) continue;',
    ],
    [
      '\t\t\tconst installCommand =',
      '\t\t\tif (toolName === "uv" && toolVersion !== "0.13.4") throw new Error("Unqualified private UV version");\n' +
        '\t\t\tif (toolName === "python") installCommands.push("/usr/local/renovate/node " + quote(' +
        JSON.stringify(helperPath) +
        ') + " " + quote(toolVersion));\n' +
        '\t\t\tconst installCommand =',
    ],
  ];
  for (const [before, after] of replacements) {
    assert.equal(text.split(before).length, 2, 'Unexpected lifecycle anchor');
    text = text.replace(before, after);
  }
  return text;
}
export function installPythonLifecycleCompat(packageDirectory) {
  assert(
    isAbsolute(packageDirectory) && !packageDirectory.split(sep).includes('..'),
    'Expected canonical runtime path',
  );
  const root = realpathSync(packageDirectory);
  const metadata = join(root, 'package.json');
  assert.equal(realpathSync(metadata), metadata);
  const pkg = JSON.parse(readFileSync(metadata, 'utf8'));
  assert.equal(pkg.name, 'renovate');
  assert.equal(pkg.version, '44.215.0');
  const helper = fileURLToPath(
    new URL('./prepare-renovate-python.mjs', import.meta.url),
  );
  assert.equal(realpathSync(helper), helper);
  const target = join(root, relative);
  assert.equal(realpathSync(target), target);
  transformPythonLifecycle(readFileSync(target), helper);
  const url = pathToFileURL(target).href;
  let applied = false;
  const hook = registerHooks({
    load(filename, context, next) {
      const result = next(filename, context);
      if (filename !== url) return result;
      assert.equal(result.format, 'module');
      applied = true;
      return {
        ...result,
        source: transformPythonLifecycle(result.source, helper),
      };
    },
  });
  return {
    wasApplied: () => applied,
    preload: async () => {
      await import(url);
      assert(
        applied,
        'Lifecycle must preload before the other compatibility imports',
      );
    },
    deregister: () => hook.deregister(),
  };
}
if (process.argv[1] === '/usr/local/renovate/dist/renovate.js') {
  assert.equal(realpathSync(process.argv[1]), process.argv[1]);
  const guard = installPythonLifecycleCompat('/usr/local/renovate');
  await guard.preload();
  console.info('Verified Renovate 44.215.0 private Python tool lifecycle');
}
