// Exercise the raw-TOML boundary with inert isolated files; never execute package code.
import fs from 'node:fs/promises';
import path from 'node:path';
import { tmpdir } from 'node:os';
import { pathToFileURL } from 'node:url';
import assert from 'node:assert/strict';
import { assertStaticUvProject } from '../scripts/renovate-security-compat.mjs';

assert.equal(process.argv.length, 3, 'Pass one installed Renovate package directory');
const installationArg = process.argv[2];
let installation;
if (installationArg === '/usr/local/renovate') {
  installation = '/usr/local/renovate';
} else {
  assert.ok(process.env.RUNNER_TEMP && path.isAbsolute(process.env.RUNNER_TEMP));
  installation = path.resolve(
    await fs.realpath(process.env.RUNNER_TEMP),
    'renovate/node_modules/renovate',
  );
  assert.equal(installationArg, installation, 'Use the fixed RUNNER_TEMP installation');
}
assert.equal(await fs.realpath(installation), installation);
const parser = path.join(installation, 'dist/util/toml.js');
assert.equal(await fs.realpath(parser), parser);
const { parse } = await import(pathToFileURL(parser).href);
const F = await fs.realpath(await fs.mkdtemp(path.join(tmpdir(), 'renovate-uv-preflight-')));
try {
  const base =
    '[project]\nname="demo"\nversion="1.0.0"\ndependencies=[]\n[build-system]\nrequires=["setuptools==84.2.1"]\nbuild-backend="setuptools.build_meta"\n';
  const lock = 'version=1\n[[package]]\nname="demo"\nversion="1.0.0"\nsource={editable="."}\n';
  const results = [];
  async function check(label, edit, expected) {
    const parent = await fs.mkdtemp(F + '/' + label + '-'),
      repo = parent + '/repo';
    await fs.mkdir(repo);
    let content = base,
      lockContent = lock;
    const args = { packageFileName: 'pyproject.toml', lockFileName: 'uv.lock', repoRoot: repo };
    let afterWrite = async () => {};
    const setup = {
      repo,
      parent,
      args,
      setAfterWrite: (v) => (afterWrite = v),
      setSource: (v) => (content = v),
      setLock: (v) => (lockContent = v),
    };
    await edit(setup);
    await fs.writeFile(repo + '/pyproject.toml', content);
    await fs.writeFile(repo + '/uv.lock', lockContent);
    await afterWrite();
    let error;
    try {
      await assertStaticUvProject({ content, ...args, parseToml: parse });
    } catch (e) {
      error = e.message;
    }
    assert.equal(!error, expected, label + ': ' + error);
    if (!expected) assert.match(error, /^Native uv lock requires manual qualification:/, label);
    results.push({ label, allowed: !error, error });
  }
  await check('static', async () => {}, true);
  await check(
    'virtual',
    async ({ setSource, setLock }) => {
      setSource(
        '[project]\nname="demo"\nversion="1"\n[dependency-groups]\ndev=["pytest>=8",{include-group="lint"}]\nlint=["ruff==0.17.3"]\n',
      );
      setLock(lock.replace('editable', 'virtual'));
    },
    true,
  );
  await check(
    'remote-wheel',
    async ({ setSource }) =>
      setSource(
        base.replace(
          'dependencies=[]',
          'dependencies=["wheelish @ https://example.invalid/a.whl"]',
        ),
      ),
    true,
  );
  await check(
    'confined-wheel',
    async ({ repo, setSource, setLock }) => {
      await fs.mkdir(repo + '/vendor');
      await fs.writeFile(repo + '/vendor/local.whl', 'PK\x03\x04');
      setSource(base + '[tool.uv.sources]\nlocal={path="vendor/local.whl"}\n');
      setLock(lock + '[[package]]\nname="local"\nsource={path="vendor/local.whl"}\n');
    },
    true,
  );
  for (const field of ['version', 'dependencies', 'readme'])
    await check(
      'dynamic-' + field,
      async ({ setSource }) =>
        setSource(base.replace('[build-system]', `dynamic=["${field}"]\n[build-system]`)),
      false,
    );
  await check(
    'backend-path',
    async ({ setSource }) => setSource(base + 'backend-path=["."]\n'),
    false,
  );
  await check(
    'custom-backend',
    async ({ setSource }) => setSource(base.replace('setuptools.build_meta', 'backend')),
    false,
  );
  await check(
    'missing-static-version',
    async ({ setSource }) => setSource(base.replace('version="1.0.0"\n', '')),
    false,
  );
  await check(
    'workspace',
    async ({ setSource }) => setSource(base + '[tool.uv.workspace]\nmembers=["child"]\n'),
    false,
  );
  for (const source of [
    '{git="https://example.invalid/repo"}',
    '{workspace=true}',
    '{path="src"}',
    '{path="local.whl",editable=true}',
    '{path="local.whl",index="index"}',
  ])
    await check(
      'source-' + results.length,
      async ({ setSource }) => setSource(base + '[tool.uv.sources]\nother=' + source + '\n'),
      false,
    );
  for (const value of [
    'pkg @ ../outside',
    'pkg @ ./src',
    'pkg @ git+https://example.invalid/repo',
    './src',
  ])
    await check(
      'requirement-' + results.length,
      async ({ setSource }) =>
        setSource(base.replace('dependencies=[]', 'dependencies=[' + JSON.stringify(value) + ']')),
      false,
    );
  await check(
    'constraint-local',
    async ({ setSource }) =>
      setSource(base + '[tool.uv]\nconstraint-dependencies=["pkg @ ../local"]\n'),
    false,
  );
  await check(
    'outside-wheel',
    async ({ parent, setSource }) => {
      await fs.writeFile(parent + '/outside.whl', 'PK');
      setSource(base + '[tool.uv.sources]\nlocal={path="../outside.whl"}\n');
    },
    false,
  );
  await check(
    'symlink-wheel',
    async ({ repo, parent, setSource }) => {
      await fs.writeFile(parent + '/outside.whl', 'PK');
      await fs.symlink(parent + '/outside.whl', repo + '/linked.whl');
      setSource(base + '[tool.uv.sources]\nlocal={path="linked.whl"}\n');
    },
    false,
  );
  for (const source of [
    '{git="https://example.invalid/repo"}',
    '{directory="src"}',
    '{editable="."}',
  ])
    await check(
      'lock-source-' + results.length,
      async ({ setLock }) => setLock(lock + '[[package]]\nname="other"\nsource=' + source + '\n'),
      false,
    );
  await check('adjacent-config', async ({ repo }) => fs.writeFile(repo + '/uv.toml', ''), false);
  await check(
    'parent-project',
    async ({ parent }) => fs.writeFile(parent + '/pyproject.toml', '[project]\nname="parent"\n'),
    false,
  );
  await check('parent-config', async ({ parent }) => fs.writeFile(parent + '/uv.toml', ''), false);
  await check('invalid-toml', async ({ setSource }) => setSource('[project\n'), false);
  await check(
    'oversized',
    async ({ setSource }) => setSource(base + '#' + 'x'.repeat(1024 * 1024)),
    false,
  );
  await check(
    'explicit-package-missing-version',
    async ({ setSource }) =>
      setSource('[project]\nname="demo"\ndependencies=[]\n[tool.uv]\npackage=true\n'),
    false,
  );
  await check(
    'static-without-deps',
    async ({ setSource }) => setSource(base.replace('dependencies=[]\n', '')),
    true,
  );
  await check(
    'static-dependency-metadata',
    async ({ setSource }) =>
      setSource(
        base +
          '[[tool.uv.dependency-metadata]]\nname="oscrypto"\nversion="1.3.0"\nrequires-dist=["asn1crypto>=1.5.1"]\n',
      ),
    true,
  );
  await check(
    'local-dependency-metadata',
    async ({ setSource }) =>
      setSource(
        base + '[[tool.uv.dependency-metadata]]\nname="other"\nrequires-dist=["local @ ./src"]\n',
      ),
    false,
  );
  await check(
    'vcs-dependency-metadata',
    async ({ setSource }) =>
      setSource(
        base +
          '[[tool.uv.dependency-metadata]]\nname="other"\nrequires-dist=["local @ git+https://example.invalid/repo"]\n',
      ),
    false,
  );
  await check(
    'traversal-source',
    async ({ args }) => {
      args.packageFileName = '../pyproject.toml';
    },
    false,
  );
  await check(
    'absolute-source',
    async ({ args, repo }) => {
      args.packageFileName = repo + '/pyproject.toml';
    },
    false,
  );
  await check(
    'different-lock-directory',
    async ({ repo, args }) => {
      await fs.mkdir(repo + '/child');
      args.lockFileName = 'child/uv.lock';
    },
    false,
  );
  await check(
    'raw-source-mismatch',
    async ({ repo, setAfterWrite }) =>
      setAfterWrite(() => fs.appendFile(repo + '/pyproject.toml', '\n# different bytes')),
    false,
  );
  await check(
    'symlink-source',
    async ({ repo, parent, setAfterWrite }) =>
      setAfterWrite(async () => {
        await fs.rename(repo + '/pyproject.toml', parent + '/source.toml');
        await fs.symlink(parent + '/source.toml', repo + '/pyproject.toml');
      }),
    false,
  );
  await check(
    'symlink-lock',
    async ({ repo, parent, setAfterWrite }) =>
      setAfterWrite(async () => {
        await fs.rename(repo + '/uv.lock', parent + '/lock.toml');
        await fs.symlink(parent + '/lock.toml', repo + '/uv.lock');
      }),
    false,
  );
  await check(
    'oversized-lock',
    async ({ setLock }) => setLock(lock + '#' + 'x'.repeat(32 * 1024 * 1024)),
    false,
  );
  await check(
    'confined-file-wheel',
    async ({ repo, setSource }) => {
      await fs.writeFile(repo + '/local.whl', 'PK');
      setSource(
        base.replace('dependencies=[]', `dependencies=["local @ file://${repo}/local.whl"]`),
      );
    },
    true,
  );
  async function checkNested(label, parentEdit, expected) {
    await check(label, async (setup) => {
      const { repo, args, setAfterWrite } = setup;
      args.packageFileName = 'research/pyproject.toml';
      args.lockFileName = 'research/uv.lock';
      setAfterWrite(async () => {
        await fs.mkdir(repo + '/research');
        await fs.copyFile(repo + '/pyproject.toml', repo + '/research/pyproject.toml');
        await fs.copyFile(repo + '/uv.lock', repo + '/research/uv.lock');
        await fs.writeFile(repo + '/pyproject.toml', base.replace('name="demo"', 'name="parent"'));
        await fs.writeFile(repo + '/uv.lock', lock.replace('name="demo"', 'name="parent"'));
        await parentEdit(setup);
      });
    }, expected);
  }
  await checkNested('nested-static-parent', async () => {}, true);
  await checkNested('nested-parent-local-dependency', async ({ repo }) =>
    fs.appendFile(repo + '/pyproject.toml', '\n[project.optional-dependencies]\nx=["local @ ./src"]\n'), false);
  await checkNested('nested-workspace-parent', async ({ repo }) =>
    fs.appendFile(repo + '/pyproject.toml', '\n[tool.uv.workspace]\nmembers=["research"]\n'), false);
  await checkNested('nested-index-parent', async ({ repo }) =>
    fs.appendFile(repo + '/pyproject.toml', '\n[[tool.uv.index]]\nurl="https://example.invalid/simple"\n'), false);
  await checkNested('nested-override-parent', async ({ repo }) =>
    fs.appendFile(repo + '/pyproject.toml', '\n[tool.uv]\noverride-dependencies=["demo==1"]\n'), false);
  await checkNested('nested-parent-backend-path', async ({ repo }) =>
    fs.appendFile(repo + '/pyproject.toml', 'backend-path=["."]\n'), false);
  await checkNested('nested-parent-dynamic-version', async ({ repo }) => {
    const file = repo + '/pyproject.toml';
    await fs.writeFile(file, (await fs.readFile(file, 'utf8')).replace('[build-system]', 'dynamic=["version"]\n[build-system]'));
  }, false);
  await checkNested('nested-parent-vcs-lock', async ({ repo }) =>
    fs.appendFile(repo + '/uv.lock', '\n[[package]]\nname="unsafe"\nsource={git="https://example.invalid/repo"}\n'), false);
  await checkNested('nested-parent-missing-lock', async ({ repo }) => fs.unlink(repo + '/uv.lock'), false);
  await checkNested('nested-parent-symlink-source', async ({ repo, parent }) => {
    await fs.rename(repo + '/pyproject.toml', parent + '/redirect.toml');
    await fs.symlink(parent + '/redirect.toml', repo + '/pyproject.toml');
  }, false);
  await checkNested('nested-parent-symlink-lock', async ({ repo, parent }) => {
    await fs.rename(repo + '/uv.lock', parent + '/redirect.lock');
    await fs.symlink(parent + '/redirect.lock', repo + '/uv.lock');
  }, false);
  await checkNested('nested-parent-oversized', async ({ repo }) =>
    fs.appendFile(repo + '/pyproject.toml', '#' + 'x'.repeat(1024 * 1024)), false);
  await checkNested('nested-parent-uv-config', async ({ repo }) => fs.writeFile(repo + '/uv.toml', ''), false);
  await checkNested('nested-external-project', async ({ parent }) =>
    fs.writeFile(parent + '/pyproject.toml', base), false);
  await checkNested('nested-parent-custom-backend', async ({ repo }) => {
    const file = repo + '/pyproject.toml';
    await fs.writeFile(file, (await fs.readFile(file, 'utf8')).replace('setuptools.build_meta', 'unsafe.backend'));
  }, false);
  async function interposeParent({ repo, args }) {
    await fs.mkdir(repo + '/middle');
    await fs.rename(repo + '/research', repo + '/middle/research');
    await fs.writeFile(repo + '/middle/pyproject.toml', base.replace('name="demo"', 'name="middle"'));
    await fs.writeFile(repo + '/middle/uv.lock', lock.replace('name="demo"', 'name="middle"'));
    args.packageFileName = 'middle/research/pyproject.toml';
    args.lockFileName = 'middle/research/uv.lock';
  }
  await checkNested('nested-two-static-parents', interposeParent, true);
  await checkNested('nested-unsafe-grandparent', async (setup) => {
    await interposeParent(setup);
    await fs.appendFile(setup.repo + '/pyproject.toml', '\n[tool.uv.workspace]\nmembers=["middle"]\n');
  }, false);
  console.log(`${results.length} raw-TOML preflight cases passed`);
} finally {
  await fs.rm(F, { recursive: true, force: true });
}
