// Evaluate publisher skip/failure boundaries with GitHub's expression engine.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { Lexer, Parser, Evaluator, data } from '@actions/expressions';

const jobs = JSON.parse(execFileSync(process.env.PYTHON || 'python3', ['-c', [
  'import sys,json',
  'sys.path.insert(0,"scripts")',
  'from install_release import release',
  'p={"repository":"owner/repo","mode":"release","visibility":"public","single_entry_ci":True,"default_branch":"main","versioning":{"schema":1,"promotion":"promote-bytes"},"closed_build":{"workflow":"closed-build.yml","environment":"closed-build","enabled":True}}',
  'print(json.dumps(release(p)["jobs"]))',
].join(';')], { encoding: 'utf8', cwd: new URL('../../', import.meta.url) }));

function evaluate(name, needs, cancelled = false, extra = {}) {
  const fn = { name: 'cancelled', minArgs: 0, maxArgs: 0, call: () => new data.BooleanData(cancelled) };
  const expr = jobs[name].if.slice(3, -2).trim();
  const ast = new Parser(new Lexer(expr).lex().tokens, ['needs', 'github', 'vars', 'inputs'], [fn]).parse();
  const context = {
    needs, vars: { RELEASE_CHANNELS_ENABLED: 'true' }, inputs: {},
    github: { repository: 'owner/repo', ref: 'refs/heads/main', event_name: 'workflow_dispatch' }, ...extra,
  };
  return new Evaluator(ast, JSON.parse(JSON.stringify(context), data.reviver), new Map([['cancelled', fn]])).evaluate().value;
}
let checks = 0;
for (const name of ['candidate', 'final', 'stable']) {
  const closedJob = name === 'stable' ? 'closed_stable_validate' : 'closed_build';
  for (const required of ['true', 'false', '', 'unknown']) {
    for (const outcome of ['success', 'skipped', 'failure', 'cancelled', '']) {
      for (const prepare of ['success', 'skipped', 'failure', 'cancelled']) {
        for (const gate of ['success', 'failure', 'skipped', 'cancelled']) {
          for (const cancelled of [false, true]) {
            const needs = {
              prepare: { result: prepare, outputs: { closed_required: required, channel: name === 'candidate' ? 'rc' : 'stable', build: name === 'stable' ? 'false' : 'true' } },
              gate: { result: gate }, [closedJob]: { result: outcome },
            };
            const expected = !cancelled && prepare === 'success' && (name === 'stable' || gate === 'success') &&
              ((required === 'true' && outcome === 'success') || (required === 'false' && outcome === 'skipped'));
            assert.equal(evaluate(name, needs, cancelled), expected, `${name}/${required}/${outcome}/${prepare}/${gate}/${cancelled}`);
            checks++;
          }
        }
      }
    }
  }
}
const rebuild = {
  closed_request: { result: 'success', outputs: { operation: 'closed-rebuild', closed_required: 'true' } },
  closed_existing_resolve: { result: 'success' },
};
assert.equal(evaluate('closed_rebuild', rebuild), true);
assert.equal(evaluate('scope', rebuild), false);
for (const result of ['skipped', 'failure', 'cancelled', '']) {
  assert.equal(evaluate('closed_rebuild', { ...rebuild, closed_existing_resolve: { result } }), false);
  checks++;
}
for (const github of [
  { repository: 'fork/repo', ref: 'refs/heads/main', event_name: 'workflow_dispatch' },
  { repository: 'owner/repo', ref: 'refs/heads/topic', event_name: 'workflow_dispatch' },
  { repository: 'owner/repo', ref: 'refs/heads/main', event_name: 'pull_request' },
]) {
  assert.equal(evaluate('closed_rebuild', rebuild, false, { github }), false);
  checks++;
}
console.log(`PASS: ${checks + 2} closed publisher/rebuild cases evaluated by GitHub expressions`);
