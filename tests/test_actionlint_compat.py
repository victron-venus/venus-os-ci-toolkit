"""Queue compatibility cannot hide unrelated linter findings or edit sources."""

from contextlib import redirect_stderr
import io
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import actionlint_compat as compat  # noqa: E402

WORKFLOW = """name: Queue test
on: push
concurrency:
  group: releases
  queue: max
  cancel-in-progress: false
jobs:
  check:
    runs-on: ubuntu-24.04
    steps:
      - run: echo '${{ github.ref }}'
"""


class QueueCompatibilityTests(unittest.TestCase):
    def test_only_queue_is_removed_without_changing_offsets(self):
        for source in (WORKFLOW, WORKFLOW.replace("    runs-on:", "    concurrency: {group: check, queue: single}\n    runs-on:")):
            result = compat.lint_copy(source)
            self.assertEqual(len(result), len(source))
            self.assertEqual([i for i, c in enumerate(result) if c == "\n"],
                             [i for i, c in enumerate(source) if c == "\n"])
            expected = yaml.load(source, Loader=yaml.BaseLoader)
            del expected["concurrency"]["queue"]
            if "concurrency" in expected["jobs"]["check"]:
                del expected["jobs"]["check"]["concurrency"]["queue"]
            self.assertEqual(yaml.load(result, Loader=yaml.BaseLoader), expected)
            self.assertEqual(compat.lint_copy(result), result)

    def test_flow_mapping_first_middle_last_and_only_queue(self):
        for value in ("{queue: max, group: release}", "{group: release, queue: max, cancel-in-progress: false}",
                      "{group: release, queue: max}", "{queue: single}"):
            source = "concurrency: " + value + "\n"
            expected = yaml.load(source, Loader=yaml.BaseLoader)
            del expected["concurrency"]["queue"]
            self.assertEqual(yaml.load(compat.lint_copy(source), Loader=yaml.BaseLoader), expected)

    def test_invalid_or_unverifiable_queue_fails_before_lint(self):
        for source in (WORKFLOW.replace("queue: max", "queue: unknown"),
                       WORKFLOW.replace("queue: max", "queue: [max]"),
                       WORKFLOW.replace("queue: max", "queue: '${{ vars.QUEUE }}'"),
                       WORKFLOW.replace("cancel-in-progress: false", "cancel-in-progress: true"),
                       WORKFLOW.replace("cancel-in-progress: false", "cancel-in-progress: '${{ vars.CANCEL }}'"),
                       WORKFLOW.replace("queue: max", "queue: max\n  queue: single"),
                       "name: &queue max\nconcurrency: {group: release, queue: *queue}\n"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                compat.lint_copy(source)

    def test_unknown_fields_and_malformed_yaml_are_not_sanitized(self):
        self.assertEqual(compat.lint_copy("concurrency: [unterminated"), "concurrency: [unterminated")
        source = WORKFLOW.replace("  queue: max", "  queue: max\n  unrelated: true")
        self.assertIn("  unrelated: true", compat.lint_copy(source))
        self.assertIn("queue: other", compat.lint_copy("jobs: {a: {queue: other}}"))

    def test_original_paths_and_all_files_reach_linter_without_mutation(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        paths = [root / "first.yml", root / "second.yml"]
        for path in paths:
            path.write_text(WORKFLOW)
        with patch.object(compat.subprocess, "run", side_effect=[subprocess.CompletedProcess([], 1), subprocess.CompletedProcess([], 0)]) as run:
            self.assertEqual(compat.main([str(path) for path in paths]), 1)
        for path, call in zip(paths, run.call_args_list, strict=True):
            self.assertEqual(path.read_text(), WORKFLOW)
            self.assertEqual(call.args[0][-3:], ["-stdin-filename", str(path), "-"])
            self.assertNotIn("-ignore", call.args[0])
            self.assertEqual(call.kwargs["input"], compat.lint_copy(WORKFLOW))
        paths[0].write_text(WORKFLOW.replace("queue: max", "queue: invalid"))
        with patch.object(compat.subprocess, "run") as run, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(compat.main([str(paths[0])]), 1)
            run.assert_not_called()
        self.assertIn(str(paths[0]), errors.getvalue())

    @unittest.skipUnless(shutil.which("actionlint"), "actionlint is not installed")
    def test_real_linter_preserves_unrelated_error_and_original_position(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = root / "workflow.yml"
        source = WORKFLOW.replace("github.ref", "github.not_a_real_property")
        path.write_text(source)
        result = subprocess.run([sys.executable, str(Path(compat.__file__)), str(path)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"{path}:11:", result.stdout)
        self.assertIn('property "not_a_real_property"', result.stdout)
        self.assertNotIn('unexpected key "queue"', result.stdout)
        self.assertEqual(path.read_text(), source)
