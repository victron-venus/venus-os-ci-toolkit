"""Exercise the workflow's real uv install command against an offline wheelhouse."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


class LockedPythonDependencies(unittest.TestCase):
    """A lock selects exact packages and cannot be repaired silently during CI."""

    def setUp(self):
        self.assertIsNotNone(shutil.which("uv"), "Install the pinned uv test runtime")
        # pylint: disable-next=consider-using-with
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.directory / "nested project"
        self.project.mkdir()
        self.wheels = self.directory / "wheels"
        self.wheels.mkdir()
        for version in ("1.0.0", "2.0.0"):
            self.make_wheel(version)
        self.manifest = self.project / "pyproject.toml"
        self.manifest.write_text(
            '[project]\nname = "ci-lock-consumer"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\ndependencies = ["locked-fixture==1.0.0"]\n'
        )
        self.path_file = self.directory / "github-path"
        self.environment = {
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "HOME": str(self.directory),
            "UV_CACHE_DIR": str(self.directory / "cache"),
            "UV_FIND_LINKS": str(self.wheels),
            "UV_NO_INDEX": "true",
            "UV_OFFLINE": "true",
            "UV_PYTHON_DOWNLOADS": "never",
            "GITHUB_PATH": str(self.path_file),
        }
        workflow = yaml.safe_load((ROOT / ".github/workflows/python-ci.yml").read_text())
        self.steps = {s["name"]: s for s in workflow["jobs"]["ci"]["steps"]}
        self.install = self.steps["Install locked dependencies"]
        self.assertEqual(
            self.install["if"], "inputs.install-dependencies && inputs.use-uv-lock"
        )
        self.assertFalse(workflow[True]["workflow_call"]["inputs"]["use-uv-lock"]["default"])
        self.assertEqual(
            self.steps["Install dependencies"]["if"],
            "inputs.install-dependencies && !inputs.use-uv-lock",
        )

    def make_wheel(self, version):
        """Create two real installable releases without network access."""
        name = f"locked_fixture-{version}"
        with zipfile.ZipFile(self.wheels / f"{name}-py3-none-any.whl", "w") as wheel:
            wheel.writestr("locked_fixture/__init__.py", f'VERSION = "{version}"\n')
            info = f"{name}.dist-info"
            wheel.writestr(
                f"{info}/METADATA",
                f"Metadata-Version: 2.1\nName: locked-fixture\nVersion: {version}\n",
            )
            wheel.writestr(
                f"{info}/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(f"{info}/RECORD", "")

    def run_command(self, *command):
        """Run in the isolated consumer, never the toolkit's environment."""
        return subprocess.run(
            command, cwd=self.project, env=self.environment,
            capture_output=True, text=True, check=False,
        )

    def lock(self):
        """Prepare a lock as a developer would before handing it to CI."""
        result = self.run_command("uv", "lock", "--python", sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.project / "uv.lock").read_bytes()

    def sync(self):
        """Execute the workflow shell verbatim with fail-fast semantics."""
        return self.run_command("bash", "-e", "-c", self.install["run"])

    def test_exact_version_and_environment_are_used_without_mutating_lock(self):
        """A newer available release cannot replace the committed version."""
        original = self.lock()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        environment_bin = Path(self.path_file.read_text().strip())
        self.assertEqual(environment_bin, self.project / ".venv/bin")
        result = self.run_command(
            str(environment_bin / "python"), "-c",
            "import json,sys,locked_fixture; print(json.dumps([locked_fixture.VERSION,sys.prefix]))",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["1.0.0", str(self.project / ".venv")])
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)

    def test_stale_lock_is_rejected_without_rewriting(self):
        """A manifest update without its lock must fail the required job."""
        original = self.lock()
        self.manifest.write_text(self.manifest.read_text().replace("==1.0.0", "==2.0.0"))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", result.stderr)
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)
        self.assertFalse(self.path_file.exists())

    def test_missing_lock_fails_before_installation(self):
        """Opting in must not silently create an unreviewed lock."""
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / "uv.lock").exists())
        self.assertFalse(self.path_file.exists())

    def test_missing_nested_project_cannot_install_from_parent(self):
        """uv's upward discovery must not hide a wrong working-directory input."""
        self.lock()
        parent_lock = (self.project / "uv.lock").read_bytes()
        parent = self.project
        self.project = parent / "missing nested project"
        self.project.mkdir()
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.path_file.exists())
        self.assertFalse((self.project / ".venv").exists())
        self.assertFalse((parent / ".venv").exists())
        self.assertEqual((parent / "uv.lock").read_bytes(), parent_lock)

    def test_missing_mypy_cannot_be_installed_over_locked_environment(self):
        """The existing check must use a tool selected by the consumer lock."""
        self.lock()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.environment["PATH"] = self.path_file.read_text().strip() + ":" + self.environment["PATH"]
        self.environment["LOCKED_DEPENDENCIES"] = "true"
        result = self.run_command("bash", "-e", "-c", self.steps["Install MyPy"]["run"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named mypy", result.stderr)

    def test_locked_checks_do_not_fall_back_to_global_tools(self):
        """A globally available executable must not mask a missing locked tool."""
        self.lock()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        global_bin = self.directory / "global-bin"
        global_bin.mkdir()
        marker = self.directory / "global-tool-ran"
        for name in ("ruff", "pytest"):
            executable = global_bin / name
            executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            executable.chmod(0o755)
        self.environment["PATH"] = (
            self.path_file.read_text().strip() + ":" + str(global_bin)
            + ":" + self.environment["PATH"]
        )
        for step_name, module in (("Run Ruff lint", "ruff"), ("Run Tests", "pytest")):
            with self.subTest(step=step_name):
                command = self.steps[step_name]["run"]
                command = command.replace("${{ inputs.test-args }}", "")
                command = command.replace("${{ inputs.coverage-threshold }}", "0")
                result = self.run_command("bash", "-e", "-c", command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"No module named {module}", result.stderr)
                self.assertFalse(marker.exists())
