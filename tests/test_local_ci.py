"""Verify local-only private CI without GitHub or paid runner dependencies."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    """Load an executable module without invoking its command-line entrypoint."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


local = load("local_ci")
installer = load("install_release")


class LocalCITests(unittest.TestCase):
    """Exercise real local commands, failure propagation and workflow removal."""

    def setUp(self):
        """Create an isolated policy whose commands cannot touch a real repository."""
        self.temp = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts/ci.sh").write_text("exit 0\n")
        self.policy = {
            "repository": "owner/private",
            "visibility": "private",
            "mode": "validation-only",
            "ci_execution": "local",
            "validation_workflows": [],
            "local_checks": ["printf first > result"],
        }
        self.enterContext(mock.patch.object(local, "ROOT", self.root))

    def save(self):
        """Write the complete checked-in policy used by one scenario."""
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))

    def test_checks_execute_in_order_and_stop_on_failure(self):
        """A failed security command cannot be hidden by a later successful command."""
        self.policy["local_checks"] += ["exit 7", "printf bypass >> result"]
        self.save()
        with mock.patch("sys.argv", ["release.py", "check"]):
            self.assertEqual(local.main(), 1)
        self.assertEqual((self.root / "result").read_text(), "first")

    def test_status_and_doctor_never_launch_processes(self):
        """Configuration display requires neither a GitHub token nor a runner."""
        self.save()
        for command in ("status", "doctor"):
            with (
                mock.patch("sys.argv", ["release.py", command]),
                mock.patch.object(subprocess, "run") as run,
            ):
                self.assertEqual(local.main(), 0)
            run.assert_not_called()

    def test_invalid_policy_is_rejected_before_any_command(self):
        """An invalid trailing command fails before the valid first command starts."""
        self.policy["local_checks"].append(None)
        self.save()
        with mock.patch("sys.argv", ["release.py", "check"]):
            self.assertEqual(local.main(), 1)
        self.assertFalse((self.root / "result").exists())

    def test_render_has_no_hosted_workflow_and_preserves_publication_boundary(self):
        """Local rendering emits only the nonpublishing client and local runbook."""
        self.save()
        files = installer.render(self.root)
        self.assertEqual(set(files), {"scripts/release.py", "docs/release-workflow.md"})
        self.assertIn(
            'choices=["check", "status", "doctor"]', files["scripts/release.py"]
        )
        self.assertNotIn('"gh"', files["scripts/release.py"])
        workflow = self.root / ".github/workflows/quality-gate.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: old hosted workflow\n")
        with self.assertRaisesRegex(ValueError, "Archive"):
            installer.render(self.root)

    def test_only_manual_self_hosted_jobs_can_remain(self):
        """A manual deploy must not hide a hosted prerequisite or an automatic trigger."""
        self.save()
        workflow = self.root / ".github/workflows/deploy.yml"
        workflow.parent.mkdir(parents=True)
        manual = "on: {workflow_dispatch: {}}\njobs:\n  deploy:\n    runs-on: [self-hosted, LAN]\n"
        workflow.write_text(manual)
        installer.render(self.root)
        for unavailable in (
            manual + "  select:\n    runs-on: ubuntu-latest\n",
            manual
            + "  validate:\n    uses: owner/repo/.github/workflows/ci.yml@main\n",
            manual.replace("workflow_dispatch", "push"),
        ):
            workflow.write_text(unavailable)
            with self.assertRaisesRegex(ValueError, "Archive"):
                installer.render(self.root)


if __name__ == "__main__":
    unittest.main()
