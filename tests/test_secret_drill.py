"""Synthetic-only recovery drill tests; actual age is explicitly opt-in."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.secret_control import drill
from tools.secret_control.catalog import ControlError


class RecoveryDrillSafetyTests(unittest.TestCase):
    def test_tool_failure_is_redacted_before_temporary_directory(self):
        with patch.object(drill, "trusted_tool", side_effect=ValueError("PRIVATE_KEY")):
            with patch.object(drill.tempfile, "TemporaryDirectory") as temporary:
                with self.assertRaises(ControlError) as caught:
                    drill.run_drill("age", "age-keygen")
        self.assertEqual(
            str(caught.exception), "synthetic recovery drill failed; details withheld"
        )
        temporary.assert_not_called()

    def test_failed_exercise_cleans_temporary_files_and_redacts(self):
        directories = []

        def fail(directory, age, keygen):
            directories.append(directory)
            (directory / "synthetic-key").write_bytes(b"PRIVATE_KEY_MARKER")
            raise OSError("PRIVATE_KEY_MARKER")

        with patch.object(drill, "trusted_tool", side_effect=lambda value: value):
            with patch.object(drill, "_exercise", side_effect=fail):
                with self.assertRaises(ControlError) as caught:
                    drill.run_drill("age", "keygen")
        self.assertNotIn("PRIVATE_KEY_MARKER", str(caught.exception))
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())

    def test_unremoved_directory_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            with patch.object(drill, "trusted_tool", side_effect=lambda value: value):
                with patch.object(drill.tempfile, "TemporaryDirectory") as context:
                    context.return_value.__enter__.return_value = str(directory)
                    with patch.object(drill, "_exercise", return_value={}):
                        with self.assertRaises(ControlError):
                            drill.run_drill("age", "keygen")
            self.assertTrue(directory.exists())

    def test_unrelated_failure_does_not_count_as_expected_refusal(self):
        def timeout():
            raise ControlError("external tool timeout")

        with self.assertRaisesRegex(ControlError, "unexpected refusal"):
            drill._refused(
                timeout, ControlError, "external tool failed; output withheld"
            )
        with self.assertRaisesRegex(ControlError, "expected refusal was not observed"):
            drill._refused(
                lambda: None, ControlError, "external tool failed; output withheld"
            )
        with self.assertRaises(FileNotFoundError):
            drill._refused(
                lambda: (_ for _ in ()).throw(FileNotFoundError()), ControlError, "x"
            )


@unittest.skipUnless(
    os.environ.get("SECRET_CONTROL_AGE")
    and os.environ.get("SECRET_CONTROL_AGE_KEYGEN"),
    "set both SECRET_CONTROL_AGE and SECRET_CONTROL_AGE_KEYGEN",
)
class RecoveryDrillAgeTests(unittest.TestCase):
    def test_complete_offline_recovery_drill(self):
        directories = []
        exercise = drill._exercise

        def observe(directory, age, keygen):
            directories.append(directory)
            return exercise(directory, age, keygen)

        with patch.object(drill, "_exercise", side_effect=observe):
            result = drill.run_drill(
                os.environ["SECRET_CONTROL_AGE"],
                os.environ["SECRET_CONTROL_AGE_KEYGEN"],
            )
        self.assertEqual(result["status"], "passed")
        self.assertTrue(result["syntheticOnly"])
        self.assertEqual(result["scope"], "offline-local-recovery-mechanics")
        self.assertEqual(result["generations"], 2)
        self.assertEqual(len(result["checks"]), 11)
        self.assertTrue(all(value is True for value in result["checks"].values()))
        self.assertEqual(len(directories), 1)
        self.assertFalse(directories[0].exists())
        report = json.dumps(result)
        for private_marker in [
            "AGE-SECRET-KEY-",
            "fictional",
            "ws-",
            str(directories[0]),
        ]:
            self.assertNotIn(private_marker, report)


if __name__ == "__main__":
    unittest.main()
