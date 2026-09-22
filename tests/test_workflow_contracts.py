"""Exercise the checks shipped to every repository's required CI gate."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml

SPEC = importlib.util.spec_from_file_location(
    "contracts", Path(__file__).resolve().parents[1] / "scripts/workflow_contracts.py"
)
contracts = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contracts)


class WorkflowContractsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".github/workflows").mkdir(parents=True)
        (self.root / ".release-policy.json").write_text(
            json.dumps({"validation_workflows": ["ci.yml"]})
        )
        self.ci = {
            "on": {"workflow_call": {}},
            "jobs": {
                "test": {"runs-on": "ubuntu-latest", "timeout-minutes": 10, "steps": []}
            },
        }
        self.gate = {
            "jobs": {
                "check-0": {"uses": "./.github/workflows/ci.yml"},
                "workflow-contracts": {},
                "gate": {"needs": ["check-0", "workflow-contracts"]},
            }
        }

    def validate(self):
        for name, workflow in [("ci.yml", self.ci), ("quality-gate.yml", self.gate)]:
            (self.root / ".github/workflows" / name).write_text(
                yaml.safe_dump(workflow)
            )
        contracts.validate(self.root)

    def test_valid_graph(self):
        self.validate()

    def test_duplicate_event_is_rejected(self):
        self.ci["on"]["pull_request"] = {}
        with self.assertRaisesRegex(ValueError, "through Quality gate"):
            self.validate()

    def test_missing_timeout_is_rejected(self):
        del self.ci["jobs"]["test"]["timeout-minutes"]
        with self.assertRaisesRegex(ValueError, "explicit timeout"):
            self.validate()

    def test_mixed_codeql_version_is_rejected(self):
        self.ci["jobs"]["test"]["steps"] = [
            {"uses": "github/codeql-action/init@" + "a" * 40},
            {"uses": "github/codeql-action/analyze@" + "b" * 40},
        ]
        with self.assertRaisesRegex(ValueError, "one full commit"):
            self.validate()

    def test_bypassed_validation_is_rejected(self):
        self.gate["jobs"]["gate"]["needs"] = ["workflow-contracts"]
        with self.assertRaisesRegex(ValueError, "every configured"):
            self.validate()

    def test_transitive_cycle_is_rejected(self):
        self.ci["jobs"]["again"] = {"uses": "./.github/workflows/ci.yml"}
        with self.assertRaisesRegex(ValueError, "Recursive"):
            self.validate()

    def test_independent_pr_validator_is_rejected(self):
        (self.root / ".github/workflows/forgotten.yml").write_text(
            yaml.safe_dump({"on": {"pull_request": {}}, "jobs": {}})
        )
        with self.assertRaisesRegex(ValueError, "outside the required gate"):
            self.validate()
