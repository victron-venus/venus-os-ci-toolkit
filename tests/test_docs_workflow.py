"""Contract for the reusable documentation check workflow.

The workflow exists so a repository can run the shared checker without vendoring
it. These tests pin the properties that make that safe: it is callable only, it
requires an immutable toolkit commit, every action is pinned to a full SHA, and
it invokes the checker that ships in this repository.
"""

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docs-check.yml"
CHECKER = ROOT / "scripts/check_docs.py"
PINNED = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")


class DocumentationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.source = WORKFLOW.read_text(encoding="utf-8")
        self.workflow = yaml.load(self.source, Loader=yaml.BaseLoader)

    def test_it_is_only_callable(self):
        triggers = self.workflow.get("on") or self.workflow.get(True)
        self.assertEqual(list(triggers), ["workflow_call"])

    def test_the_toolkit_commit_is_required(self):
        inputs = (self.workflow.get("on") or self.workflow.get(True))["workflow_call"]["inputs"]
        self.assertEqual(inputs["toolkit-ref"]["required"], "true")

    def test_every_action_is_pinned_to_a_full_sha(self):
        pins = {pin['packageName']: pin['digest']
                for pin in json.loads((ROOT / '.github/action-pins.json').read_text())}
        for job in self.workflow["jobs"].values():
            for step in job["steps"]:
                reference = step.get("uses")
                if reference is None:
                    continue
                with self.subTest(reference=reference):
                    self.assertRegex(reference, PINNED)
                    action, revision = reference.rsplit('@', 1)
                    if action in pins:
                        self.assertEqual(revision, pins[action])

    def test_mutable_or_injected_checker_refs_fail_before_checkout(self):
        steps = self.workflow['jobs']['docs']['steps']
        guard = steps[0]
        self.assertEqual(guard['shell'], 'bash')
        self.assertEqual(guard['env'], {'TOOLKIT_REF': '${{ inputs.toolkit-ref }}'})
        self.assertNotIn('${{', guard['run'])
        checker = next(step for step in steps
                       if step.get('with', {}).get('path') == 'toolkit')
        self.assertEqual(checker['with']['ref'], '${{ inputs.toolkit-ref }}')
        for reference, accepted in (
            ('a' * 40, True), ('A1' * 20, True), ('main', False),
            ('v1.0.0', False), ('a' * 39, False), ('a' * 41, False),
            ('g' * 40, False), ('', False), ('a' * 40 + '\n', False),
            ('$(exit 0)', False), ('"; exit 0; #', False),
        ):
            with self.subTest(reference=reference):
                result = subprocess.run(
                    ['bash', '--noprofile', '--norc', '-e', '-c', guard['run']],
                    env={**os.environ, 'TOOLKIT_REF': reference},
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_permissions_are_read_only(self):
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})
        for job in self.workflow['jobs'].values():
            self.assertIn(job.get('permissions'), (None, {'contents': 'read'}))
            self.assertNotIn('secrets', job)

    def test_it_runs_the_shared_checker_from_the_toolkit(self):
        steps = self.workflow["jobs"]["docs"]["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("toolkit/scripts/check_docs.py", commands)
        self.assertIn("--checks", commands)

    def test_the_named_checker_exists(self):
        self.assertTrue(CHECKER.is_file())


if __name__ == "__main__":
    unittest.main()
