"""One actionable report instead of one import error per test module."""

import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from scripts import check_test_environment


class TestEnvironmentReportTests(unittest.TestCase):
    def report(self):
        stream = io.StringIO()
        with redirect_stderr(stream):
            status = check_test_environment.main()
        return status, stream.getvalue()

    def test_complete_environment_reports_nothing(self):
        with patch.object(check_test_environment, "missing_modules", return_value=[]):
            self.assertEqual(self.report(), (0, ""))

    def test_missing_requirements_name_the_locked_install_command_once(self):
        with patch.object(check_test_environment, "missing_modules", return_value=["PyNaCl"]):
            status, output = self.report()
        self.assertEqual(status, 2)
        self.assertEqual(output.count("pip install"), 1)
        self.assertIn("PyNaCl", output)
        self.assertIn(".github/requirements-generator.txt", output)
        self.assertIn("tools/secret_control/requirements-github-lock.txt", output)

    def test_detection_reports_only_absent_distributions(self):
        present = type("Spec", (), {})()
        with patch.object(
            check_test_environment.importlib.util,
            "find_spec",
            side_effect=lambda name: None if name == "nacl" else present,
        ):
            self.assertEqual(check_test_environment.missing_modules(), ["PyNaCl"])
