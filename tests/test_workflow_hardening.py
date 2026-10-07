"""Generated workflows retain the reviewed runner protection across refreshes."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from workflow_hardening import harden_jobs, harden_step


class WorkflowHardeningTests(unittest.TestCase):
    def test_protection_precedes_checkout_and_preserves_original(self):
        workflow = {"jobs": {"build": {"runs-on": "ubuntu-latest", "steps": [{"uses": "actions/checkout@abc"}, {"run": "pytest"}]}}}
        original = copy.deepcopy(workflow)
        hardened = harden_jobs(workflow)
        self.assertEqual(hardened["jobs"]["build"]["steps"], [harden_step(), *original["jobs"]["build"]["steps"]])
        self.assertEqual(workflow, original)
        self.assertEqual(harden_jobs(hardened), hardened)

    def test_reusable_callers_and_job_containers_are_preserved(self):
        workflow = {"jobs": {"call": {"uses": "./.github/workflows/check.yml"}, "container": {"runs-on": "ubuntu-latest", "container": "python:3.12", "steps": [{"run": "pytest"}]}}}
        self.assertEqual(harden_jobs(workflow), workflow)


if __name__ == "__main__":
    unittest.main()
