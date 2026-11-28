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
        with patch.object(check_test_environment, "missing_modules", return_value=[]), \
                patch.object(check_test_environment, "python3_version", return_value=(3, 13, 1)), \
                patch.object(check_test_environment, "release_contracts_skip_reason", return_value=None):
            self.assertEqual(self.report(), (0, ""))

    def test_missing_requirements_name_the_locked_install_command_once(self):
        with patch.object(check_test_environment, "missing_modules", return_value=["PyNaCl"]):
            status, output = self.report()
        self.assertEqual(status, 2)
        self.assertEqual(output.count("pip install"), 1)
        self.assertIn("PyNaCl", output)
        self.assertIn(".github/requirements-generator.txt", output)
        self.assertIn("tools/secret_control/requirements-github-lock.txt", output)

    def test_old_python3_warns_without_failing_the_run(self):
        stream = io.StringIO()
        with patch.object(check_test_environment, "missing_modules", return_value=[]), \
                patch.object(check_test_environment, "python3_version", return_value=(3, 9, 6)), \
                patch.object(check_test_environment, "release_contracts_skip_reason", return_value=None), \
                redirect_stderr(stream):
            self.assertEqual(check_test_environment.main(), 0)
        output = stream.getvalue()
        self.assertIn("warning:", output)
        self.assertIn("3.9.6", output)
        self.assertIn("3.12", output)
        self.assertIn("CONTRIBUTING.md", output)

    def test_skipped_release_contracts_warn_about_the_missing_runtime(self):
        stream = io.StringIO()
        with patch.object(check_test_environment, "missing_modules", return_value=[]), \
                patch.object(check_test_environment, "python3_version", return_value=(3, 13, 1)), \
                patch.object(check_test_environment, "release_contracts_skip_reason",
                             return_value=check_test_environment.RELEASE_CONTRACTS_RUNTIME), \
                redirect_stderr(stream):
            self.assertEqual(check_test_environment.main(), 0)
        output = stream.getvalue()
        self.assertIn("warning:", output)
        self.assertIn("release-process contracts will skip", output)
        self.assertIn("waitid(WNOWAIT)", output)
        self.assertIn("not the CI result", output)

    def test_missing_requirements_stop_the_run_before_warnings(self):
        stream = io.StringIO()
        with patch.object(check_test_environment, "missing_modules", return_value=["PyNaCl"]), \
                patch.object(check_test_environment, "environment_warnings") as warnings, \
                redirect_stderr(stream):
            self.assertEqual(check_test_environment.main(), 2)
        warnings.assert_not_called()

    def test_detection_reports_only_absent_distributions(self):
        present = type("Spec", (), {})()
        with patch.object(
            check_test_environment.importlib.util,
            "find_spec",
            side_effect=lambda name: None if name == "nacl" else present,
        ):
            self.assertEqual(check_test_environment.missing_modules(), ["PyNaCl"])
