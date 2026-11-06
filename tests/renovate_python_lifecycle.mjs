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
