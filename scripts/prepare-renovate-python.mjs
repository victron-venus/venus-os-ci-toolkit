// Private runner only: retire verified obsolete Python or Go tool records.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import {
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statfsSync,
} from 'node:fs';
import { performance } from 'node:perf_hooks';
import { fileURLToPath } from 'node:url';

const cli = '/usr/local/containerbase/bin/containerbase-cli';
const toolRoot = '/opt/containerbase/tools';
const pythonVersion = /^3\.(\d{1,2})\.(\d{1,3})$/;
const goVersion = /^1\.(\d{1,2})\.(\d{1,3})$/;
export function qualifiedGoVersion(value) {
  const parts = typeof value === 'string' ? goVersion.exec(value) : null;
  assert(parts && Number(parts[1]) >= 9, 'Unqualified requested Go');
  const version = `1.${Number(parts[1])}.${Number(parts[2])}`;
  assert.equal(value, version, 'Noncanonical Go version');
  return version;
}
export function qualifiedPythonVersion(value) {
  const parts = typeof value === 'string' ? pythonVersion.exec(value) : null;
  assert(parts, 'Unqualified requested Python');
  // Reconstruct from numeric components before the value can enter a path.
  const version = `3.${Number(parts[1])}.${Number(parts[2])}`;
  assert.equal(value, version, 'Noncanonical Python version');
  return version;
}
export function assertCliVersion(output) {
  assert.equal(
    output.trim(),
    '14.64.0 (Node v24.22.2)',
    'Unqualified containerbase CLI version',
  );
}
const own = (obj, key) => Object.hasOwn(obj, key);
function exactKeys(obj, required, optional = []) {
  assert(
    obj && typeof obj === 'object' && !Array.isArray(obj),
    'Expected object',
  );
  assert(
    required.every((k) => own(obj, k)),
    'Missing metadata field',
  );
  assert(
    Object.keys(obj).every((k) => required.includes(k) || optional.includes(k)),
    'Unexpected metadata field',
  );
}
function text(value) {
  assert(
    typeof value === 'string' &&
      value.length > 0 &&
      value.length <= 200 &&
      !/[\x00-\x1f\x7f]/.test(value),
    'Invalid metadata string',
  );
}
export function validateInventory(value) {
  exactKeys(value, ['tools']);
  assert(
    Array.isArray(value.tools) && value.tools.length <= 256,
    'Invalid tool inventory',
  );
  const names = new Set();
  for (const tool of value.tools) {
    exactKeys(tool, ['name', 'version', 'versions'], ['type']);
    text(tool.name);
    assert(!names.has(tool.name), 'Duplicate tool');
    names.add(tool.name);
    assert(
      tool.version === null || typeof tool.version === 'string',
      'Invalid current version',
    );
    if (tool.version !== null) text(tool.version);
    if (own(tool, 'type'))
      assert(['gem', 'npm', 'pip'].includes(tool.type), 'Invalid tool type');
    assert(
      Array.isArray(tool.versions) &&
        tool.versions.length > 0 &&
        tool.versions.length <= 64,
      'Invalid versions',
    );
    const rows = new Set();
    for (const version of tool.versions) {
      exactKeys(version, ['version'], ['parent']);
      text(version.version);
      if (own(version, 'parent')) {
        exactKeys(version.parent, ['name', 'version']);
        text(version.parent.name);
        text(version.parent.version);
      }
      const key = JSON.stringify([
        version.version,
        version.parent?.name,
        version.parent?.version,
      ]);
      assert(!rows.has(key), 'Duplicate version');
      rows.add(key);
    }
    assert(
      tool.version === null ||
        tool.versions.some((v) => v.version === tool.version),
      'Current version is not installed',
    );
  }
  return value;
}
function childRemovalSteps(inventory, old) {
  const qualifiedChildren = { uv: '0.13.4', hashin: '1.0.5' };
  const children = new Set();
  for (const tool of inventory.tools)
    for (const v of tool.versions) {
      if (v.parent?.name === 'python') {
        assert(old.has(v.parent.version), 'Unrecorded Python parent');
        assert(
          Object.hasOwn(qualifiedChildren, tool.name) &&
            tool.type === 'pip' &&
            v.version === qualifiedChildren[tool.name],
          'Unknown Python child',
        );
        children.add(tool.name);
      }
    }
  const steps = [];
  for (const name of ['uv', 'hashin']) {
    if (!children.has(name)) continue;
    const child = inventory.tools.find((t) => t.name === name);
    const version = qualifiedChildren[name];
    assert(
      child.version === null || child.version === version,
      'Unexpected current qualified child',
    );
    assert(
      child.versions.every(
        (v) =>
          v.version === version &&
          v.parent?.name === 'python' &&
          old.has(v.parent.version),
      ),
      'Qualified child has a preserved or unknown parent',
    );
    assert(
      !inventory.tools.some((t) =>
        t.versions.some((v) => v.parent?.name === name),
      ),
      'Qualified child has child tools',
    );
    steps.push({ name, version });
  }
  return steps;
}
export function removalPlan(inventory, requested) {
  requested = qualifiedPythonVersion(requested);
  validateInventory(inventory);
  const python = inventory.tools.find((t) => t.name === 'python');
  if (!python) return { reason: 'no-previous-python', requested, steps: [] };
  assert(
    !python.type && python.versions.length <= 8,
    'Unexpected Python inventory',
  );
  for (const v of python.versions)
    assert(
      pythonVersion.test(v.version) && !v.parent,
      'Unqualified Python record',
    );
  if (python.versions.some((v) => v.version === requested))
    return { reason: 'requested-already-installed', requested, steps: [] };
  const old = new Set(python.versions.map((v) => v.version));
  const steps = childRemovalSteps(inventory, old);
  for (const version of [...old].sort((a, b) => a.localeCompare(b)))
    steps.push({ name: 'python', version });
  return { reason: 'replace-obsolete-python', requested, steps };
}
function qualifiedGoInventory(inventory) {
  validateInventory(inventory);
  const go = inventory.tools.find((t) => t.name === 'golang');
  if (!go) return undefined;
  assert(!go.type && go.versions.length <= 8, 'Unexpected Go inventory');
  for (const version of go.versions) {
    qualifiedGoVersion(version.version);
    assert(!version.parent, 'Unexpected Go parent');
  }
  return go;
}
function goRemovalSteps(inventory, go) {
  assert(
    !inventory.tools.some((tool) =>
      tool.versions.some((v) => v.parent?.name === 'golang'),
    ),
    'Go child tools are not qualified for removal',
  );
  return (go?.versions ?? [])
    .map((v) => ({ name: 'golang', version: v.version }))
    .sort((a, b) => a.version.localeCompare(b.version));
}
export function goRemovalPlan(inventory, requested) {
  requested = qualifiedGoVersion(requested);
  const go = qualifiedGoInventory(inventory);
  if (!go) return { reason: 'no-previous-go', requested, steps: [] };
  if (go.versions.some((v) => v.version === requested))
    return { reason: 'requested-already-installed', requested, steps: [] };
  return {
    reason: 'replace-obsolete-go',
    requested,
    steps: goRemovalSteps(inventory, go),
  };
}
export function pythonWithoutGoRemovalPlan(inventory, requested) {
  const python = removalPlan(inventory, requested);
  const go = qualifiedGoInventory(inventory);
  return {
    reason: 'prepare-python-without-go',
    requested: python.requested,
    steps: [...goRemovalSteps(inventory, go), ...python.steps],
  };
}
export function canonicalInventory(inventory) {
  return JSON.stringify(
    inventory.tools
      .map((t) => ({
        name: t.name,
        version: t.version,
        type: t.type ?? null,
        versions: t.versions
          .map((v) => [
            v.version,
            v.parent?.name ?? null,
            v.parent?.version ?? null,
          ])
          .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))),
      }))
      .sort((a, b) => a.name.localeCompare(b.name)),
  );
}
export function afterRemoval(inventory, step) {
  const value = structuredClone(inventory);
  const tool = value.tools.find((t) => t.name === step.name);
  assert(tool, 'Missing removed tool');
  tool.versions = tool.versions.filter((v) => v.version !== step.version);
  if (tool.version === step.version) tool.version = null;
  value.tools = value.tools.filter((t) => t.versions.length);
  return value;
}
export function assertAncestorOnly(records, self) {
  assert(
    records instanceof Map && records.size <= 4096,
    'Invalid process inventory',
  );
  const chain = new Set();
  let pid = self;
  while (pid !== 0) {
    assert(!chain.has(pid), 'Cyclic process ancestry');
    chain.add(pid);
    const row = records.get(pid);
    assert(row?.start && row.state !== 'Z', 'Missing or dead ancestor');
    pid = row.parent;
  }
  assert(chain.has(1), 'Unverified PID namespace');
  for (const pid of records.keys())
    assert(chain.has(pid), 'Unrelated process in private tool namespace');
  return [...chain];
}
export function parseProcStat(content) {
  const end = content.lastIndexOf(')');
  assert(end > 0, 'Invalid process stat');
  const pid = Number(content.slice(0, content.indexOf(' ')));
  const fields = content
    .slice(end + 2)
    .trim()
    .split(/\s+/);
  assert(
    Number.isSafeInteger(pid) && pid > 0 && fields.length >= 20,
    'Invalid process identity',
  );
  const parent = Number(fields[1]);
  assert(
    Number.isSafeInteger(parent) && parent >= 0 && /^\d+$/.test(fields[19]),
    'Invalid process ancestry',
  );
  return { pid, parent, start: fields[19], state: fields[0] };
}
function procSnapshot() {
  assert(process.platform === 'linux', 'Private lifecycle requires Linux');
  assert.equal(statfsSync('/proc').type, 0x9fa0, 'Expected procfs');
  const ids = readdirSync('/proc').filter((n) => /^\d+$/.test(n));
  assert(ids.length <= 4096, 'Excessive process count');
  return new Map(
    ids.map((id) => {
      const row = parseProcStat(readFileSync('/proc/' + id + '/stat', 'utf8'));
      assert.equal(row.pid, Number(id));
      return [row.pid, row];
    }),
  );
}
export function assertStableNamespace(first, second, self) {
  assertAncestorOnly(first, self);
  assertAncestorOnly(second, self);
  const identities = (records) =>
    [...records]
      .map(([pid, row]) => [pid, row.parent, row.start])
      .sort((a, b) => a[0] - b[0]);
  assert.deepEqual(
    identities(first),
    identities(second),
    'Process namespace changed during validation',
  );
}
function exclusiveNamespace() {
  assertStableNamespace(procSnapshot(), procSnapshot(), process.pid);
}
export function canonicalPath(path, kind) {
  assert(path.startsWith('/'), 'Absolute path required');
  let prefix = '';
  for (const part of path.split('/').filter(Boolean)) {
    prefix += '/' + part;
    const st = lstatSync(prefix);
    assert(!st.isSymbolicLink(), 'Redirected tool path');
    assert.equal(realpathSync(prefix), prefix, 'Noncanonical tool path');
  }
  const st = lstatSync(path);
  assert(
    kind === 'file' ? st.isFile() : st.isDirectory(),
    'Unexpected tool path type',
  );
  return st;
}
function verifyFixedRuntime() {
  assert.equal(process.platform, 'linux');
  const st = canonicalPath(cli, 'file');
  assert.equal(st.uid, 0, 'CLI must be root owned');
  assert.equal(st.mode & 0o022, 0, 'CLI must not be writable by runner');
  canonicalPath('/opt/containerbase', 'directory');
  canonicalPath(toolRoot, 'directory');
  canonicalPath('/home/ubuntu', 'directory');
}
function versionPath(step) {
  assert(['python', 'uv', 'hashin'].includes(step.name));
  if (step.name === 'python')
    return toolRoot + '/python/' + qualifiedPythonVersion(step.version);
  if (step.name === 'uv') {
    assert.equal(step.version, '0.13.4');
    return toolRoot + '/uv/0.13.4';
  }
  assert.equal(step.version, '1.0.5');
  return toolRoot + '/hashin/1.0.5';
}
function verifyPlanPaths(plan, inventory) {
  if (plan.reason === 'requested-already-installed') {
    canonicalPath(
      versionPath({ name: 'python', version: plan.requested }),
      'directory',
    );
    return;
  }
  for (const step of plan.steps) canonicalPath(versionPath(step), 'directory');
  const python = inventory.tools.find((t) => t.name === 'python');
  if (python) {
    canonicalPath(toolRoot + '/python', 'directory');
    assert.deepEqual(
      readdirSync(toolRoot + '/python').sort((a, b) => a.localeCompare(b)),
      python.versions.map((v) => v.version).sort((a, b) => a.localeCompare(b)),
      'Unrecorded Python paths',
    );
  }
  for (const name of ['uv', 'hashin']) {
    if (!plan.steps.some((s) => s.name === name)) continue;
    const root = toolRoot + (name === 'uv' ? '/uv' : '/hashin');
    const version = name === 'uv' ? '0.13.4' : '1.0.5';
    assert.deepEqual(
      readdirSync(root).sort((a, b) => a.localeCompare(b)),
      [version],
      'Unrecorded qualified child paths',
    );
    const parents = inventory.tools
      .find((t) => t.name === name)
      .versions.map((v) => v.parent.version)
      .sort((a, b) => a.localeCompare(b));
    assert.deepEqual(
      readdirSync(root + '/' + version).sort((a, b) => a.localeCompare(b)),
      parents,
      'Unexpected qualified child parent paths',
    );
    for (const parent of parents)
      canonicalPath(root + '/' + version + '/' + parent, 'directory');
  }
}
function goVersionPath(step) {
  assert.equal(step.name, 'golang', 'Expected fixed Go tool');
  return toolRoot + '/golang/' + qualifiedGoVersion(step.version);
}
export function assertGoDirectoryEntries(inventory, entries) {
  const go = inventory.tools.find((t) => t.name === 'golang');
  assert.deepEqual(
    [...entries].sort((a, b) => a.localeCompare(b)),
    (go?.versions ?? [])
      .map((v) => v.version)
      .sort((a, b) => a.localeCompare(b)),
    'Unrecorded Go paths',
  );
}
function verifyGoPlanPaths(plan, inventory) {
  if (plan.reason === 'requested-already-installed') {
    canonicalPath(
      goVersionPath({ name: 'golang', version: plan.requested }),
      'directory',
    );
    return;
  }
  const root = toolRoot + '/golang';
  // An empty root left by the official uninstaller is legitimate.
  if (absent(root)) {
    assert(
      !inventory.tools.some((t) => t.name === 'golang'),
      'Missing Go root',
    );
    return;
  }
  canonicalPath(root, 'directory');
  assertGoDirectoryEntries(inventory, readdirSync(root));
  for (const step of plan.steps)
    canonicalPath(goVersionPath(step), 'directory');
}
function verifyPythonWithoutGoPlanPaths(plan, inventory) {
  const expected = pythonWithoutGoRemovalPlan(inventory, plan.requested);
  assert.deepEqual(
    plan.steps,
    expected.steps,
    'Cross-tool removal plan changed',
  );
  verifyGoPlanPaths(
    { ...plan, steps: plan.steps.filter((s) => s.name === 'golang') },
    inventory,
  );
  verifyPlanPaths(removalPlan(inventory, plan.requested), inventory);
}
function absent(path) {
  try {
    lstatSync(path);
    return false;
  } catch (e) {
    if (e.code === 'ENOENT') return true;
    throw e;
  }
}
export function runLifecycle(requested, io) {
  const initial = structuredClone(validateInventory(io.inventory()));
  const plan = removalPlan(initial, requested);
  return executePlan(requested, io, initial, plan);
}
export function runGoLifecycle(requested, io) {
  const initial = structuredClone(validateInventory(io.inventory()));
  const plan = goRemovalPlan(initial, requested);
  return executePlan(requested, io, initial, plan);
}
export function runPythonWithoutGoLifecycle(requested, io) {
  const initial = structuredClone(validateInventory(io.inventory()));
  const plan = pythonWithoutGoRemovalPlan(initial, requested);
  return executePlan(requested, io, initial, plan);
}
function executePlan(requested, io, initial, plan) {
  io.verifyPaths(plan, initial); // Entire plan and every path validated before any mutation.
  if (!plan.steps.length) return { reason: plan.reason, removed: [] };
  let expected = initial;
  const removed = [];
  for (const step of plan.steps) {
    assert.equal(
      canonicalInventory(validateInventory(io.inventory())),
      canonicalInventory(expected),
      'Installed tool state changed',
    );
    io.verifyPaths(
      {
        requested,
        steps: plan.steps.slice(removed.length),
        reason: plan.reason,
      },
      expected,
    );
    io.exclusive();
    io.uninstall(step); // No recursive flag; official CLI retains child guard.
    expected = afterRemoval(expected, step);
    assert.equal(
      canonicalInventory(validateInventory(io.inventory())),
      canonicalInventory(expected),
      'Unexpected uninstall result',
    );
    io.assertAbsent(step);
    removed.push(step);
  }
  return { reason: plan.reason, removed };
}
function main(requested, tool = 'python') {
  const handlers = {
    python: [runLifecycle, verifyPlanPaths],
    golang: [runGoLifecycle, verifyGoPlanPaths],
    'python-without-go': [
      runPythonWithoutGoLifecycle,
      verifyPythonWithoutGoPlanPaths,
    ],
  };
  assert(Object.hasOwn(handlers, tool), 'Unexpected lifecycle tool');
  verifyFixedRuntime();
  exclusiveNamespace();
  const deadline = performance.now() + 120000;
  const call = (args) => {
    const remaining = deadline - performance.now();
    assert(remaining > 0, 'Private lifecycle deadline exceeded');
    return execFileSync(cli, args, {
      encoding: 'utf8',
      timeout: Math.max(1, Math.floor(Math.min(30000, remaining))),
      killSignal: 'SIGKILL',
      maxBuffer: 1048576,
      env: {
        HOME: '/home/ubuntu',
        USER_NAME: 'ubuntu',
        USER_ID: '12021',
        PATH: '/usr/local/containerbase/bin:/usr/local/bin:/usr/bin:/bin',
        LANG: 'C.UTF-8',
        LOG_LEVEL: 'error',
      },
    });
  };
  assertCliVersion(call(['--version']));
  const [run, verifyPaths] = handlers[tool];
  const result = run(requested, {
    inventory: () => JSON.parse(call(['list', 'tools', '--json'])),
    verifyPaths,
    exclusive: exclusiveNamespace,
    uninstall: (step) => call(['uninstall', 'tool', step.name, step.version]),
    assertAbsent: (step) =>
      assert(
        absent(
          step.name === 'golang' ? goVersionPath(step) : versionPath(step),
        ),
        'Removed tool path remains',
      ),
  });
  console.info(
    JSON.stringify(
      tool === 'golang'
        ? { privateGoLifecycle: result }
        : { privatePythonLifecycle: result },
    ),
  );
}
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (process.argv.length === 3) {
    main(process.argv[2]);
  } else {
    assert.equal(process.argv.length, 4, 'Expected one exact tool version');
    assert(
      ['golang', 'python-without-go'].includes(process.argv[2]),
      'Expected fixed lifecycle mode',
    );
    main(process.argv[3], process.argv[2]);
  }
}
