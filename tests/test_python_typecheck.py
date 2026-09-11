"""Exercise the reusable workflow's type-check steps in isolated environments."""

import os
import shutil
import subprocess
import tempfile
import unittest
import venv
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class PythonTypeCheckContract(unittest.TestCase):
    """Run the actual workflow commands with and without the optional check."""

    def setUp(self):
        # enterContext closes this context after each test, including failures.
        # pylint: disable-next=consider-using-with
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        environment = self.directory / "venv"
        venv.create(environment, with_pip=True)
        self.environment = dict(
            os.environ, PATH=f"{environment / 'bin'}:{os.environ['PATH']}"
        )
        self.python = environment / "bin" / "python"
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/python-ci.yml").read_text()
        )
        self.steps = [
            step
            for step in workflow["jobs"]["ci"]["steps"]
            if "MyPy" in step.get("name", "")
        ]
        self.assertEqual(len(self.steps), 2)
        self.project = self.directory / "project"
        self.project.mkdir()
        self.assertNotEqual(self.run_python("-m", "mypy", "--version").returncode, 0)

    def run_python(self, *arguments):
        """Invoke the isolated interpreter without depending on runner packages."""
        return subprocess.run(
            [str(self.python), *arguments], capture_output=True, text=True, check=False
        )

    def run_steps(self, enabled):
        """Apply the exact boolean input condition and execute the workflow shell."""
        results = []
        for step in self.steps:
            self.assertEqual(step["if"], "inputs.run-type-check")
            self.assertEqual(step["shell"], "bash")
            if enabled:
                result = subprocess.run(
                    ["bash", "-e", "-c", step["run"]],
                    cwd=self.project,
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                results.append(result)
                if result.returncode:
                    break
        return results

    def test_enabled_installs_mypy_and_rejects_annotation_error(self):
        """A fresh consumer must reject a real annotation error, then accept a fix."""
        source = ROOT / "tests/fixtures/python-type-error/example.py"
        target = self.project / "example.py"
        shutil.copyfile(source, target)
        results = self.run_steps(True)
        self.assertEqual(len(results), 2, results[0].stdout + results[0].stderr)
        self.assertEqual(results[0].returncode, 0)
        self.assertNotEqual(results[1].returncode, 0)
        self.assertIn("Incompatible return value type", results[1].stdout)
        target.write_text(source.read_text().replace("-> str:", "-> int:"))
        results = self.run_steps(True)
        self.assertTrue(all(result.returncode == 0 for result in results), results)

    def test_disabled_skips_install_and_invalid_source(self):
        """Opting out must neither install MyPy nor inspect the invalid fixture."""
        shutil.copyfile(
            ROOT / "tests/fixtures/python-type-error/example.py",
            self.project / "example.py",
        )
        self.assertEqual(self.run_steps(False), [])
        self.assertNotEqual(self.run_python("-m", "mypy", "--version").returncode, 0)
