"""Metadata automation can use an existing trusted runner without checking out code."""

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def metadata_entrypoint(filename, script):
    """Use the checked-in command, with an explicit interpreter for portable tests."""
    workflow = yaml.load(
        (ROOT / ".github/workflows" / filename).read_text(), Loader=yaml.BaseLoader
    )
    step = next(iter(workflow["jobs"].values()))["steps"][0]
    if "shell" in step:
        script.write_text(step["run"])
        command = shlex.split(step["shell"].format(shlex.quote(str(script))))
        stdin = None
        expected = "Configure BOT_PAT or APPROVAL_PAT"
    else:
        first, source = step["run"].split("\n", 1)
        command = shlex.split(first.split(" <<", 1)[0])
        stdin = source.rsplit("\nPY", 1)[0]
        expected = "BOT_PAT is missing"
    command[0] = sys.executable
    return command, stdin, expected


class MetadataRunnerTests(unittest.TestCase):
    """Keep hosted defaults and metadata-only execution intact for both consumers."""

    def test_runner_selection_is_typed_and_defaults_to_hosted(self):
        """Dedicated labels are explicit inputs rather than PR-controlled metadata."""
        for filename in ("auto-merge.yml", "auto-approve-reusable.yml"):
            with self.subTest(filename=filename):
                workflow = yaml.load(
                    (ROOT / ".github/workflows" / filename).read_text(),
                    Loader=yaml.BaseLoader,
                )
                runner = workflow["on"]["workflow_call"]["inputs"]["runner-labels"]
                self.assertEqual(runner["type"], "string")
                self.assertEqual(runner["default"], '["ubuntu-latest"]')
                for job in workflow["jobs"].values():
                    self.assertEqual(
                        job["runs-on"], "${{ fromJSON(inputs.runner-labels) }}"
                    )
                    for step in job["steps"]:
                        self.assertNotIn("uses", step)
                        self.assertNotIn("checkout", step.get("run", ""))

    def test_approval_requires_python3_on_selected_runner(self):
        """Private Linux runners need not have a legacy python alias."""
        workflow = yaml.load(
            (ROOT / ".github/workflows/auto-approve-reusable.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            workflow["jobs"]["auto-approve"]["steps"][0]["shell"], "python3 -I {0}"
        )

    def test_python_entrypoints_ignore_workspace_and_pythonpath_modules(self):
        """Actual workflow commands must not import leftovers beside private tokens."""
        for filename in ("auto-merge.yml", "auto-approve-reusable.yml"):
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                workspace = root / "workspace"
                injected = root / "pythonpath"
                workspace.mkdir()
                injected.mkdir()
                command, stdin, expected = metadata_entrypoint(
                    filename, root / "metadata.py"
                )
                for location in (workspace, injected):
                    with self.subTest(filename=filename, location=location.name):
                        shadow = location / "json.py"
                        shadow.write_text('raise RuntimeError("UNTRUSTED_IMPORT")\n')
                        environment = {"PATH": os.defpath, "PYTHONPATH": str(injected)}
                        result = subprocess.run(
                            command,
                            input=stdin,
                            cwd=workspace,
                            env=environment,
                            text=True,
                            capture_output=True,
                            check=False,
                            timeout=10,
                        )
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(expected, result.stderr)
                        self.assertNotIn("UNTRUSTED_IMPORT", result.stderr)
                        # Prove the PYTHONPATH fixture reaches the real import path
                        # if isolation is removed; no real credentials are supplied.
                        if location == injected:
                            unsafe = subprocess.run(
                                [part for part in command if part != "-I"],
                                input=stdin,
                                cwd=workspace,
                                env=environment,
                                text=True,
                                capture_output=True,
                                check=False,
                                timeout=10,
                            )
                            self.assertIn("UNTRUSTED_IMPORT", unsafe.stderr)
                        shadow.unlink()


if __name__ == "__main__":
    unittest.main()
