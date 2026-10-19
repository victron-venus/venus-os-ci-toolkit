"""Consumer adoption preserves all fields except explicitly owned routing and pins."""
import json
import copy
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from prepare_runner_reserve import main, render, render_policy_pins  # noqa: E402 - Import after adding the source directory.
from runner_selection import runner_labels, yaml_editor  # noqa: E402 - Import the source scripts after adding their directory.
from workflow_contracts import validate_fuzz_pins  # noqa: E402 - Exercise the consumer's actual pin contract.


class ReserveAdoptionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.workflows = self.root / '.github/workflows'
        self.workflows.mkdir(parents=True)

    def test_legacy_consumer_preserves_events_permissions_and_platforms(self):
        source = '''on: {push: {}, pull_request: {}}
permissions: {contents: read}
jobs:
  linux:
    runs-on: ubuntu-latest
    steps: [{run: 'pytest --cov-fail-under=90'}]
  matrix:
    runs-on: ${{ matrix.os }}
    strategy: {matrix: {os: [ubuntu-24.04-arm, macos-latest, windows-latest]}}
    steps: [{run: test}]
  fixed:
    runs-on: ubuntu-22.04
    steps: [{run: test}]
  hardware:
    runs-on: [self-hosted, linux, cerbo-lab]
    steps: [{run: test}]
  shared:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/python-ci.yml@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    secrets: inherit
'''
        path = self.workflows / 'ci.yml'
        path.write_text(source)
        rendered = render(self.root, 'b' * 40)
        new = yaml_editor().load(rendered['.github/workflows/ci.yml'])
        old = yaml_editor().load(source)
        self.assertIn('vars.CI_RUNNER_LABELS', new['jobs']['linux']['runs-on'])
        self.assertTrue(new['jobs']['shared']['uses'].endswith('@' + 'b' * 40))
        for key, field in [('linux', 'runs-on'), ('shared', 'uses')]:
            new['jobs'][key][field] = old['jobs'][key][field]
        self.assertEqual(new, old)
        self.assertEqual(path.read_text(), source)
        path.write_text(rendered['.github/workflows/ci.yml'])
        self.assertEqual(render(self.root, 'b' * 40), {})

    def test_coverage_pin_matches_without_upgrading_release_engine(self):
        policy = {'versioning': {'toolkit_revision': 'a' * 40},
                  'coverage': {'toolkit_ref': 'a' * 40, 'reports': [{'name': 'keep'}]}}
        (self.root / '.release-policy.json').write_text(json.dumps(policy))
        updated = json.loads(render(self.root, 'b' * 40)['.release-policy.json'])
        self.assertEqual(updated['versioning'], policy['versioning'])
        self.assertEqual(updated['coverage']['toolkit_ref'], 'b' * 40)
        self.assertEqual(updated['coverage']['reports'], policy['coverage']['reports'])

    def test_policy_pin_edits_preserve_consumer_layout(self):
        self.fuzz_consumer()
        template = '''{
  "versioning": {"toolkit_revision": "OLD"},
  "coverage": {"toolkit_ref": "COVERAGE_PIN", "reports": ["one", "two"]},
  "fuzzing": {"toolkit_ref": "FUZZ_PIN"},
  "other": {"coverage": {"toolkit_ref": "OLD"}},
  "text": "\\\"toolkit_ref\\\": \\\"OLD\\\" and caf\\u00e9",
  "array": [{"fuzzing": {"toolkit_ref": "OLD"}}, true, null, 1e-3]
}'''.replace('OLD', 'a' * 40)
        path = self.root / '.release-policy.json'
        for newline, indent, final in (('\n', '  ', '\n'), ('\r\n', '\t', ''), ('\n', '', '')):
            formatted = template.replace('\n', newline).replace('  ', indent) + final
            source = formatted.replace('COVERAGE_PIN', 'a' * 40).replace('FUZZ_PIN', 'a' * 40)
            expected = formatted.replace('COVERAGE_PIN', 'b' * 40).replace('FUZZ_PIN', 'b' * 40)
            path.write_bytes(source.encode())
            with self.subTest(newline=newline, indent=indent, final=final):
                files = render(self.root, 'b' * 40)
                self.assertEqual(files['.release-policy.json'], expected)
                self.assertEqual(path.read_bytes(), source.encode())
                policy = json.loads(source)
                policy['coverage']['toolkit_ref'] = 'b' * 40
                policy['fuzzing']['toolkit_ref'] = 'b' * 40
                self.assertEqual(json.loads(expected), policy)

    def test_policy_pin_edits_support_escaped_keys_and_values(self):
        source = '  {"cover\\u0061ge":{"toolkit_\\u0072ef":"' + '\\u0061' * 40 + '"}, "keep": []}  '
        path = self.root / '.release-policy.json'
        path.write_text(source)
        expected = source.replace('\\u0061' * 40, 'b' * 40)
        self.assertEqual(render(self.root, 'b' * 40)['.release-policy.json'], expected)
        path.write_text(expected)
        self.assertEqual(render(self.root, 'b' * 40), {})

    def test_cli_writes_exact_rendered_bytes(self):
        source = '{\r\n\t"coverage": {"toolkit_ref": "' + 'a' * 40 + '"}\r\n}'
        path = self.root / '.release-policy.json'
        path.write_bytes(source.encode())
        with mock.patch.object(sys, 'argv', ['prepare_runner_reserve.py', str(self.root), '--toolkit-ref', 'b' * 40]):
            main()
        self.assertEqual(path.read_bytes(), source.replace('a' * 40, 'b' * 40).encode())

    def test_duplicate_policy_members_refuse_all_edits(self):
        valid = '{"coverage":{"toolkit_ref":"' + 'a' * 40 + '"}}'
        cases = ('{"coverage":{},"coverage":{}}',
                 '{"coverage":{"toolkit_ref":"a","toolkit_ref":"b"}}',
                 valid[:-1] + ',"other":{"same":1,"same":2}}')
        workflow = self.workflows / 'ci.yml'
        workflow.write_text('permissions: {contents: read}\njobs: {ci: {runs-on: ubuntu-latest, steps: [{run: test}]}}')
        path = self.root / '.release-policy.json'
        for source in cases:
            path.write_text(source)
            before = {p: p.read_bytes() for p in (path, workflow)}
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'Duplicate policy key'):
                render(self.root, 'b' * 40)
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_policy_preservation_checks_all_semantic_values(self):
        source = '{"coverage":{"toolkit_ref":"' + 'a' * 40 + '"},"untouched":true}'
        expected = json.loads(source)
        expected['coverage']['toolkit_ref'] = 'b' * 40
        expected['untouched'] = False
        with self.assertRaisesRegex(ValueError, 'unexpected values'):
            render_policy_pins(source, expected)

    def fuzz_consumer(self):
        policy = {'versioning': {'toolkit_revision': 'c' * 40}, 'fuzzing': {'toolkit_ref': 'a' * 40}}
        workflow = {'on': {'workflow_dispatch': {}}, 'permissions': {'contents': 'read'}, 'jobs': {
            'fuzz': {'uses': 'victron-venus/venus-os-ci-toolkit/.github/workflows/fuzz.yml@' + 'a' * 40,
                     'if': "${{ github.event.inputs.channel == 'rc' }}", 'needs': ['prepare'],
                     'with': {'toolkit-ref': 'a' * 40, 'release-plan-artifact': 'frozen-plan'}}}}
        (self.root / '.release-policy.json').write_text(json.dumps(policy))
        # JSON is also YAML; keep the fixture's source stable when checking no-write failures.
        (self.workflows / 'release-pipeline.yml').write_text(json.dumps(workflow))
        return policy, workflow

    def test_fuzz_wrapper_runtime_and_policy_upgrade_together(self):
        policy, workflow = self.fuzz_consumer()
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        files = render(self.root, 'b' * 40)
        updated = yaml_editor().load(files['.github/workflows/release-pipeline.yml'])
        updated_policy = json.loads(files['.release-policy.json'])
        self.assertEqual(updated['jobs']['fuzz']['with']['toolkit-ref'], 'b' * 40)
        self.assertTrue(updated['jobs']['fuzz']['uses'].endswith('@' + 'b' * 40))
        self.assertEqual(updated_policy['fuzzing']['toolkit_ref'], 'b' * 40)
        self.assertEqual(updated_policy['versioning'], policy['versioning'])
        updated['jobs']['fuzz']['uses'] = workflow['jobs']['fuzz']['uses']
        updated['jobs']['fuzz']['with']['toolkit-ref'] = 'a' * 40
        self.assertEqual(updated, workflow)
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        for path, text in files.items():
            (self.root / path).write_text(text)
        validate_fuzz_pins(self.root, {'release-pipeline.yml': yaml_editor().load(
            (self.workflows / 'release-pipeline.yml').read_text())})
        self.assertEqual(render(self.root, 'b' * 40), {})

    def test_inconsistent_existing_fuzz_contract_refuses_all_edits(self):
        for mutation in ('wrapper', 'runtime', 'missing-runtime', 'missing-policy', 'missing-job', 'invalid-policy'):
            policy, workflow = self.fuzz_consumer()
            if mutation == 'wrapper':
                workflow['jobs']['fuzz']['uses'] = workflow['jobs']['fuzz']['uses'].replace('a' * 40, 'd' * 40)
            elif mutation == 'runtime':
                workflow['jobs']['fuzz']['with']['toolkit-ref'] = 'd' * 40
            elif mutation == 'missing-runtime':
                del workflow['jobs']['fuzz']['with']['toolkit-ref']
            elif mutation == 'missing-policy':
                del policy['fuzzing']
            elif mutation == 'missing-job':
                del workflow['jobs']['fuzz']
            else:
                policy['fuzzing']['toolkit_ref'] = 'main'
            (self.root / '.release-policy.json').write_text(json.dumps(policy))
            (self.workflows / 'release-pipeline.yml').write_text(json.dumps(workflow))
            before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                render(self.root, 'b' * 40)
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_automation_steps_and_nonimmutable_pins_rejected(self):
        for ref in ('main', 'a' * 39, 'b' * 41):
            with self.assertRaises(ValueError):
                render(self.root, ref)
        (self.workflows / 'auto-approve.yml').write_text('jobs: {unsafe: {runs-on: ubuntu-latest, steps: [{run: test}]}}')
        with self.assertRaisesRegex(ValueError, 'automation steps'):
            render(self.root, 'b' * 40)

    def vendor_consumer(self):
        workflow = {'on': {'push': {}}, 'permissions': {'contents': 'read'}, 'jobs': {
            'deliver': {'uses': 'victron-venus/venus-os-ci-toolkit/.github/workflows/vendor-update.yml@' + 'a' * 40,
                        'if': "${{ github.ref == 'refs/heads/main' }}", 'needs': ['build'],
                        'with': {'toolkit-revision': 'a' * 40, 'artifact-id': '${{ needs.build.outputs.id }}'},
                        'secrets': {'DELIVERY_TOKEN': '${{ secrets.DELIVERY_TOKEN }}'}}}}
        path = self.workflows / 'vendor.yml'
        path.write_text(json.dumps(workflow))
        return path, workflow

    def test_vendor_workflow_and_runtime_upgrade_together(self):
        path, workflow = self.vendor_consumer()
        before = path.read_bytes()
        files = render(self.root, 'b' * 40)
        self.assertEqual(set(files), {'.github/workflows/vendor.yml'})
        updated = yaml_editor().load(files['.github/workflows/vendor.yml'])
        self.assertEqual(updated['jobs']['deliver']['with']['toolkit-revision'], 'b' * 40)
        self.assertEqual(updated['jobs']['deliver']['uses'],
                         workflow['jobs']['deliver']['uses'].replace('a' * 40, 'b' * 40))
        restored = copy.deepcopy(updated)
        restored['jobs']['deliver']['uses'] = workflow['jobs']['deliver']['uses']
        restored['jobs']['deliver']['with']['toolkit-revision'] = 'a' * 40
        self.assertEqual(restored, workflow)
        self.assertEqual(path.read_bytes(), before)
        path.write_text(files['.github/workflows/vendor.yml'])
        self.assertEqual(render(self.root, 'b' * 40), {})

    def test_inconsistent_vendor_pins_refuse_all_edits(self):
        for mutation in ('missing-input', 'missing-with', 'invalid-with', 'different-input', 'mutable-ref', 'already-target'):
            path, workflow = self.vendor_consumer()
            job = workflow['jobs']['deliver']
            if mutation == 'missing-input':
                del job['with']['toolkit-revision']
            elif mutation == 'missing-with':
                del job['with']
            elif mutation == 'invalid-with':
                job['with'] = 'unexpected'
            elif mutation == 'different-input':
                job['with']['toolkit-revision'] = 'c' * 40
            elif mutation == 'mutable-ref':
                job['uses'] = job['uses'].replace('a' * 40, 'main')
                job['with']['toolkit-revision'] = 'main'
            else:
                job['uses'] = job['uses'].replace('a' * 40, 'b' * 40)
            path.write_text(json.dumps(workflow))
            # A preceding valid workflow must not be written before this error.
            earlier = self.workflows / 'ci.yml'
            earlier.write_text('permissions: {contents: read}\njobs: {test: {runs-on: ubuntu-latest, steps: []}}')
            before = {p: p.read_bytes() for p in (path, earlier)}
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'vendor-update'):
                render(self.root, 'b' * 40)
            self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_symlink_rejected(self):
        outside = self.root / 'external.yml'
        outside.write_text('jobs: {}')
        (self.workflows / 'ci.yml').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            render(self.root, 'b' * 40)

    def test_scorecard_publisher_keeps_provider_required_hosted_label(self):
        path = self.workflows / 'scorecards.yml'
        path.write_text('''permissions: {contents: read}
jobs:
  scorecard:
    runs-on: ubuntu-latest
    steps:
      - uses: ossf/scorecard-action@aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
        with: {publish_results: true}
  analysis:
    runs-on: ubuntu-latest
    steps: [{run: test}]
''')
        workflow = yaml_editor().load(render(self.root, 'b' * 40)['.github/workflows/scorecards.yml'])
        self.assertEqual(workflow['jobs']['scorecard']['runs-on'], 'ubuntu-latest')
        self.assertIn('vars.CI_RUNNER_LABELS', workflow['jobs']['analysis']['runs-on'])

    def write_workflow(self, workflow, name='ci.yml'):
        path = self.workflows / name
        path.write_text(json.dumps(workflow))
        return path

    def test_mixed_ci_publisher_requires_exact_reviewed_release_role(self):
        source = {'on': {'push': {}}, 'permissions': {'contents': 'read'}, 'jobs': {
            'test': {'runs-on': 'ubuntu-latest', 'steps': [{'run': 'pytest'}]},
            'publish': {'runs-on': 'ubuntu-latest', 'permissions': {'id-token': 'write'},
                        'steps': [{'uses': 'pypa/gh-action-pypi-publish@' + 'a' * 40}]}}}
        path = self.write_workflow(source)
        with self.assertRaisesRegex(ValueError, 'Review job profile ci.yml:publish'):
            render(self.root, 'b' * 40)
        result = render(self.root, 'b' * 40, job_profiles=['ci.yml:publish=release'])
        actual = yaml_editor().load(result['.github/workflows/ci.yml'])
        self.assertEqual(actual['jobs']['test']['runs-on'], runner_labels('ci'))
        self.assertEqual(actual['jobs']['publish']['runs-on'], runner_labels('release'))
        for job in actual['jobs'].values():
            job['runs-on'] = 'ubuntu-latest'
        self.assertEqual(actual, source)
        self.assertEqual(json.loads(path.read_text()), source)
        path.write_text(result['.github/workflows/ci.yml'])
        self.assertEqual(render(self.root, 'b' * 40, job_profiles=['ci.yml:publish=release']), {})

    def test_unknown_permissions_and_referenced_credentials_need_review(self):
        baseline = {'permissions': {'contents': 'read'}, 'jobs': {
            'test': {'runs-on': 'ubuntu-latest', 'steps': [{'run': 'pytest'}]}}}
        cases = []
        unknown = copy.deepcopy(baseline)
        del unknown['permissions']
        cases.append(unknown)
        for location in ('workflow', 'job', 'step', 'container', 'services'):
            source = copy.deepcopy(baseline)
            job = source['jobs']['test']
            if location == 'workflow':
                source['env'] = {'AUTH': '${{ secrets.UPLOAD_TOKEN }}'}
            elif location == 'job':
                job['env'] = {'AUTH': '${{ github["token"] }}'}
            elif location == 'step':
                job['steps'][0]['with'] = {'token': '${{ inputs.token }}'}
            elif location == 'container':
                job['container'] = {'image': 'example', 'credentials': {'password': '${{ inputs.password }}'}}
            else:
                job['services'] = {'db': {'image': 'example', 'credentials': {'password': '${{ secrets.DB }}'}}}
            cases.append(source)
        for source in cases:
            path = self.write_workflow(source)
            before = path.read_bytes()
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, 'Review job profile'):
                render(self.root, 'b' * 40)
            self.assertEqual(path.read_bytes(), before)

    def test_job_permissions_replace_inherited_write_and_ci_override_is_explicit(self):
        source = {'permissions': 'write-all', 'jobs': {
            'test': {'runs-on': 'ubuntu-latest', 'permissions': {'contents': 'read'}, 'steps': [{'run': 'pytest'}]}}}
        self.write_workflow(source)
        self.assertIn('.github/workflows/ci.yml', render(self.root, 'b' * 40))
        source['jobs']['test']['permissions'] = {'contents': 'read', 'security-events': 'write'}
        self.write_workflow(source)
        with self.assertRaisesRegex(ValueError, 'Review job profile'):
            render(self.root, 'b' * 40)
        result = render(self.root, 'b' * 40, job_profiles=['ci.yml:test=ci'])
        self.assertEqual(yaml_editor().load(result['.github/workflows/ci.yml'])['jobs']['test']['runs-on'], runner_labels('ci'))

    def test_whole_secret_context_requires_review_before_any_write(self):
        for expression in ('${{ toJSON(secrets) }}', '${{ toJSON(SECRETS) }}', '${{ secrets[inputs.name] }}'):
            for location in ('workflow-env', 'step-with', 'multiline-run'):
                source = {'permissions': {'contents': 'read'}, 'jobs': {
                    'test': {'runs-on': 'ubuntu-latest', 'steps': [{'run': 'pytest'}]}}}
                if location == 'workflow-env':
                    source['env'] = {'CONTEXT': expression}
                elif location == 'step-with':
                    source['jobs']['test']['steps'][0]['with'] = {'context': expression}
                else:
                    source['jobs']['test']['steps'][0]['run'] = 'echo start\necho ' + expression
                path = self.write_workflow(source)
                before = path.read_bytes()
                with self.subTest(expression=expression, location=location), self.assertRaisesRegex(
                        ValueError, 'credential reference'):
                    render(self.root, 'b' * 40)
                self.assertEqual(path.read_bytes(), before)

    def test_hosted_budget_canonical_keeps_budget_and_requires_ci_role(self):
        source = {'permissions': {'contents': 'read'}, 'jobs': {
            'test': {'runs-on': runner_labels(hosted_budget=True), 'steps': [{'run': 'pytest'}]}}}
        path = self.write_workflow(source)
        before = path.read_bytes()
        for role in ('ci', 'preserve'):
            with self.subTest(role=role):
                self.assertEqual(render(self.root, 'b' * 40, job_profiles=['ci.yml:test=' + role]), {})
        for role in ('automation', 'release', 'hosted'):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'hosted-budget'):
                render(self.root, 'b' * 40, job_profiles=['ci.yml:test=' + role])
        self.assertEqual(path.read_bytes(), before)
        source['jobs']['test']['env'] = {'CONTEXT': '${{ toJSON(secrets) }}'}
        self.write_workflow(source)
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'credential reference'):
            render(self.root, 'b' * 40)
        self.assertEqual(path.read_bytes(), before)

    def test_workflow_run_publisher_requires_hosted_exception(self):
        source = {'on': {'workflow_run': {}}, 'permissions': {'packages': 'write'}, 'jobs': {
            'publish': {'runs-on': 'ubuntu-latest', 'steps': [{'run': 'docker push example'}]}}}
        self.write_workflow(source)
        for role in ('ci', 'automation', 'release'):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, 'Keep workflow_run publisher'):
                render(self.root, 'b' * 40, job_profiles=['ci.yml:publish=' + role])
        self.assertEqual(render(self.root, 'b' * 40, job_profiles=['ci.yml:publish=hosted']), {})

    def test_reviewed_custom_automation_and_fixed_platforms_remain_unchanged(self):
        custom = runner_labels('automation').replace('"ubuntu-latest"', '"ubuntu-24.04"')
        custom = custom.replace("github.event_name)", "github.event_name) && github.event_name != 'workflow_run'")
        for runner, role in [('ubuntu-24.04', 'automation'), (custom, 'preserve')]:
            source = {'permissions': {'contents': 'write'}, 'jobs': {
                'approve': {'runs-on': runner, 'steps': [{'run': 'python reviewed_metadata_only.py',
                             'env': {'GH_TOKEN': '${{ secrets.BOT_PAT }}'}}]}}}
            path = self.write_workflow(source, 'auto-approve.yml')
            with self.assertRaisesRegex(ValueError, 'Review local automation steps'):
                render(self.root, 'b' * 40)
            self.assertEqual(render(self.root, 'b' * 40, job_profiles=['auto-approve.yml:approve=' + role]), {})
            self.assertEqual(json.loads(path.read_text()), source)
        with self.assertRaisesRegex(ValueError, 'Use preserve'):
            render(self.root, 'b' * 40, job_profiles=['auto-approve.yml:approve=hosted'])

    def test_overrides_reject_typos_duplicates_and_reusable_jobs(self):
        source = {'permissions': {'contents': 'read'}, 'jobs': {
            'test': {'runs-on': 'ubuntu-24.04', 'steps': [{'run': 'pytest'}]},
            'shared': {'uses': 'owner/repo/.github/workflows/ci.yml@' + 'a' * 40}}}
        self.write_workflow(source)
        for options in (['typo.yml:test=ci'], ['ci.yml:missing=ci'], ['ci.yml:shared=ci'],
                        ['ci.yml:test=ci', 'ci.yml:test=release'], ['ci.yml:test=typo']):
            with self.subTest(options=options), self.assertRaises(ValueError):
                render(self.root, 'b' * 40, job_profiles=options)
        self.assertEqual(render(self.root, 'b' * 40, job_profiles=['ci.yml:test=hosted']), {})

    def test_similar_custom_expression_is_not_a_canonical_selection(self):
        custom = runner_labels('ci').replace('"ubuntu-latest"', '"ubuntu-24.04"')
        source = {'permissions': {'contents': 'read'}, 'jobs': {
            'test': {'runs-on': custom, 'steps': [{'run': 'pytest'}]}}}
        self.write_workflow(source)
        self.assertEqual(render(self.root, 'b' * 40), {})

    def test_publishing_command_needs_review_even_with_read_permissions(self):
        for command in ('npm publish', 'git push', 'docker push example', 'twine upload dist/*'):
            for prefix in ('', 'echo ok\n', 'echo ok\r\n  ', 'echo ok\n\t'):
                self.write_workflow({'permissions': {'contents': 'read'}, 'jobs': {
                    'publish': {'runs-on': 'ubuntu-latest', 'steps': [{'run': prefix + command}]}}})
                with self.subTest(command=command, prefix=prefix), self.assertRaisesRegex(ValueError, 'publication or deployment'):
                    render(self.root, 'b' * 40)
