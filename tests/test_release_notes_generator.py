"""Generated consumers validate their actual changelog before release builds."""

import importlib.util
import json
import subprocess  # nosec B404 - fixed local test argv, never a shell
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("notes_installer", SCRIPTS / "install_release.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class ReleaseNotesGeneratorTests(unittest.TestCase):
    """Run the emitted test in an independent consumer directory."""

    def test_generated_checkout_contract_accepts_valid_notes_and_rejects_missing_guidance(self):
        """The contract invokes the real parser, rather than searching headings."""
        policy = {
            "repository": "example/project", "mode": "release",
            "version_file": "version", "release_notes": "CHANGELOG.md",
            "validation_workflows": ["ci.yml"],
            "versioning": {
                "schema": 1, "promotion": "promote-bytes",
                "files": [{"path": "version", "format": "text", "value": "package"}],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = root / ".github/workflows/release-build.yml"
            adapter.parent.mkdir(parents=True)
            adapter.write_text(
                "on: {workflow_call: {inputs: {release_plan_artifact: "
                "{type: string, required: true}}}}\njobs: {}\n"
            )
            (adapter.parent / "ci.yml").write_text("on: {workflow_call: {}}\njobs: {}\n")
            (root / ".release-policy.json").write_text(json.dumps(policy))
            (root / "version").write_text("1.2.3\n")
            files = installer.render(root)
            path = ".github/release-tests/test_checkout_release_notes.py"
            self.assertIn(path, files)
            for name, content in files.items():
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            for guidance, success in (
                ("### Upgrade\nKeep a rollback package.\n### Security\nKeep TLS enabled.\n", True),
                ("### Security\nKeep TLS enabled.\n", False),
                ("### Upgrade\nKeep a rollback package.\n### Security\n<!-- hidden -->\n", False),
            ):
                with self.subTest(guidance=guidance):
                    (root / "CHANGELOG.md").write_text("## [1.2.3]\n" + guidance)
                    checked = subprocess.run(  # nosec B603 - fixed local test command
                        [sys.executable, "-m", "unittest", "discover", "-s", ".github/release-tests",
                         "-p", "test_checkout_release_notes.py", "-v"],
                        cwd=root, capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(checked.returncode == 0, success, checked.stdout + checked.stderr)
                    self.assertIn("test_current_version_has_publishable_source_bound_notes", checked.stderr)
                    if not success:
                        self.assertIn("guidance", checked.stderr)
            policy.pop("release_notes")
            (root / ".release-policy.json").write_text(json.dumps(policy))
            self.assertNotIn(path, installer.render(root))

    def test_generated_contract_defers_unselected_versions_but_not_broken_committed_versions(self):
        """Manual/tag-derived bases belong to prepare, not a shallow review checkout."""
        for selection, skipped in (
            ({}, True),
            ({"version_from_tags": True}, True),
            ({"version": "1.2.3"}, False),
            ({"version_file": "missing-version"}, False),
        ):
            with self.subTest(selection=selection), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                policy = {
                    "repository": "example/project", "mode": "release",
                    "release_notes": "CHANGELOG.md", "validation_workflows": ["ci.yml"],
                    **selection,
                }
                adapter = root / ".github/workflows/release-build.yml"
                adapter.parent.mkdir(parents=True)
                adapter.write_text("on: {workflow_call: {}}\njobs: {}\n")
                (adapter.parent / "ci.yml").write_text("on: {workflow_call: {}}\njobs: {}\n")
                (root / ".release-policy.json").write_text(json.dumps(policy))
                (root / "CHANGELOG.md").write_text("## [1.2.3]\nMissing release guidance.\n")
                for name, content in installer.render(root).items():
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
                checked = subprocess.run(  # nosec B603 - fixed local test command
                    [sys.executable, "-m", "unittest", "discover", "-s", ".github/release-tests",
                     "-p", "test_checkout_release_notes.py", "-v"],
                    cwd=root, capture_output=True, text=True, check=False,
                )
                self.assertEqual(checked.returncode == 0, skipped, checked.stdout + checked.stderr)
                self.assertEqual("skipped=1" in checked.stderr, skipped, checked.stderr)
