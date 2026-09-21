"""Reject nested workflow permission errors before GitHub refuses to start a run."""

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
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class WorkflowPermissionTests(unittest.TestCase):
    """Model the caller cap, including declarations on jobs that will be skipped."""

    def setUp(self):
        """Create callable wrapper and scanner fixtures without GitHub access."""
        temporary = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".github/workflows").mkdir(parents=True)
        self.policy = {
            "repository": "owner/repo",
            "mode": "validation-only",
            "validation_workflows": ["security-required.yml"],
        }
        self.wrapper = {
            "on": {"workflow_call": {}},
            "permissions": {"contents": "read", "actions": "read"},
            "jobs": {
                "scan": {
                    "uses": "./.github/workflows/security-scan.yml",
                    "with": {"run-codeql": False},
                }
            },
        }
        self.scanner = {
            "on": {"workflow_call": {}},
            "jobs": {
                "codeql": {
                    "if": "inputs.run-codeql",
                    "runs-on": "ubuntu-latest",
                    "permissions": {"security-events": "write", "contents": "read"},
                    "steps": [{"run": "true"}],
                }
            },
        }

    def validate(self):
        """Write the selected declarations and run the real installer preflight."""
        for name, workflow in [
            ("security-required.yml", self.wrapper),
            ("security-scan.yml", self.scanner),
        ]:
            (self.root / ".github/workflows" / name).write_text(
                yaml.safe_dump(workflow)
            )
        installer.validate_workflow_adapters(self.root, self.policy)

    def test_skipped_nested_codeql_still_requires_caller_permission(self):
        """Reproduce the merged toolkit's startup failure despite run-codeql=false."""
        with self.assertRaisesRegex(ValueError, "security-events.*write.*none"):
            self.validate()

    def test_explicit_call_permission_allows_nested_scanner(self):
        """The wrapper can forward the scope already granted by Quality gate."""
        self.wrapper["jobs"]["scan"]["permissions"] = {
            "contents": "read",
            "actions": "read",
            "security-events": "write",
        }
        self.validate()

    def test_omitted_permissions_inherit_the_caller_cap(self):
        """An omitted permissions block inherits instead of revoking every scope."""
        del self.wrapper["permissions"]
        self.validate()

    def test_private_gate_rejects_public_only_write_scope(self):
        """A private policy cannot inherit Code Security write permissions."""
        self.policy["visibility"] = "private"
        del self.wrapper["permissions"]
        with self.assertRaisesRegex(ValueError, "security-events.*write.*none"):
            self.validate()

    def test_read_all_is_not_enough_for_write_scope(self):
        """Read-only wildcard permissions cannot forward a write request."""
        self.wrapper["permissions"] = "read-all"
        with self.assertRaises(ValueError):
            self.validate()

    def test_recursive_local_workflows_are_rejected(self):
        """A callable wrapper cannot loop back into itself through another file."""
        self.wrapper["jobs"]["scan"]["uses"] = (
            "./.github/workflows/security-required.yml"
        )
        with self.assertRaisesRegex(ValueError, "Recursive"):
            self.validate()

    def test_codeql_mixed_versions_fail_before_workflow_generation(self):
        """A dependency bump of only one action must not render a broken release gate."""
        del self.wrapper["permissions"]
        self.scanner["jobs"]["codeql"]["steps"] = [
            {"uses": "github/codeql-action/init@" + "a" * 40},
            {"uses": "github/codeql-action/autobuild@" + "b" * 40},
            {"uses": "github/codeql-action/analyze@" + "a" * 40},
        ]
        with self.assertRaisesRegex(ValueError, "CodeQL job codeql.*same SHA"):
            self.validate()

    def test_codeql_manual_build_can_omit_autobuild(self):
        """An init/analyze pair remains valid when the build is explicit."""
        del self.wrapper["permissions"]
        self.scanner["jobs"]["codeql"]["steps"] = [
            {"uses": "github/codeql-action/init@" + "a" * 40},
            {"run": "make"},
            {"uses": "github/codeql-action/analyze@" + "a" * 40},
        ]
        self.validate()

    def test_codeql_mutable_versions_are_rejected(self):
        """Matching mutable tags do not establish a reviewed immutable toolchain."""
        del self.wrapper["permissions"]
        self.scanner["jobs"]["codeql"]["steps"] = [
            {"uses": "github/codeql-action/init@v4"},
            {"uses": "github/codeql-action/analyze@v4"},
        ]
        with self.assertRaisesRegex(ValueError, "CodeQL job codeql.*full commit SHA"):
            self.validate()

    def test_real_toolkit_quality_gate_has_valid_nested_permissions(self):
        """Keep the repository's actual call chain startup-valid after edits."""
        installer.validate_workflow_adapters(
            ROOT, json.loads((ROOT / ".release-policy.json").read_text())
        )


if __name__ == "__main__":
    unittest.main()
