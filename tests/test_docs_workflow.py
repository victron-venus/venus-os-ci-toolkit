"""Contract for the reusable documentation check workflow.

The workflow exists so a repository can run the shared checker without vendoring
it. These tests pin the properties that make that safe: it is callable only, it
requires an immutable toolkit commit, every action is pinned to a full SHA, and
it invokes the checker that ships in this repository.
"""

import re
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
        for job in self.workflow["jobs"].values():
            for step in job["steps"]:
                reference = step.get("uses")
                if reference is None:
                    continue
                with self.subTest(reference=reference):
                    self.assertRegex(reference, PINNED)

    def test_permissions_are_read_only(self):
        self.assertEqual(self.workflow.get("permissions"), {"contents": "read"})

    def test_it_runs_the_shared_checker_from_the_toolkit(self):
        steps = self.workflow["jobs"]["docs"]["steps"]
        commands = "\n".join(step.get("run", "") for step in steps)
        self.assertIn("toolkit/scripts/check_docs.py", commands)
        self.assertIn("--checks", commands)

    def test_the_named_checker_exists(self):
        self.assertTrue(CHECKER.is_file())


if __name__ == "__main__":
    unittest.main()
