"""Keep vendored subprocess reviews compatible with strict consumer Bandit gates."""

import json
from pathlib import Path
# The contract invokes only the local interpreter's Bandit module.
import subprocess  # nosec B404
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "scripts/release_fuzz.py",
    "scripts/assemble_native_container.py",
    "scripts/publish_verified.py",
    "scripts/verified_images.py",
    "tests/test_assemble_native_container.py",
    "tests/test_change_scope.py",
    "tests/test_merge_oci_archives.py",
    "tests/test_verified_publication.py",
)


class ReleaseBanditTests(unittest.TestCase):
    def scan(self, paths, *options):
        # Fixed local interpreter and scanner; arguments are test-owned source paths.
        result = subprocess.run(  # nosec B603
            [sys.executable, "-m", "bandit", "-q", "-f", "json", *options, *paths],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        self.assertIn(result.returncode, (0, 1), result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["errors"], [])
        return report["results"]

    def test_vendored_subprocess_sites_have_no_unreviewed_findings(self):
        self.assertEqual(self.scan(FILES), [])

    def test_subprocess_rules_remain_active(self):
        findings = self.scan(("scripts/release_fuzz.py",), "--ignore-nosec")
        self.assertTrue({"B404", "B603", "B607"} <= {item["test_id"] for item in findings})
