import test from 'node:test';
import assert from 'node:assert/strict';
import {
  mkdtempSync,
  realpathSync,
  writeFileSync,
  symlinkSync,
  rmSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  validateInventory,
  removalPlan,
  afterRemoval,
  runLifecycle,
  assertAncestorOnly,
  assertStableNamespace,
  parseProcStat,
  canonicalPath,
  assertCliVersion,
  goRemovalPlan,
  runGoLifecycle,
  assertGoDirectoryEntries,
  pythonWithoutGoRemovalPlan,
  runPythonWithoutGoLifecycle,
} from '../scripts/prepare-renovate-python.mjs';
const fixture = () => ({
  tools: [
    {
      name: 'python',
      version: '3.12.16',
      versions: [{ version: '3.12.16' }, { version: '3.11.14' }],
    },
    {
      name: 'uv',
      type: 'pip',
      version: '0.13.4',
      versions: [
        { version: '0.13.4', parent: { name: 'python', version: '3.12.16' } },
        { version: '0.13.4', parent: { name: 'python', version: '3.11.14' } },
      ],
    },
    { name: 'node', version: '26.13.0', versions: [{ version: '26.13.0' }] },
  ],
});
const requested = '3.15.2';
const goFixture = () => ({
  tools: [
    {
      name: 'golang',
      version: '1.27.3',
      versions: [{ version: '1.27.3' }, { version: '1.25.4' }],
    },
    ...fixture().tools,
  ],
});
function harness(data = fixture()) {
  let state = structuredClone(data);
  const calls = [];
  return {
    calls,
    get state() {
      return state;
    },
    set state(v) {
      state = v;
    },
    io: {
      inventory: () => structuredClone(state),
      verifyPaths: () => calls.push('paths'),
      exclusive: () => calls.push('exclusive'),
      uninstall: (s) => {
        calls.push(s.name + '@' + s.version);
        state = afterRemoval(state, s);
      },
      assertAbsent: () => calls.push('absent'),
    },
  };
}
test('validates entire old-parent set and removes UV leaf before both Python versions', () => {
  const h = harness();
  const r = runLifecycle(requested, h.io);
  assert.deepEqual(r.removed, [
    { name: 'uv', version: '0.13.4' },
    { name: 'python', version: '3.11.14' },
    { name: 'python', version: '3.12.16' },
  ]);
  assert.deepEqual(h.state.tools, [fixture().tools[2]]);
  assert.equal(h.calls.filter((x) => x === 'exclusive').length, 3);
});
test('requested installed preserves all tools and performs no mutation/proc action', () => {
  const h = harness();
  assert.equal(
    runLifecycle('3.12.16', h.io).reason,
    'requested-already-installed',
  );
  assert.deepEqual(h.calls, ['paths']);
  assert.deepEqual(h.state, fixture());
});
test('no previous Python has no uninstall', () => {
  const h = harness({ tools: [] });
  assert.deepEqual(runLifecycle(requested, h.io).removed, []);
});
test('unknown child rejected before all mutation', () => {
  const d = fixture();
  d.tools.push({
    name: 'pip-tools',
    type: 'pip',
    version: '7.5.0',
    versions: [
      { version: '7.5.0', parent: { name: 'python', version: '3.12.16' } },
    ],
  });
  const h = harness(d);
  assert.throws(() => runLifecycle(requested, h.io), /Unknown Python child/);
  assert.deepEqual(h.calls, []);
});
test('UV shared with a parent outside the removal set rejected', () => {
  const d = fixture();
  d.tools[1].versions.push({
    version: '0.13.4',
    parent: { name: 'python', version: requested },
  });
  const h = harness(d);
  assert.throws(
    () => runLifecycle(requested, h.io),
    /Unrecorded Python parent/,
  );
  assert.deepEqual(h.calls, []);
});
test('unqualified UV version and UV child rejected', () => {
  let d = fixture();
  d.tools[1].version = '0.14.0';
  d.tools[1].versions[0].version = '0.14.0';
  assert.throws(() => removalPlan(d, requested), /Unknown Python child/);
  d = fixture();
  d.tools.push({
    name: 'extra',
    version: '1',
    versions: [{ version: '1', parent: { name: 'uv', version: '0.13.4' } }],
  });
  assert.throws(() => removalPlan(d, requested), /Qualified child has child/);
});
test('malformed, duplicate, control and path-shaped versions rejected', () => {
  for (const bad of ['../../python', '3.15.2 --all', '3.15.2\n'])
    assert.throws(() => removalPlan(fixture(), bad));
  const d = fixture();
  d.tools.push(structuredClone(d.tools[0]));
  assert.throws(() => validateInventory(d), /Duplicate/);
  assert.throws(
    () => validateInventory({ tools: [], extra: true }),
    /Unexpected/,
  );
});
test('complete path validation fails before first uninstall', () => {
  const h = harness();
  h.io.verifyPaths = () => {
    throw new Error('redirected path');
  };
  assert.throws(() => runLifecycle(requested, h.io), /redirected/);
  assert.deepEqual(h.calls, []);
});
test('late inventory drift rejected before mutation', () => {
  const h = harness();
  let n = 0;
  h.io.inventory = () => {
    const d = structuredClone(h.state);
    if (++n > 1) d.tools.pop();
    return d;
  };
  assert.throws(() => runLifecycle(requested, h.io), /state changed/);
  assert(!h.calls.some((x) => x.includes('@')));
});
test('official uninstall no-op is detected and no next uninstall occurs', () => {
  const h = harness();
  h.io.uninstall = (s) => h.calls.push(s.name + '@' + s.version);
  assert.throws(
    () => runLifecycle(requested, h.io),
    /Unexpected uninstall result/,
  );
  assert.deepEqual(
    h.calls.filter((x) => x.includes('@')),
    ['uv@0.13.4'],
  );
});
test('unexpected post-uninstall state stops subsequent mutation', () => {
  const h = harness();
  const original = h.io.uninstall;
  h.io.uninstall = (s) => {
    original(s);
    h.state.tools.pop();
  };
  assert.throws(
    () => runLifecycle(requested, h.io),
    /Unexpected uninstall result/,
  );
  assert.deepEqual(
    h.calls.filter((x) => x.includes('@')),
    ['uv@0.13.4'],
  );
});
test('unrelated process and zombie ancestry rejected without killing', () => {
  const p = new Map([
    [1, { parent: 0, start: '1', state: 'S' }],
    [2, { parent: 1, start: '2', state: 'S' }],
    [3, { parent: 2, start: '3', state: 'R' }],
  ]);
  assert.deepEqual(assertAncestorOnly(p, 3), [3, 2, 1]);
  p.set(4, { parent: 1, start: '4', state: 'S' });
  assert.throws(() => assertAncestorOnly(p, 3), /Unrelated/);
  p.delete(4);
  p.get(2).state = 'Z';
  assert.throws(() => assertAncestorOnly(p, 3), /dead/);
});
test('proc stat parser keeps parent/starttime after command-name parentheses', () => {
  const fields = [
    'S',
    '2',
    ...new Array(17).fill('0'),
    '12345',
    ...new Array(10).fill('0'),
  ];
  assert.deepEqual(parseProcStat('3 (a (b)) ' + fields.join(' ')), {
    pid: 3,
    parent: 2,
    state: 'S',
    start: '12345',
  });
});

test('null parent and orphan Python child metadata fail before mutation', () => {
  const d = fixture();
  d.tools[1].versions[0].parent = null;
  assert.throws(() => validateInventory(d), /Expected object/);
  const orphan = fixture();
  orphan.tools[1].versions = [
    { version: '0.13.4', parent: { name: 'python', version: '3.9.1' } },
  ];
  const h = harness(orphan);
  assert.throws(
    () => runLifecycle(requested, h.io),
    /Unrecorded Python parent/,
  );
  assert.deepEqual(h.calls, []);
});

test('actual canonical path check rejects redirected files and wrong kinds', () => {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'private-python-path-')));
  try {
    const file = join(dir, 'file'),
      link = join(dir, 'redirect');
    writeFileSync(file, 'inert');
    assert(canonicalPath(file, 'file').isFile());
    symlinkSync(file, link);
    assert.throws(() => canonicalPath(link, 'file'), /Redirected/);
    assert.throws(() => canonicalPath(file, 'directory'), /type/);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('noncanonical numeric Python spellings are rejected before paths', () => {
  for (const value of ['3.012.1', '3.12.001', '3.01.1'])
    assert.throws(() => removalPlan(fixture(), value));
});

test('CLI identity accepts the complete official format and rejects any altered runtime', () => {
  assertCliVersion('14.64.0 (Node v24.22.2)\n');
  for (const value of [
    '14.64.0',
    '14.65.0 (Node v24.22.2)',
    '14.64.0 (Node v24.22.3)',
    '14.64.0 (Node v24.22.2) extra',
  ]) {
    assert.throws(() => assertCliVersion(value), /Unqualified containerbase/);
  }
});

const withHashin = () => {
  const value = fixture();
  value.tools.push({
    name: 'hashin',
    type: 'pip',
    version: '1.0.5',
    versions: [
      { version: '1.0.5', parent: { name: 'python', version: '3.12.16' } },
      { version: '1.0.5', parent: { name: 'python', version: '3.11.14' } },
    ],
  });
  return value;
};
test('qualified hashin and UV leaves are removed before every obsolete Python parent', () => {
  const h = harness(withHashin());
  const result = runLifecycle(requested, h.io);
  assert.deepEqual(result.removed, [
    { name: 'uv', version: '0.13.4' },
    { name: 'hashin', version: '1.0.5' },
    { name: 'python', version: '3.11.14' },
    { name: 'python', version: '3.12.16' },
  ]);
  assert.deepEqual(h.state.tools, [fixture().tools[2]]);
});
test('unqualified hashin version, foreign parent and descendants reject the whole plan before mutation', () => {
  const mutate = [
    (d) => {
      d.tools[3].version = '1.0.6';
      d.tools[3].versions[0].version = '1.0.6';
    },
    (d) => {
      d.tools[3].versions[1].parent = { name: 'node', version: '26.13.0' };
    },
    (d) => {
      d.tools.push({
        name: 'extra',
        version: '1',
        versions: [
          { version: '1', parent: { name: 'hashin', version: '1.0.5' } },
        ],
      });
    },
  ];
  for (const change of mutate) {
    const d = withHashin();
    change(d);
    const h = harness(d);
    assert.throws(() => runLifecycle(requested, h.io));
    assert.deepEqual(h.calls, []);
  }
});
const processFixture = () =>
  new Map([
    [1, { pid: 1, parent: 0, start: '100', state: 'S' }],
    [7, { pid: 7, parent: 1, start: '200', state: 'S' }],
    [9, { pid: 9, parent: 7, start: '300', state: 'R' }],
  ]);
test('scheduling state changes preserve stable live process identity', () => {
  const first = processFixture(),
    second = processFixture();
  second.get(7).state = 'R';
  second.get(9).state = 'S';
  assert.doesNotThrow(() => assertStableNamespace(first, second, 9));
});
test('PID reuse, reparenting, unrelated processes and dead ancestors reject either snapshot', () => {
  const mutations = [
    (d) => {
      d.get(7).start = '201';
    },
    (d) => {
      d.get(9).parent = 1;
    },
    (d) => {
      d.set(11, { pid: 11, parent: 1, start: '400', state: 'S' });
    },
    (d) => {
      d.get(7).state = 'Z';
    },
  ];
  for (const mutate of mutations) {
    const changed = processFixture();
    mutate(changed);
    assert.throws(() => assertStableNamespace(processFixture(), changed, 9));
    assert.throws(() => assertStableNamespace(changed, processFixture(), 9));
  }
});

test('Go transition removes only recorded old Go versions, preserving Python children and Node', () => {
  const h = harness(goFixture());
  const result = runGoLifecycle('1.26.0', h.io);
  assert.deepEqual(result.removed, [
    { name: 'golang', version: '1.25.4' },
    { name: 'golang', version: '1.27.3' },
  ]);
  assert.deepEqual(h.state, fixture());
  assert.equal(h.calls.filter((c) => c === 'exclusive').length, 2);
});
test('requested installed Go preserves every existing version and has no uninstall', () => {
  const h = harness(goFixture());
  assert.equal(
    runGoLifecycle('1.25.4', h.io).reason,
    'requested-already-installed',
  );
  assert.deepEqual(h.state, goFixture());
  assert.deepEqual(h.calls, ['paths']);
});
test('first Go install has no previous version to uninstall', () => {
  const h = harness(fixture());
  assert.equal(runGoLifecycle('1.26.0', h.io).reason, 'no-previous-go');
  assert.deepEqual(h.state, fixture());
  assert.deepEqual(h.calls, ['paths']);
});
test('any Go child or non-root Go metadata rejects the whole removal plan', () => {
  const child = goFixture();
  child.tools.push({
    name: 'unknown',
    version: '1.0.0',
    versions: [
      { version: '1.0.0', parent: { name: 'golang', version: '1.27.3' } },
    ],
  });
  const h = harness(child);
  assert.throws(() => runGoLifecycle('1.26.0', h.io), /Go child/);
  assert.deepEqual(h.calls, []);
  const parent = goFixture();
  parent.tools[0].versions[0].parent = { name: 'node', version: '26.13.0' };
  assert.throws(() => goRemovalPlan(parent, '1.26.0'), /Go parent/);
  parent.tools[0].type = 'npm';
  assert.throws(() => goRemovalPlan(parent, '1.26.0'), /Go inventory/);
});
test('Go rejects noncanonical, unsafe, unsupported requested and recorded versions', () => {
  for (const value of [
    '1.026.0',
    '1.26.00',
    '1.8.0',
    '2.0.0',
    '1.26',
    '1.26.0/../x',
    '1.26.0 --all',
    '1.26.0\n',
  ]) {
    assert.throws(() => goRemovalPlan(goFixture(), value));
  }
  const d = goFixture();
  d.tools[0].versions[1].version = '1.25.04';
  assert.throws(() => goRemovalPlan(d, '1.26.0'), /Noncanonical/);
});
test('Go exact directory membership rejects unknown versions and sidecars before uninstall', () => {
  assertGoDirectoryEntries(goFixture(), ['1.27.3', '1.25.4']);
  assertGoDirectoryEntries(fixture(), []);
  for (const entries of [
    ['1.27.3'],
    ['1.27.3', '1.25.4', '1.26.0'],
    ['1.27.3', '1.25.4', 'env.sh'],
  ]) {
    const h = harness(goFixture());
    h.io.verifyPaths = (_plan, inventory) =>
      assertGoDirectoryEntries(inventory, entries);
    assert.throws(() => runGoLifecycle('1.26.0', h.io), /Unrecorded Go paths/);
    assert.deepEqual(h.calls, []);
  }
});
test('Go path and process failures prevent every uninstall', () => {
  const h = harness(goFixture());
  h.io.verifyPaths = () => {
    throw new Error('Redirected tool path');
  };
  assert.throws(() => runGoLifecycle('1.26.0', h.io), /Redirected/);
  assert.deepEqual(h.calls, []);
  const p = harness(goFixture());
  p.io.exclusive = () =>
    assertAncestorOnly(
      new Map([
        [1, { parent: 0, start: '1', state: 'S' }],
        [2, { parent: 1, start: '2', state: 'R' }],
        [3, { parent: 1, start: '3', state: 'S' }],
      ]),
      2,
    );
  assert.throws(() => runGoLifecycle('1.26.0', p.io), /Unrelated/);
  assert(!p.calls.some((c) => c.includes('@')));
});
test('Go rechecks inventory before each removal and rejects late foreign changes', () => {
  const h = harness(goFixture());
  let reads = 0;
  h.io.inventory = () => {
    const value = structuredClone(h.state);
    if (++reads === 2) value.tools.pop();
    return value;
  };
  assert.throws(() => runGoLifecycle('1.26.0', h.io), /state changed/);
  assert(!h.calls.some((c) => c.includes('@')));
});
test('Go failed uninstall and unexpected leftover stop before any further version', () => {
  const failed = harness(goFixture());
  failed.io.uninstall = () => {
    throw new Error('official CLI failed');
  };
  assert.throws(
    () => runGoLifecycle('1.26.0', failed.io),
    /official CLI failed/,
  );
  assert.deepEqual(failed.state, goFixture());
  const leftover = harness(goFixture());
  leftover.io.assertAbsent = () => {
    throw new Error('Removed tool path remains');
  };
  assert.throws(() => runGoLifecycle('1.26.0', leftover.io), /path remains/);
  assert.deepEqual(
    leftover.calls.filter((c) => c.includes('@')),
    ['golang@1.25.4'],
  );
});

test('Python-only cold transition retires Go before qualified Python children and preserves Node', () => {
  const h = harness(goFixture());
  assert.deepEqual(runPythonWithoutGoLifecycle(requested, h.io).removed, [
    { name: 'golang', version: '1.25.4' },
    { name: 'golang', version: '1.27.3' },
    { name: 'uv', version: '0.13.4' },
    { name: 'python', version: '3.11.14' },
    { name: 'python', version: '3.12.16' },
  ]);
  assert.deepEqual(h.state.tools, [fixture().tools[2]]);
});
test('warm Python still retires Go and retains all Python children and Node', () => {
  const value = withHashin();
  value.tools.unshift(goFixture().tools[0]);
  const h = harness(value);
  assert.deepEqual(runPythonWithoutGoLifecycle('3.12.16', h.io).removed, [
    { name: 'golang', version: '1.25.4' },
    { name: 'golang', version: '1.27.3' },
  ]);
  assert.deepEqual(h.state, withHashin());
});
test('first Python preparation retires existing Go without requiring an old Python', () => {
  const h = harness({ tools: [goFixture().tools[0], fixture().tools[2]] });
  assert.equal(runPythonWithoutGoLifecycle(requested, h.io).removed.length, 2);
  assert.deepEqual(h.state.tools, [fixture().tools[2]]);
});
test('no-Go preparation preserves ordinary Python removal semantics', () => {
  const h = harness();
  assert.deepEqual(
    runPythonWithoutGoLifecycle(requested, h.io).removed,
    removalPlan(fixture(), requested).steps,
  );
  const warm = harness();
  assert.deepEqual(runPythonWithoutGoLifecycle('3.12.16', warm.io).removed, []);
  assert.deepEqual(warm.state, fixture());
});
test('unknown Go child fails warm and cold Python plans before all mutation', () => {
  const value = goFixture();
  value.tools.push({
    name: 'extra',
    version: '1.0.0',
    versions: [
      { version: '1.0.0', parent: { name: 'golang', version: '1.27.3' } },
    ],
  });
  for (const version of [requested, '3.12.16']) {
    const h = harness(value);
    assert.throws(() => runPythonWithoutGoLifecycle(version, h.io), /Go child/);
    assert.deepEqual(h.calls, []);
  }
});
test('bad later Python child rejects the combined plan before any earlier Go removal', () => {
  const value = goFixture();
  value.tools.push({
    name: 'extra',
    version: '1.0.0',
    versions: [
      { version: '1.0.0', parent: { name: 'python', version: '3.12.16' } },
    ],
  });
  const h = harness(value);
  assert.throws(
    () => runPythonWithoutGoLifecycle(requested, h.io),
    /Unknown Python child/,
  );
  assert.deepEqual(h.calls, []);
});
test('combined path or process rejection never uninstalls any tool', () => {
  for (const method of ['verifyPaths', 'exclusive']) {
    const h = harness(goFixture());
    h.io[method] = () => {
      throw new Error('unsafe boundary');
    };
    assert.throws(
      () => runPythonWithoutGoLifecycle(requested, h.io),
      /unsafe boundary/,
    );
    assert.deepEqual(h.state, goFixture());
    assert(!h.calls.some((c) => c.includes('@')));
  }
});
test('combined plan detects drift and failed official uninstall before proceeding to Python', () => {
  const h = harness(goFixture());
  h.io.uninstall = (step) => h.calls.push(step.name + '@' + step.version);
  assert.throws(
    () => runPythonWithoutGoLifecycle(requested, h.io),
    /Unexpected uninstall result/,
  );
  assert.deepEqual(
    h.calls.filter((c) => c.includes('@')),
    ['golang@1.25.4'],
  );
  const drift = harness(goFixture());
  let reads = 0;
  drift.io.inventory = () => {
    const value = structuredClone(drift.state);
    if (++reads === 2) value.tools.pop();
    return value;
  };
  assert.throws(
    () => runPythonWithoutGoLifecycle(requested, drift.io),
    /state changed/,
  );
  assert(!drift.calls.some((c) => c.includes('@')));
});
test('combined plan rejects malformed recorded Go versions even when Python is warm', () => {
  const value = goFixture();
  value.tools[0].versions[0].version = '1.27.03';
  value.tools[0].version = '1.27.03';
  assert.throws(
    () => pythonWithoutGoRemovalPlan(value, '3.12.16'),
    /Noncanonical/,
  );
});

test('orphan Go child rejects Python-only preparation even without a Go record', () => {
  const value = fixture();
  value.tools.push({
    name: 'extra',
    version: '1.0.0',
    versions: [
      { version: '1.0.0', parent: { name: 'golang', version: '1.27.3' } },
    ],
  });
  const h = harness(value);
  assert.throws(() => runPythonWithoutGoLifecycle('3.12.16', h.io), /Go child/);
  assert.deepEqual(h.calls, []);
});
