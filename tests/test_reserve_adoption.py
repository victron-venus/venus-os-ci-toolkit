"""Consumer adoption preserves all fields except explicitly owned routing and pins."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from prepare_runner_reserve import render  # noqa: E402 - Import the source scripts after adding their directory.
from runner_selection import yaml_editor  # noqa: E402 - Import the source scripts after adding their directory.


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

    def test_automation_steps_and_nonimmutable_pins_rejected(self):
        for ref in ('main', 'a' * 39, 'b' * 41):
            with self.assertRaises(ValueError):
                render(self.root, ref)
        (self.workflows / 'auto-approve.yml').write_text('jobs: {unsafe: {runs-on: ubuntu-latest, steps: [{run: test}]}}')
        with self.assertRaisesRegex(ValueError, 'automation steps'):
            render(self.root, 'b' * 40)

    def test_symlink_rejected(self):
        outside = self.root / 'external.yml'
        outside.write_text('jobs: {}')
        (self.workflows / 'ci.yml').symlink_to(outside)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            render(self.root, 'b' * 40)

    def test_scorecard_publisher_keeps_provider_required_hosted_label(self):
        path = self.workflows / 'scorecards.yml'
        path.write_text('''jobs:
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
