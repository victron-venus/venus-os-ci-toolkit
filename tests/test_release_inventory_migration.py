"""Finite inventory upgrades preserve existing engines, policy and checkout data."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
installer = importlib.import_module("install_release")
migration = importlib.import_module("release_inventory_migration")
receipts = migration.receipts
PREFIX = "#!/usr/bin/env python3\n" + receipts.HEADER


class InventoryMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.consumer = self.root / "consumer"
        (self.consumer / "scripts").mkdir(parents=True)
        self.policy = {
            "mode": "release",
            "versioning": {"schema": 1, "required_build_receipts": ["release-inputs-package.json"]},
        }
        self.write_policy()
        self.originals, self.expected, specs = {}, {}, {}
        for name in migration.FILES:
            flag = "DETACHED_PROVENANCE_REQUIRED = False\n" if name == receipts.RECEIPT else ""
            before = flag + "def guard():\n    return 'old'\n"
            after = before.replace("'old'", "'inventory'")
            self.originals[name], self.expected[name] = PREFIX + before, PREFIX + after
            (self.consumer / name).write_text(self.originals[name])
            specs[name] = {
                "hunks": [{"before": "return 'old'", "after": "return 'inventory'"}],
                "original_to_current": {receipts.digest("#!/usr/bin/env python3\n" + before): receipts.digest("#!/usr/bin/env python3\n" + after)},
                "current": [receipts.digest("#!/usr/bin/env python3\n" + after)],
            }
        (self.consumer / ".gitignore").write_text("keep-private-data\n")
        self.delta = self.root / "delta.json"
        self.delta.write_text(json.dumps({"files": specs}))
        self.enterContext(patch.object(migration, "DELTA", self.delta))

    def write_policy(self):
        (self.consumer / ".release-policy.json").write_text(json.dumps(self.policy))

    def snapshot(self):
        return {str(p.relative_to(self.consumer)): p.read_bytes() for p in self.consumer.rglob("*") if p.is_file()}

    def cli(self, *extra):
        with patch.object(sys, "argv", ["install_release.py", str(self.consumer), "--release-inventory-only", *extra]), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return installer.main()

    def test_render_is_read_only_and_changes_only_three_known_runtime_files(self):
        before = self.snapshot()
        self.assertEqual(migration.render(self.consumer), self.expected)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.cli(), 0)
        self.assertEqual(migration.render(self.consumer), {})
        for name in (".gitignore", ".release-policy.json"):
            self.assertEqual(self.snapshot()[name], before[name])

    def test_unknown_last_file_prevents_every_cli_write(self):
        path = self.consumer / receipts.PUBLISHER
        path.write_text(path.read_text() + "# custom runtime\n")
        before = self.snapshot()
        self.assertEqual(self.cli(), 1)
        self.assertEqual(self.snapshot(), before)

    def test_partial_inventory_upgrade_is_rejected(self):
        (self.consumer / migration.PLAN).write_text(self.expected[migration.PLAN])
        with self.assertRaisesRegex(ValueError, "Partially migrated"):
            migration.render(self.consumer)

    def test_required_inventory_must_be_explicit_and_valid(self):
        for value in (None, [], "release-inputs-package.json", ["../package.json"], ["release-inputs-fuzz.json"], ["release-inputs-package.json"] * 2):
            with self.subTest(value=value):
                self.policy["versioning"]["required_build_receipts"] = value
                self.write_policy()
                before = self.snapshot()
                self.assertEqual(self.cli(), 1)
                self.assertEqual(self.snapshot(), before)
        del self.policy["versioning"]["required_build_receipts"]
        self.write_policy()
        with self.assertRaisesRegex(ValueError, "Declare reviewed"):
            migration.render(self.consumer)

    def test_private_local_or_non_versioned_policy_is_rejected(self):
        for key, value in (("visibility", "private"), ("ci_execution", "local"), ("mode", "validate"), ("versioning", {"schema": True})):
            with self.subTest(key=key), patch.dict(self.policy, {key: value}):
                self.write_policy()
                with self.assertRaisesRegex(ValueError, "public hosted versioned"):
                    migration.render(self.consumer)

    def test_missing_or_aliased_file_is_rejected(self):
        path = self.consumer / receipts.PUBLISHER
        outside = self.root / "outside.py"
        path.rename(outside)
        with self.assertRaises(FileNotFoundError):
            migration.render(self.consumer)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "local regular"):
            migration.render(self.consumer)

    def test_detached_provenance_flag_is_preserved(self):
        path = self.consumer / receipts.RECEIPT
        path.write_text(path.read_text().replace("REQUIRED = False", "REQUIRED = True"))
        self.assertIn("REQUIRED = True", migration.render(self.consumer)[receipts.RECEIPT])

    def test_check_mode_and_exclusive_renderer_options(self):
        before = self.snapshot()
        self.assertEqual(self.cli("--check"), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.cli(), 0)
        self.assertEqual(self.cli("--check"), 0)
        with self.assertRaises(SystemExit) as error:
            self.cli("--release-receipts-only")
        self.assertEqual(error.exception.code, 2)


class InventoryOracleTests(unittest.TestCase):
    def test_shipped_delta_preserves_exact_accepted_pre_inventory_sources(self):
        # These hashes identify literal source files at accepted commit 9c1031e.
        previous = {
            migration.PLAN: "e29350ddaa08667bd9ecca2d5a18c6b1e188826fd69c4e77027372aad0cd2d1e",
            receipts.RECEIPT: "4f273af2376ffe0de1ec1b064365badbbebf5b2718233817d12ead988d941e81",
            receipts.PUBLISHER: "e27e76d2a6080bb257ce60420cf9ead4a4a86f1b4a86f568225aa6ee9706875e",
        }
        specifications = json.loads(migration.DELTA.read_text())["files"]
        for name, expected in previous.items():
            with self.subTest(name=name):
                current = (ROOT / name).read_text()
                old = migration.previous_canonical_source(current, name)
                self.assertEqual(hashlib.sha256(old.encode()).hexdigest(), expected)
                state, result = receipts.transform(old.replace("#!/usr/bin/env python3\n", PREFIX, 1), name, specifications[name])
                self.assertEqual(state, "original")
                self.assertEqual(result, current.replace("#!/usr/bin/env python3\n", PREFIX, 1))
                with self.assertRaisesRegex(ValueError, "Unknown current"):
                    migration.previous_canonical_source(current + "# unknown\n", name)


if __name__ == "__main__":
    unittest.main()
