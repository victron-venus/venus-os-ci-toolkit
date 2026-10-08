"""Execute the composite's real pip command against isolated offline fixtures."""

import hashlib
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tempfile
import unittest
import venv
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


class SetupPythonActionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="python action ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.consumer = self.root / "unrelated consumer"
        self.consumer.mkdir()
        # A relative requirements lookup must fail rather than accidentally pass.
        (self.consumer / "requirements.txt").write_text("missing-consumer-package==0\n")
        self.action = self.root / "toolkit action"
        self.action.mkdir()
        self.wheels = self.root / "wheels"
        self.wheels.mkdir()
        self.environment = self.root / "environment"
        venv.EnvBuilder(with_pip=True).create(self.environment)
        binaries = self.environment / ("Scripts" if os.name == "nt" else "bin")
        self.python = binaries / ("python.exe" if os.name == "nt" else "python")
        self.env = {
            **os.environ,
            "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
            "GITHUB_ACTION_PATH": str(self.action),
            "PIP_NO_INDEX": "1",
            "PIP_FIND_LINKS": self.wheels.as_uri(),
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
        }
        self.steps = yaml.safe_load(
            (ROOT / "actions/setup-python/action.yml").read_text()
        )["runs"]["steps"]
        self.install = next(s for s in self.steps if s.get("name") == "Install common tools")
        self.assertEqual(self.install["env"]["GITHUB_ACTION_PATH"], "${{ github.action_path }}")
        self.wheel = self.wheels / "action_fixture-1.0.0-py3-none-any.whl"
        with zipfile.ZipFile(self.wheel, "w") as wheel:
            wheel.writestr("action_fixture.py", "VALUE = 42\n")
            info = "action_fixture-1.0.0.dist-info"
            wheel.writestr(info + "/METADATA", "Metadata-Version: 2.1\nName: action-fixture\nVersion: 1.0.0\n")
            wheel.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            wheel.writestr(info + "/RECORD", "")
        self.digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()

    def run_install(self, digest):
        (self.action / "requirements.txt").write_text(
            f"action-fixture==1.0.0 --hash=sha256:{digest}\n"
        )
        return subprocess.run(
            [shutil.which("bash"), "-eo", "pipefail", "-c", self.install["run"]],
            cwd=self.consumer, env=self.env, text=True, capture_output=True, timeout=60,
            check=False,
        )

    def test_install_uses_action_directory_from_an_unrelated_consumer(self):
        result = self.run_install(self.digest)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = subprocess.run(
            [str(self.python), "-c", "import action_fixture; print(action_fixture.VALUE)"],
            cwd=self.consumer, env=self.env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "42")

    def test_tampered_hash_prevents_install(self):
        result = self.run_install("0" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DO NOT MATCH THE HASHES", result.stdout + result.stderr)
        result = subprocess.run(
            [str(self.python), "-c", "import importlib.util; print(importlib.util.find_spec('action_fixture'))"],
            cwd=self.consumer, env=self.env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "None")

    def test_version_guard_runs_before_tool_install(self):
        guard = next(s for s in self.steps if s.get("name") == "Verify supported Python version")
        self.assertLess(self.steps.index(guard), self.steps.index(self.install))
        # Run the guard in real Python with an isolated version-info fixture.
        command = guard["run"].strip()
        arguments = shlex.split(command)
        self.assertEqual(arguments[:2], ["python", "-c"])
        for version, succeeds in (((3, 9), False), ((3, 10), True), ((3, 14), True)):
            with self.subTest(version=version):
                result = subprocess.run(
                    [sys.executable, "-c", f"import sys; sys.version_info={version!r}; " + arguments[2]],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                if not succeeds:
                    self.assertIn("CVE-2025-71176", result.stderr)


if __name__ == "__main__":
    unittest.main()
