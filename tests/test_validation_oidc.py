"""Exercise explicit OIDC capabilities across generated reusable workflow calls."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "install_release", ROOT / "scripts/install_release.py"
)
assert SPEC is not None
assert SPEC.loader is not None
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class ValidationOidcTests(unittest.TestCase):
    """OIDC is opt-in and must survive every permission cap in the call chain."""

    def setUp(self):
        """Build a token-requesting validator and an unrelated read-only job."""
        temporary = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workflows = self.root / ".github/workflows"
        self.workflows.mkdir(parents=True)
        self.policy = {
            "repository": "owner/repo",
            "mode": "release",
            "validation_workflows": ["python-ci.yml", "lint.yml"],
            "validation_oidc_workflows": ["python-ci.yml"],
        }
        self.validator = {
            "on": {"workflow_call": {}},
            "permissions": {"contents": "read"},
            "jobs": {
                "test": {
                    "permissions": {"contents": "read", "id-token": "write"},
                    "runs-on": "ubuntu-latest",
                    "steps": [{"run": "true"}],
                }
            },
        }
        self.write("python-ci.yml", self.validator)
        readonly = {
            "on": {"workflow_call": {}},
            "permissions": {"contents": "read"},
            "jobs": {"run": {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}},
        }
        self.write("lint.yml", readonly)
        self.write("release-build.yml", readonly)

    def write(self, name, value):
        """Persist only a temporary test fixture, preserving YAML string keys."""
        (self.workflows / name).write_text(yaml.safe_dump(value))

    def test_quality_grants_only_the_selected_validator(self):
        """Both release and validation-only modes use the same narrow job cap."""
        for mode in ("release", "validation-only"):
            with self.subTest(mode=mode):
                policy = self.policy | {"mode": mode}
                workflow = installer.quality(policy, self.root)
                self.assertNotIn("id-token", workflow["permissions"])
                grants = {
                    name
                    for name, job in workflow["jobs"].items()
                    if job.get("permissions", {}).get("id-token") == "write"
                }
                self.assertEqual(grants, {"check-0"})
                installer.validate_workflow_adapters(self.root, policy)

    def test_release_forwards_only_through_checks(self):
        """The actual release -> quality -> validator graph accepts the capability."""
        self.write("quality-gate.yml", installer.quality(self.policy, self.root))
        workflow = installer.release(self.policy)
        self.assertNotIn("id-token", workflow["permissions"])
        grants = {
            name
            for name, job in workflow["jobs"].items()
            if job.get("permissions", {}).get("id-token") == "write"
        }
        self.assertEqual(grants, {"checks"})
        installer.validate_local_calls(self.root, workflow)
        del workflow["jobs"]["checks"]["permissions"]["id-token"]
        with self.assertRaisesRegex(ValueError, "id-token.*write.*none"):
            installer.validate_local_calls(self.root, workflow)

    def test_versioned_release_preserves_the_same_narrow_forwarding(self):
        """Promote-bytes and final-build inherit the checked legacy caller cap."""
        self.write("quality-gate.yml", installer.quality(self.policy, self.root))
        for promotion in ("promote-bytes", "final-build"):
            with self.subTest(promotion=promotion):
                policy = self.policy | {
                    "versioning": {
                        "schema": 1,
                        "promotion": promotion,
                        "build_number_floor": 50,
                        "files": [
                            {"path": "version", "format": "text", "value": "package"}
                        ],
                    }
                }
                workflow = installer.release(policy)
                grants = {
                    name
                    for name, job in workflow["jobs"].items()
                    if job.get("permissions", {}).get("id-token") == "write"
                }
                self.assertEqual(grants, {"checks"})
                installer.validate_local_calls(self.root, workflow)

    def test_undeclared_validator_capability_is_rejected(self):
        """A leaf cannot elevate permissions without the reviewed policy opt-in."""
        for selected in ([], ["lint.yml"]):
            with (
                self.subTest(selected=selected),
                self.assertRaisesRegex(ValueError, "id-token.*write.*none"),
            ):
                installer.validate_workflow_adapters(
                    self.root, self.policy | {"validation_oidc_workflows": selected}
                )

    def test_nested_wrapper_must_forward_the_selected_capability(self):
        """An intermediate explicit read-only cap still fails before dispatch."""
        self.write("coverage.yml", self.validator)
        wrapper = {
            "on": {"workflow_call": {}},
            "permissions": {"contents": "read"},
            "jobs": {"coverage": {"uses": "./.github/workflows/coverage.yml"}},
        }
        self.write("python-ci.yml", wrapper)
        with self.assertRaisesRegex(ValueError, "id-token.*write.*none"):
            installer.validate_workflow_adapters(self.root, self.policy)
        wrapper["jobs"]["coverage"]["permissions"] = {
            "contents": "read",
            "id-token": "write",
        }
        self.write("python-ci.yml", wrapper)
        installer.validate_workflow_adapters(self.root, self.policy)

    def test_omitted_and_empty_policy_keep_existing_output(self):
        """Existing consumers gain no new scope or generated-file differences."""
        baseline = {
            k: v for k, v in self.policy.items() if k != "validation_oidc_workflows"
        }
        empty = baseline | {"validation_oidc_workflows": []}
        for render in (installer.quality, installer.release):
            self.assertEqual(render(baseline), render(empty))
            encoded = json.dumps(render(empty))
            self.assertNotIn("id-token", encoded)

    def test_invalid_selections_fail_before_rendering(self):
        """Reject broad, malformed, duplicate, unknown, and recursive grants."""
        invalid = (
            None,
            True,
            {},
            "python-ci.yml",
            [1],
            [[]],
            ["python-ci.yml", "python-ci.yml"],
            ["unknown.yml"],
            ["../python-ci.yml"],
            ["./.github/workflows/python-ci.yml"],
            ["*.yml"],
            ["python-ci.yml\n"],
            ["quality-gate.yml"],
            ["release-pipeline.yml"],
        )
        for selected in invalid:
            for render in (installer.quality, installer.release):
                with (
                    self.subTest(selected=selected, render=render.__name__),
                    self.assertRaisesRegex(ValueError, "validation_oidc_workflows"),
                ):
                    render(self.policy | {"validation_oidc_workflows": selected})

    def test_policy_validation_rejects_invalid_grants_for_local_mode(self):
        """Unsupported hosted capabilities cannot bypass validation via local CI."""
        policy = self.policy | {
            "mode": "validation-only",
            "ci_execution": "local",
            "validation_workflows": [],
        }
        with self.assertRaisesRegex(ValueError, "configured validators"):
            installer.validate_policy(self.root, policy)

    def test_current_toolkit_policy_does_not_gain_oidc(self):
        """The canonical repository does not opt itself into third-party OIDC."""
        policy = json.loads((ROOT / ".release-policy.json").read_text())
        workflow = installer.quality(policy, ROOT)
        self.assertNotIn("id-token", json.dumps(workflow))


if __name__ == "__main__":
    unittest.main()
