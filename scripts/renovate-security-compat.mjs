// Compatibility for reviewed Renovate 44.215.0 planner and Python build boundaries.
// Only the in-memory source of exact, hash-verified upstream modules is changed.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { registerHooks } from 'node:module';
import { dirname, isAbsolute, join, sep } from 'node:path';
import { pathToFileURL } from 'node:url';

export { assertStaticUvProject } from './renovate-uv-preflight.mjs';

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

const pythonPatches = [
  {
    file: 'dist/modules/manager/pip-compile/artifacts.js',
    sha256: '556a86994d74e2dbdc456dd9553480578443b246e1399969e25a0f50c3729ef2',
    replacements: [[
      '\t\t\tconst cmd = constructPipCompileCmd(compileArgs, upgradePackages);',
      '\t\t\tif (compileArgs.commandType !== "uv") throw new Error("The reviewed runner only supports UV compilation without source builds");\n' +
      '\t\t\tif (!compileArgs.argv.includes("--no-build")) compileArgs.argv.push("--no-build");\n' +
      '\t\t\tconst cmd = constructPipCompileCmd(compileArgs, upgradePackages);',
    ]],
  },
  {
    file: 'dist/modules/manager/pip-compile/common.js',
    sha256: '2d1a8685b236bb0e034bda349b6ebad1164059dd6181223ba1fd8a248ba99a56',
    replacements: [
      ['\tuv: [\n', '\tuv: [\n\t\t"--no-build",\n'],
      ['constraint: await resolveToolConstraint(config, "uv")', 'constraint: "0.13.4"'],
    ],
  },
  {
    file: 'dist/modules/manager/pep621/processors/uv.js',
    sha256: '0a6b4041796da980027e514c05ade26373ef5963f7f45b0203edab0ab9bb3250',
    replacements: [
      ['const uvUpdateCMD = "uv lock";', 'const uvUpdateCMD = "uv lock --no-build";'],
      ['constraint: await resolveToolConstraint(config, "uv", () => project.tool?.uv?.["required-version"])',
        'constraint: "0.13.4"'],
      ['\t\t\tconst pythonConstraint = {',
        '\t\t\tawait assertStaticUvProject({ content: updateArtifact.newPackageFileContent, packageFileName, lockFileName, repoRoot: GlobalConfig.get("localDir"), parseToml: parse });\n' +
        '\t\t\tconst pythonConstraint = {'],
    ],
    imports: `import { assertStaticUvProject } from ${JSON.stringify(new URL('./renovate-uv-preflight.mjs', import.meta.url).href)};\n` +
      'import { GlobalConfig } from "../../../../config/global.js";\n' +
      'import { parse } from "../../../../util/toml.js";\n',
  },
  {
    file: 'dist/workers/repository/update/branch/execute-post-upgrade-commands.js',
    sha256: '727f65b5afd9b61202a9e88fd6ffc12ba223e1de76d00127dad85e7db32bd7ec',
    replacements: [[
      '\t\t\t\t\tconst execResult = await gitExec(compiledCmd, execOpts);',
      '\t\t\t\t\tif (compiledCmd.startsWith("uv export ")) {\n' +
      '\t\t\t\t\t\tconst commandArgs = split(compiledCmd);\n' +
      '\t\t\t\t\t\tif (commandArgs.filter((arg) => arg === "--directory").length > 1 || commandArgs.some((arg) => /^(?:--directory=|--project(?:=|$)|--config-file(?:=|$)|-C|-P)/.test(arg))) throw new Error("Unqualified UV export location option");\n' +
      '\t\t\t\t\t\tconst locationIndex = commandArgs.indexOf("--directory");\n' +
      '\t\t\t\t\t\tconst exportDirectory = locationIndex < 0 ? workingDir : upath.resolve(workingDir, commandArgs[locationIndex + 1]);\n' +
      '\t\t\t\t\t\tconst directory = upath.relative(GlobalConfig.get("localDir"), exportDirectory);\n' +
      '\t\t\t\t\t\tconst packageFileName = upath.join(directory, "pyproject.toml");\n' +
      '\t\t\t\t\t\tawait assertStaticUvProject({ content: await readLocalFile(packageFileName, "utf8"), packageFileName, lockFileName: upath.join(directory, "uv.lock"), repoRoot: GlobalConfig.get("localDir"), parseToml: parse });\n' +
      '\t\t\t\t\t}\n' +
      '\t\t\t\t\tif (compiledCmd.startsWith("uv ")) execOpts.toolConstraints = [...(execOpts.toolConstraints ?? []).filter((tool) => tool.toolName !== "uv"), { toolName: "uv", constraint: "0.13.4" }];\n' +
      '\t\t\t\t\tconst execResult = await gitExec(compiledCmd, execOpts);',
    ]],
    imports: `import { assertStaticUvProject } from ${JSON.stringify(new URL('./renovate-uv-preflight.mjs', import.meta.url).href)};\n` +
      'import { parse } from "../../../../util/toml.js";\n' +
      'import { split } from "shlex";\n',
  },
];

function transformPythonModule(source, patch) {
  let text = typeof source === 'string' ? source : Buffer.from(source).toString('utf8');
  assert.equal(createHash('sha256').update(text).digest('hex'), patch.sha256,
    `Unreviewed Renovate Python module ${patch.file}; requalify the build boundary`);
  for (const [oldText, newText] of patch.replacements) {
    assert.equal(text.split(oldText).length, 2, `Expected one reviewed expression in ${patch.file}`);
    text = text.replace(oldText, newText);
  }
  return (patch.imports ?? '') + text;
}

export function installPythonBuildCompat(packageDirectory) {
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
  const targets = new Map(pythonPatches.map((patch) => {
    const filename = checkedPath(patch.file);
    transformPythonModule(readFileSync(filename), patch);
    return [pathToFileURL(filename).href, patch];
  }));
  const applied = new Set();
  const hook = registerHooks({
    load(url, context, nextLoad) {
      const result = nextLoad(url, context);
      const patch = targets.get(url);
      if (!patch) return result;
      assert.equal(result.format, 'module', 'Expected the reviewed ESM Python manager');
      const source = transformPythonModule(result.source, patch);
      applied.add(url);
      return { ...result, source };
    },
  });
  return {
    wasApplied: () => applied.size === targets.size,
    preload: async () => { for (const url of targets.keys()) await import(url); },
    deregister: () => hook.deregister(),
  };
}

// The official image has a fixed, root-owned CLI installation. Tests explicitly
// install the same hook against their isolated, verified npm installation.
if (process.argv[1] === '/usr/local/renovate/dist/renovate.js') {
  assert.equal(realpathSync(process.argv[1]), process.argv[1], 'CLI path must be canonical');
  const compatibility = installSecurityCompat(dirname(dirname(process.argv[1])));
  const pythonCompatibility = installPythonBuildCompat(dirname(dirname(process.argv[1])));
  // Importing the planner here guarantees the hook is exercised before the CLI
  // can perform API writes; dependencies are the same official runtime modules.
  await import(pathToFileURL('/usr/local/renovate/dist/workers/repository/updates/generate.js').href);
  assert.ok(compatibility.wasApplied(), 'Security branch compatibility was not loaded');
  await pythonCompatibility.preload();
  assert.ok(pythonCompatibility.wasApplied(), 'Python build boundary was not loaded');
  console.info('Verified Renovate 44.215.0 security branch compatibility');
  console.info('Verified Renovate 44.215.0 Python build boundary');
}
