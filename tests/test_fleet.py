"""Exercise fleet identity checks without invoking Git or changing remote state."""

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "fleet", Path(__file__).resolve().parents[1] / "scripts/fleet.py"
)
fleet = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fleet)


class FleetIdentityTests(unittest.TestCase):
    """Keep fetch, push and policy identities bound to the selected repository."""

    def test_source_and_push_origin_must_both_match(self):
        """Reject mismatched origins and lookalike GitHub hosts before submission."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text(
                json.dumps({"repository": "owner/repo"})
            )
            for remotes, accepted in [
                (
                    [
                        "https://github.com/owner/repo.git",
                        "git@github.com:owner/repo.git",
                    ],
                    True,
                ),
                (
                    [
                        "https://github.com/other/repo.git",
                        "git@github.com:owner/repo.git",
                    ],
                    False,
                ),
                (
                    [
                        "https://github.com/owner/repo.git",
                        "git@github.com:other/repo.git",
                    ],
                    False,
                ),
                (
                    [
                        "https://github.com.example.com/owner/repo.git",
                        "git@github.com:owner/repo.git",
                    ],
                    False,
                ),
            ]:
                with (
                    self.subTest(remotes=remotes),
                    mock.patch.object(
                        fleet,
                        "run",
                        side_effect=[
                            subprocess.CompletedProcess([], 0, url) for url in remotes
                        ],
                    ),
                ):
                    if accepted:
                        fleet.validate_identity(root, {"repository": "owner/repo"})
                    else:
                        with self.assertRaises(ValueError):
                            fleet.validate_identity(root, {"repository": "owner/repo"})

    def test_wrong_policy_is_rejected_before_git_access(self):
        """Fail a conflicting local policy without consulting repository remotes."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text(
                json.dumps({"repository": "other/repo"})
            )
            with mock.patch.object(fleet, "run") as run, self.assertRaises(ValueError):
                fleet.validate_identity(root, {"repository": "owner/repo"})
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
