// Evaluate dispatch expressions with GitHub's actual parser/evaluator, not a mock.
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { readFile } from 'node:fs/promises';
import { Lexer, Parser, Evaluator, data } from '@actions/expressions';

const expected = JSON.parse(await readFile(new URL('./package.json', import.meta.url), 'utf8')).dependencies['@actions/expressions'];
const actual = JSON.parse(await readFile(new URL('./node_modules/@actions/expressions/package.json', import.meta.url), 'utf8')).version;
assert.equal(actual, expected);
const python = process.env.PYTHON || 'python3';
const declarations = JSON.parse(execFileSync(python, ['-c', [
  'import sys,json',
  'sys.path.insert(0,"scripts")',
  'from runner_selection import runner_labels',
  'print(json.dumps({"ci":runner_labels(),"automation":runner_labels("automation"),"release":runner_labels("release"),"legacy":runner_labels("automation",explicit_input="runner-labels"),"scope":runner_labels(explicit_input="runner",scalar=True)}))',
].join(';')], { encoding: 'utf8', cwd: new URL('../../', import.meta.url) }));
function evaluate(kind, variables = {}, event = 'push', options = {}) {
  const context = {
    vars: variables,
    inputs: { 'runner-labels': '["ubuntu-latest"]', runner: 'ubuntu-latest', ...options.inputs },
    github: {
      repository: 'owner/project', event_name: event, ref: options.ref || 'refs/heads/main',
      event: {
        repository: { default_branch: 'main', private: options.private || false },
        ...(event.startsWith('pull_request') ? { pull_request: { head: { repo: { full_name: options.fork ? 'outside/fork' : 'owner/project' } } } } : {}),
      },
    },
  };
  const expression = declarations[kind].slice(3, -2).trim();
  const ast = new Parser(new Lexer(expression).lex().tokens, ['vars', 'inputs', 'github'], []).parse();
  const result = new Evaluator(ast, JSON.parse(JSON.stringify(context), data.reviver)).evaluate();
  return JSON.parse(JSON.stringify(result, data.replacer));
}
let checks = 0;
function equal(kind, variables, event, options, expectedResult) {
  assert.deepEqual(evaluate(kind, variables, event, options), expectedResult, `${kind}/${event}/${JSON.stringify(options)}`);
  checks++;
}
const vars = { CI_RUNNER_MODE: 'self-hosted', CI_RUNNER_LABELS: '["self-hosted","linux","x64","isolated-ci"]', CI_RUNNER_AUTOMATION_LABELS: '["isolated-automation"]', CI_RUNNER_RELEASE_LABELS: '["isolated-release"]' };
for (const kind of ['ci', 'automation', 'release', 'legacy', 'scope']) {
  for (const mode of ['', 'github', 'typo']) equal(kind, { ...vars, CI_RUNNER_MODE: mode }, 'push', {}, ['ubuntu-latest']);
  equal(kind, {}, 'push', {}, ['ubuntu-latest']);
  for (const event of ['pull_request', 'pull_request_target', 'pull_request_review']) equal(kind, vars, event, { fork: true }, ['ubuntu-latest']);
  for (const event of ['merge_group', 'issue_comment', 'workflow_run', 'unknown']) equal(kind, vars, event, {}, ['ubuntu-latest']);
  equal(kind, vars, 'workflow_dispatch', { ref: 'refs/heads/feature' }, ['ubuntu-latest']);
}
for (const mode of ['self-hosted', 'k3s']) {
  for (const event of ['push', 'schedule', 'workflow_dispatch', 'pull_request']) {
    equal('ci', { ...vars, CI_RUNNER_MODE: mode }, event, {}, ['self-hosted', 'linux', 'x64', 'isolated-ci']);
    equal('automation', { ...vars, CI_RUNNER_MODE: mode }, event, {}, ['isolated-automation']);
    equal('release', { ...vars, CI_RUNNER_MODE: mode }, event, {}, event === 'pull_request' ? ['ubuntu-latest'] : ['isolated-release']);
  }
}
equal('ci', vars, 'pull_request_target', {}, ['ubuntu-latest']);
equal('automation', vars, 'pull_request_target', {}, ['isolated-automation']);
equal('legacy', {}, 'pull_request_target', { private: true, fork: true, inputs: { 'runner-labels': '["existing-private"]' } }, ['existing-private']);
equal('legacy', vars, 'pull_request_target', { fork: true, inputs: { 'runner-labels': '["forbidden"]' } }, ['ubuntu-latest']);
equal('scope', {}, 'workflow_dispatch', { private: true, inputs: { runner: 'existing-private' } }, 'existing-private');
equal('scope', vars, 'pull_request', { fork: true, inputs: { runner: 'forbidden' } }, ['ubuntu-latest']);
equal('ci', { CI_RUNNER_MODE: 'self-hosted' }, 'push', {}, []); // invalid runs-on: fails admission, never guesses a pool
assert.throws(() => evaluate('ci', { ...vars, CI_RUNNER_LABELS: 'not json' }));
console.log(`PASS: ${checks + 1} cases evaluated by GitHub expressions ${expected}`);
