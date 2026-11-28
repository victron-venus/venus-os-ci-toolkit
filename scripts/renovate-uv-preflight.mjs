// Inspect raw TOML before native `uv lock`. The CLI separately forbids third-party builds.
import { lstat as uvLstat, readFile as uvReadFile, realpath as uvRealpath } from 'node:fs/promises';
import { dirname as uvDirname, isAbsolute as uvIsAbsolute, resolve as uvResolve, sep as uvSep, basename as uvBasename, relative as uvRelative } from 'node:path';
import { fileURLToPath as uvFileURLToPath } from 'node:url';

const uvStandardBackends = new Set(['setuptools.build_meta', 'hatchling.build', 'flit_core.buildapi', 'poetry.core.masonry.api', 'pdm.backend', 'uv_build']);
const uvObject = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const uvName = (value) => String(value).toLowerCase().replace(/[-_.]+/g, '-');
function uvReject(reason) { throw new Error(`Native uv lock requires manual qualification: ${reason}`); }
async function uvExists(file) {
  try { await uvLstat(file); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}
async function uvRegular(file, root) {
  if (file !== root && !file.startsWith(root + uvSep)) uvReject('input escapes repository');
  const info = await uvLstat(file);
  if (!info.isFile() || await uvRealpath(file) !== file) uvReject('input must be a regular file without symlinks');
  return info;
}

export async function assertStaticUvProject({ content, packageFileName, lockFileName, repoRoot, parseToml }) {
  if (typeof content !== 'string' || Buffer.byteLength(content) > 1024 * 1024 || typeof parseToml !== 'function') uvReject('invalid raw project input');
  if (typeof repoRoot !== 'string' || !uvIsAbsolute(repoRoot)) uvReject('repository root must be absolute');
  const root = uvResolve(repoRoot);
  if (await uvRealpath(root) !== root) uvReject('repository root must not redirect through symlinks');
  const localPath = (name, basename) => {
    if (typeof name !== 'string' || uvIsAbsolute(name) || name.includes('\\') || name.includes('\0') || name.split('/').some(p => p === '..' || p === '')) uvReject('invalid repository-relative input');
    const result = uvResolve(root, name);
    if (uvBasename(result) !== basename) uvReject('unexpected input filename');
    return result;
  };
  const projectFile = localPath(packageFileName, 'pyproject.toml');
  const lockFile = localPath(lockFileName, 'uv.lock');
  const directory = uvDirname(projectFile);
  if (uvDirname(lockFile) !== directory) uvReject('parent/workspace lockfile is not qualified');
  await uvRegular(projectFile, root);
  const lockInfo = await uvRegular(lockFile, root);
  if (lockInfo.size > 32 * 1024 * 1024) uvReject('lockfile exceeds inspection limit');
  if (await uvReadFile(projectFile, 'utf8') !== content) uvReject('raw project differs from the file uv will read');
  // uv discovers parent projects/config files automatically. Qualify each ancestor
  // inside this repository; external projects and inherited UV settings stay denied.
  let parentProject;
  for (let current = directory, depth = 0; ; current = uvDirname(current), depth++) {
    if (depth > 128) uvReject('parent context exceeds inspection limit');
    if (await uvExists(uvResolve(current, 'uv.toml'))) uvReject('adjacent or parent uv.toml is not qualified');
    if (current !== directory && await uvExists(uvResolve(current, 'pyproject.toml'))) {
      if (current !== root && !current.startsWith(root + uvSep)) uvReject('parent project escapes repository');
      parentProject ??= uvResolve(current, 'pyproject.toml');
    }
    if (uvDirname(current) === current) break;
  }
  if (parentProject) {
    const parentInfo = await uvRegular(parentProject, root);
    if (parentInfo.size > 1024 * 1024) uvReject('parent project exceeds inspection limit');
    const parentContent = await uvReadFile(parentProject, 'utf8');
    let parentRaw;
    try { parentRaw = parseToml(parentContent); } catch { uvReject('parent TOML cannot be parsed'); }
    // Independent nested projects do not need inherited workspace, index, source,
    // constraint or path settings. Do not silently accept those configurations.
    if (!uvObject(parentRaw) || parentRaw.tool?.uv !== undefined) uvReject('parent UV configuration is not qualified');
    const parentLock = uvResolve(uvDirname(parentProject), 'uv.lock');
    if (!await uvExists(parentLock)) uvReject('parent lockfile is not qualified');
    // The nearest ancestor recursively inspects its ancestors exactly once. Apply
    // the same static metadata, backend, dependency, lockfile and symlink checks.
    await assertStaticUvProject({
      content: parentContent,
      packageFileName: uvRelative(root, parentProject).split(uvSep).join('/'),
      lockFileName: uvRelative(root, parentLock).split(uvSep).join('/'),
      repoRoot: root,
      parseToml,
    });
  }
  let raw, lock;
  try { raw = parseToml(content); lock = parseToml(await uvReadFile(lockFile, 'utf8')); }
  catch { uvReject('raw TOML cannot be parsed'); }
  if (!uvObject(raw) || !uvObject(lock)) uvReject('invalid TOML document');
  const project = raw.project;
  if (!uvObject(project) || typeof project.name !== 'string' || !project.name) uvReject('static project name is required');
  if (project.dynamic !== undefined && (!Array.isArray(project.dynamic) || project.dynamic.length)) uvReject('dynamic project metadata');
  const build = raw['build-system'];
  if (build !== undefined) {
    if (!uvObject(build) || Object.hasOwn(build, 'backend-path') || !uvStandardBackends.has(build['build-backend'])) uvReject('unqualified build backend');
    if (typeof project.version !== 'string' || !project.version) uvReject('static project version is required for buildable projects');
  }
  const uv = raw.tool?.uv ?? {};
  if (!uvObject(uv) || Object.hasOwn(uv, 'workspace')) uvReject('workspace metadata');
  if (uv.package === true && (typeof project.version !== 'string' || !project.version)) uvReject('static project version is required for buildable projects');
  const wheel = async (reference) => {
    if (typeof reference !== 'string' || !reference || reference.includes('\\') || reference.includes('\0')) uvReject('invalid wheel path');
    let file;
    if (reference.startsWith('file:')) {
      let url;
      try { url = new URL(reference); file = uvFileURLToPath(url); } catch { uvReject('invalid file URL'); }
      if (url.search || url.hash || url.hostname) uvReject('ambiguous file URL');
    } else {
      if (uvIsAbsolute(reference) || reference.includes('%') || reference.includes(':')) uvReject('wheel path must be relative');
      file = uvResolve(directory, reference);
    }
    if (!file.endsWith('.whl')) uvReject('local source must be a prebuilt wheel');
    await uvRegular(file, root);
  };
  const location = async (reference) => {
    if (typeof reference !== 'string' || !reference) uvReject('invalid dependency location');
    if (/^https?:\/\//.test(reference)) return; // --no-build guards downloaded distributions.
    await wheel(reference);
  };
  const requirement = async (value) => {
    if (typeof value !== 'string' || !value.trim()) uvReject('invalid requirement');
    const at = value.indexOf('@');
    if (at >= 0) await location(value.slice(at + 1).trim().split(/\s+;/)[0].trim());
    else if (!/^[A-Za-z0-9][A-Za-z0-9._-]*(?:\s|\[|[<=>!~]|$)/.test(value.trim())) uvReject('unqualified local or VCS requirement');
  };
  const requirements = async (values) => {
    if (values === undefined) return;
    if (!Array.isArray(values)) uvReject('requirements must be an array');
    for (const value of values) await requirement(value);
  };
  await requirements(project.dependencies);
  await requirements(build?.requires);
  if (project['optional-dependencies'] !== undefined && !uvObject(project['optional-dependencies'])) uvReject('invalid optional dependencies');
  for (const values of Object.values(project['optional-dependencies'] ?? {})) await requirements(values);
  if (raw['dependency-groups'] !== undefined && !uvObject(raw['dependency-groups'])) uvReject('invalid dependency groups');
  for (const values of Object.values(raw['dependency-groups'] ?? {})) {
    if (!Array.isArray(values)) uvReject('invalid dependency group');
    for (const value of values) {
      if (uvObject(value) && Object.keys(value).length === 1 && typeof value['include-group'] === 'string') continue;
      await requirement(value);
    }
  }
  for (const key of ['dev-dependencies', 'constraint-dependencies', 'override-dependencies', 'build-constraint-dependencies']) await requirements(uv[key]);
  if (uv['dependency-metadata'] !== undefined && !Array.isArray(uv['dependency-metadata'])) uvReject('invalid dependency metadata');
  for (const metadata of uv['dependency-metadata'] ?? []) {
    if (!uvObject(metadata)) uvReject('invalid dependency metadata');
    await requirements(metadata['requires-dist']);
  }
  if (uv.sources !== undefined && !uvObject(uv.sources)) uvReject('invalid uv sources');
  for (const entries of Object.values(uv.sources ?? {})) for (const source of Array.isArray(entries) ? entries : [entries]) {
    if (!uvObject(source) || Object.keys(source).some(key => !['path', 'url', 'index', 'marker', 'extra', 'group'].includes(key))) uvReject('directory, VCS, editable or unknown uv source');
    const kinds = ['path', 'url', 'index'].filter(key => Object.hasOwn(source, key));
    if (kinds.length !== 1) uvReject('ambiguous uv source');
    if (kinds[0] === 'path') await wheel(source.path);
    if (kinds[0] === 'url') await location(source.url);
    if (kinds[0] === 'index' && typeof source.index !== 'string') uvReject('invalid index source');
  }
  if (!Array.isArray(lock.package)) uvReject('invalid lockfile package list');
  for (const pkg of lock.package) {
    if (!uvObject(pkg) || !uvObject(pkg.source) || Object.keys(pkg.source).length !== 1) uvReject('unqualified lockfile source');
    const [kind, reference] = Object.entries(pkg.source)[0];
    if (kind === 'registry' && typeof reference === 'string') continue;
    if (kind === 'url') { await location(reference); continue; }
    if (kind === 'path') { await wheel(reference); continue; }
    if (['editable', 'virtual', 'directory'].includes(kind) && reference === '.' && uvName(pkg.name) === uvName(project.name)) continue;
    uvReject('local directory, workspace or VCS lockfile source');
  }
}
