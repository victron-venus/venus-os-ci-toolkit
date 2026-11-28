// Qualified npm 12.2.2 bundle-fetch compatibility. No repository code is loaded.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { accessSync, constants, existsSync, closeSync, fstatSync, ftruncateSync, fsyncSync, lstatSync, openSync, readFileSync, readlinkSync, realpathSync, writeSync } from 'node:fs';
import { join, delimiter, isAbsolute } from 'node:path';
import { pathToFileURL } from 'node:url';

const hashes = {
  "package.json": "44c37a6a06ae01d4ad987bc4710031ae3a42d47173100316e4913ab2cd113cd3",
  "bin/npm-cli.js": "8e5f6f3429f8cdbe693cdc29904e9d5a7b127a494bd15c804bd54c7403bfcbe7",
  "lib/cli.js": "67666f06479f9b0bbc01412c198caadd4287d34d5ed74a02871ea78f186451e7",
  "node_modules/@npmcli/arborist/lib/arborist/build-ideal-tree.js": "d94ec6e38ba5ab6e5fa63e4c7d46ad8145bcce27a41e759ff7509a259196ae56",
  "node_modules/@npmcli/arborist/lib/arborist/reify.js": "d43747975102489ed4f7579e779e68c099688f16192b4f8b9599eaf647112766"
};
const patchedHash = "2b84a38880ca79253d64584980979b03139efb796bd2eb8573810d2b5ebb990f";
const copiedRegistryMethods = "  // When extracting a registry-resolved package, the spec we hand to pacote is name@URL.\n  // pacote re-parses that with npa and gets spec.type === 'remote', so without an override the allow-remote gate would fire on every registry tarball (both =none and =root mis-fire).\n  // Returns true only when we are confident this is a registry-mediated install.\n  #isRegistryResolvedTarball (node) {\n    if (!node.resolved || !node.isRegistryDependency) {\n      return false\n    }\n    try {\n      // Match the effective fetch URL, not the raw lockfile value.\n      // #registryResolved applies replace-registry-host, rewriting a public-registry pin to the configured proxy/mirror so it matches.\n      const resolvedURL = new URL(this.#registryResolved(node.resolved))\n      // pickRegistry only consults spec.scope, so a bare-name (tag) parse is sufficient and avoids a node.version dependency.\n      const registry = new URL(pickRegistry(npa(node.name), this.options))\n      const registryPath = registry.pathname.replace(/\\/?$/, '/')\n      return resolvedURL.origin === registry.origin &&\n        (registryPath === '/' || resolvedURL.pathname.startsWith(registryPath))\n    } catch {\n      return false\n    }\n  }\n\n  #registryResolved (resolved) {\n    // the default registry url is a magic value meaning \"the currently\n    // configured registry\".\n    // `resolved` must never be falsey.\n    //\n    // XXX: use a magic string that isn't also a valid value, like\n    // ${REGISTRY} or something.  This has to be threaded through the\n    // Shrinkwrap and Node classes carefully, so for now, just treat\n    // the default reg as the magical animal that it has been.\n    try {\n      const resolvedURL = hgi.parseUrl(resolved)\n      const registryURL = new URL(this.registry)\n      const registryPath = registryURL.pathname.replace(/\\/$/, '')\n\n      let matchURL = null\n      try {\n        matchURL = new URL(this.options.replaceRegistryHost)\n      } catch {\n        // keep matchURL null\n      }\n\n      const matchHost = matchURL?.hostname ?? this.options.replaceRegistryHost\n      const matchPath = matchURL?.pathname.replace(/\\/$/, '') ?? null\n      const hasPathPrefix = (pathname, prefix) =>\n        pathname === prefix || pathname.startsWith(`${prefix}/`)\n\n      const hostMatches = this.options.replaceRegistryHost === 'always' || matchHost === resolvedURL.hostname\n      const pathMatches = !matchPath || hasPathPrefix(resolvedURL.pathname, matchPath)\n\n      if (!hostMatches || !pathMatches) {\n        return resolved\n      }\n\n      resolvedURL.protocol = registryURL.protocol\n      resolvedURL.hostname = registryURL.hostname\n      resolvedURL.port = registryURL.port\n\n      if (matchPath) {\n        // full-URL prefix: swap old path prefix for the registry path\n        resolvedURL.pathname = registryPath + resolvedURL.pathname.slice(matchPath.length)\n      } else if (registryPath && !hasPathPrefix(resolvedURL.pathname, registryPath)) {\n        // host-only: prepend registry path if not already present\n        resolvedURL.pathname = registryPath + resolvedURL.pathname\n      }\n\n      return resolvedURL.toString()\n    } catch {\n      // if we could not parse the url at all then returning nothing\n      // here means it will get removed from the tree in the next step\n      return undefined\n    }\n  }\n\n";
const target = 'node_modules/@npmcli/arborist/lib/arborist/build-ideal-tree.js';
const digest = (bytes) => createHash('sha256').update(bytes).digest('hex');

export function transformNpmBundle(source) {
  const text = Buffer.from(source).toString('utf8');
  if (digest(text) === patchedHash) return text;
  assert.equal(digest(text), hashes[target], 'Unreviewed npm bundle module');
  const replacements = [
    ["const npa = require('npm-package-arg')\n", "const npa = require('npm-package-arg')\nconst hgi = require('hosted-git-info')\nconst { pickRegistry } = require('npm-registry-fetch')\n"],
    ['module.exports = cls => class IdealTreeBuilder extends cls {\n', 'module.exports = cls => class IdealTreeBuilder extends cls {\n' + copiedRegistryMethods],
    ['          integrity: node.integrity,\n', "          integrity: node.integrity,\n          ...(this.#isRegistryResolvedTarball(node) ? { allowRemote: 'all' } : {}),\n"],
  ];
  let result = text;
  for (const [before, after] of replacements) {
    assert.equal(result.split(before).length, 2, 'Unexpected npm source anchor');
    result = result.replace(before, after);
  }
  assert.equal(digest(result), patchedHash, 'Unexpected patched npm source');
  return result;
}

// The public CLI accepts no path overrides. installRoot/binAlias are explicit test seams only.
export function prepareNpmBundle({ installRoot = '/opt/containerbase', binAlias = '/usr/local/bin', env = process.env } = {}) {
  assert.equal(realpathSync(installRoot), installRoot, 'Tool installation root must be canonical');
  const canonicalFile = (file, read = true) => {
    assert.equal(realpathSync(file), file, 'Tool file must not redirect through symlinks');
    const stat = lstatSync(file);
    assert.ok(stat.isFile() && stat.nlink === 1, 'Tool file must be a regular, unshared file');
    return read ? readFileSync(file) : undefined;
  };
  const wrapper = join(installRoot, 'bin/npm');
  const findExecutable = (name) => {
    let selected;
    for (const entry of (env.PATH ?? '').split(delimiter)) {
      assert.ok(isAbsolute(entry), 'Tool PATH must contain only absolute directories');
      const candidate = join(entry, name);
      if (!existsSync(candidate)) continue;
      try { accessSync(candidate, constants.X_OK); selected = candidate; break; } catch { /* continue */ }
    }
    return selected;
  };
  const verifyExecutable = (name, label) => {
    const selected = findExecutable(name);
    const expected = join(installRoot, 'bin', name);
    if (selected === expected) return;
    // containerbase 14.64 links /usr/local/bin to /opt/containerbase/bin.
    assert.equal(selected, join(binAlias, name), `Refusing a shadowed ${label} executable`);
    assert.ok(lstatSync(binAlias).isSymbolicLink(), 'Tool bin alias must be a symlink');
    assert.equal(readlinkSync(binAlias), join(installRoot, 'bin'), 'Unreviewed tool bin alias target');
    assert.equal(realpathSync(binAlias), join(installRoot, 'bin'), 'Tool bin alias must resolve to the canonical directory');
    assert.equal(realpathSync(selected), expected, 'Tool bin alias must select the verified wrapper');
  };
  verifyExecutable('npm', 'npm');
  verifyExecutable('node', 'Node');
  const wrapperText = canonicalFile(wrapper).toString('utf8');
  const escaped = installRoot.replace(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`);
  const match = wrapperText.match(new RegExp(String.raw`^${escaped}/tools/npm/12\.2\.2/(\d+\.\d+\.\d+)/bin/npm "\$@"$`, 'm'));
  assert.ok(match, 'Expected the qualified npm version and containerbase layout');
  const nodeVersion = match[1];
  const [major, minor, patch] = nodeVersion.split('.').map(Number);
  assert.ok(major >= 26 || (major === 24 && minor >= 15) || (major === 22 && (minor > 22 || (minor === 22 && patch >= 2))), 'Selected Node does not meet npm 12.2.2 engines');
  const expectedWrapper = `#!/bin/bash

if [[ -z "\${CONTAINERBASE_ENV+x}" ]]; then
  . /usr/local/etc/env
fi

if [[ ! -f "/tmp/containerbase/tool.init.d/npm" ]]; then
  # set logging to only warn and above to not interfere with tool output
  CONTAINERBASE_LOG_LEVEL=warn containerbase-cli init tool "npm"
fi
# load tool envs
include () {
    local file=${installRoot}/tools/$1/env.sh
    [[ -f "$file" ]] && source "$file"
}
include node
include npm
unset include
${installRoot}/tools/npm/12.2.2/${nodeVersion}/bin/npm "$@"
`;
  assert.equal(wrapperText, expectedWrapper, 'Unreviewed npm launcher');
  const expectedNodeWrapper = expectedWrapper.replaceAll('tool.init.d/npm', 'tool.init.d/node').replaceAll('init tool "npm"', 'init tool "node"').replace('include node\ninclude npm\n', 'include node\n').replace(`${installRoot}/tools/npm/12.2.2/${nodeVersion}/bin/npm`, `${installRoot}/tools/node/${nodeVersion}/bin/node`);
  assert.equal(canonicalFile(join(installRoot, 'bin/node')).toString('utf8'), expectedNodeWrapper, 'Unreviewed or inconsistent Node launcher');
  canonicalFile(join(installRoot, 'tools/node', nodeVersion, 'bin/node'), false);
  const packageDir = join(installRoot, 'tools/npm/12.2.2', nodeVersion, 'node_modules/npm');
  assert.equal(realpathSync(join(installRoot, 'tools/npm/12.2.2', nodeVersion, 'bin/npm')), join(packageDir, 'bin/npm-cli.js'), 'Npm launch link must resolve to the verified CLI');
  for (const [relative, hash] of Object.entries(hashes)) {
    const actual = digest(canonicalFile(join(packageDir, relative)));
    assert.ok(actual === hash || (relative === target && actual === patchedHash), `Unreviewed npm file: ${relative}`);
  }
  const metadata = JSON.parse(readFileSync(join(packageDir, 'package.json'), 'utf8'));
  assert.equal(metadata.name, 'npm');
  assert.equal(metadata.version, '12.2.2');
  const filename = join(packageDir, target);
  const fd = openSync(filename, constants.O_RDWR | constants.O_NOFOLLOW);
  try {
    assert.ok(fstatSync(fd).isFile() && fstatSync(fd).nlink === 1, 'Target changed');
    const before = readFileSync(fd);
    const result = transformNpmBundle(before);
    if (digest(before) !== patchedHash) {
      const bytes = Buffer.from(result);
      let written = 0;
      while (written < bytes.length) written += writeSync(fd, bytes, written, bytes.length - written, written);
      ftruncateSync(fd, bytes.length);
      fsyncSync(fd);
    }
  } finally { closeSync(fd); }
  assert.equal(digest(canonicalFile(filename)), patchedHash, 'Npm patch was not preserved');
  return { npmVersion: metadata.version, nodeVersion, packageDir };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  assert.equal(process.argv.length, 2, 'No helper arguments are accepted');
  prepareNpmBundle();
  console.log('Verified npm 12.2.2 registry bundle compatibility');
}
