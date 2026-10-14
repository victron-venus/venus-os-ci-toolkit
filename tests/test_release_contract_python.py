"""Keep release acceptance tests on their explicitly supported interpreter."""

import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "contract_python_installer", ROOT / "scripts/install_release.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class ReleaseContractPythonTests(unittest.TestCase):
    """Exercise serialized jobs and reject ambiguous policy values before install."""

    def setUp(self):
        self.policy = {
            "repository": "example/project",
            "mode": "release",
            "single_entry_ci": True,
            "validation_workflows": ["ci.yml"],
        }

    def test_default_and_supported_interpreters_preserve_other_jobs(self):
        for version in (None, "3.11", "3.12", "3.13", "3.14"):
            with self.subTest(version=version):
                policy = dict(self.policy)
                if version is not None:
                    policy["release_contract_python"] = version
                workflow = installer.yaml.load(
                    installer.dump(installer.quality(policy)),
                    Loader=installer.WorkflowLoader,
                )
                jobs = workflow["jobs"]
                for job, expected in (
                    ("release-contracts", version or "3.12"),
                    ("workflow-contracts", "3.12"),
                ):
                    setup = next(
                        step for step in jobs[job]["steps"]
                        if step.get("uses", "").startswith("actions/setup-python@")
                    )
                    self.assertEqual(setup["with"]["python-version"], expected)
                self.assertEqual(jobs["check-0"]["uses"], "./.github/workflows/ci.yml")
                self.assertIn("release-contracts", jobs["gate"]["needs"])

    def test_invalid_values_fail_policy_validation_and_direct_rendering(self):
        invalid = (None, True, 3.14, ["3.14"], {"version": "3.14"}, "", "3.10", "3.15", "3.x", "3.14-dev", "${{ inputs.python }}")
        with tempfile.TemporaryDirectory() as temporary:
            for version in invalid:
                with self.subTest(version=version):
                    policy = dict(self.policy, release_contract_python=version)
                    with self.assertRaisesRegex(ValueError, "release_contract_python"):
                        installer.validate_policy(Path(temporary), policy)
                    with self.assertRaisesRegex(ValueError, "release_contract_python"):
                        installer.quality(policy)

    def test_validation_only_does_not_gain_release_contracts(self):
        policy = dict(self.policy, mode="validation-only", release_contract_python="3.14")
        workflow = installer.quality(policy)
        self.assertNotIn("release-contracts", workflow["jobs"])


if __name__ == "__main__":
    unittest.main()
