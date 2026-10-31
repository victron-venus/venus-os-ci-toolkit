"""Portable migration invariants; independent immutable consumer goldens are separate."""
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
migration = importlib.import_module("release_receipt_migration")
inventory = importlib.import_module("release_inventory_migration")

PREFIX = "#!/usr/bin/env python3\n" + migration.HEADER
SOURCES = {
    migration.RECEIPT: (
        "DETACHED_PROVENANCE_REQUIRED = False\n"
        "# retain this local fixture behavior\n"
        "def receipt():\n    return {'assets': ['app']}\n",
        "def receipt():\n    return {'assets': ['app'], 'producer': 'current'}\n",
    ),
    migration.PUBLISHER: (
        "# keep unrelated download API\n"
        "def publish(stage, plan, assets, policy, producer):\n"
        "    return verify_receipts(stage, plan, assets, policy)\n",
        "    return verify_receipts(stage, plan, assets, policy, producer)\n",
    ),
    migration.SIGNER: (
        "def prepare(stage, plan, assets, policy, producer):\n"
        "    return verify_receipts(stage, plan, assets, policy)\n",
        "    return verify_receipts(stage, plan, assets, policy, producer)\n",
    ),
}


def sha(source):
    return hashlib.sha256(source.encode()).hexdigest()


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.consumer = self.root / "consumer"
        (self.consumer / "scripts").mkdir(parents=True)
        self.policy = {"mode": "release", "versioning": {"schema": 1}, "sign_releases": True}
        self.write_policy()
        self.originals = {}
        self.expected = {}
        specifications = {}
        for name, (body, replacement) in SOURCES.items():
            old = "def receipt():\n    return {'assets': ['app']}\n" if name == migration.RECEIPT else "    return verify_receipts(stage, plan, assets, policy)\n"
            new_body = body.replace(old, replacement)
            self.originals[name] = PREFIX + body
            self.expected[name] = PREFIX + new_body
            (self.consumer / name).write_text(self.originals[name])
            specifications[name] = {
                "hunks": [{"before": old, "after": replacement}],
                "original_to_current": {sha("#!/usr/bin/env python3\n" + body): sha("#!/usr/bin/env python3\n" + new_body)},
                "current": [sha("#!/usr/bin/env python3\n" + new_body)],
            }
        (self.consumer / ".gitignore").write_text("preserve private files\n")
        delta = self.root / "delta.json"
        delta.write_text(json.dumps({"files": specifications}))
        self.enterContext(patch.object(migration, "DELTA", delta))

    def write_policy(self):
        (self.consumer / ".release-policy.json").write_text(json.dumps(self.policy))

    def snapshot(self):
        return {str(path.relative_to(self.consumer)): path.read_bytes() for path in self.consumer.rglob("*") if path.is_file()}

    def cli(self, *arguments):
        with patch.object(sys, "argv", ["install_release.py", str(self.consumer), "--release-receipts-only", *arguments]), redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
            return installer.main()

    def test_complete_render_preserves_unrelated_bytes_and_links_producer(self):
        before = self.snapshot()
        output = migration.render(self.consumer)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(set(output), set(SOURCES))
        self.assertIn("'producer': 'current'", output[migration.RECEIPT])
        for name in (migration.PUBLISHER, migration.SIGNER):
            self.assertIn("verify_receipts(stage, plan, assets, policy, producer)", output[name])
        self.assertIn("# keep unrelated download API", output[migration.PUBLISHER])
        for name, content in output.items():
            self.assertEqual(content, self.expected[name])
            (self.consumer / name).write_text(content)
        self.assertEqual(migration.render(self.consumer), {})
        self.assertEqual((self.consumer / ".gitignore").read_bytes(), before[".gitignore"])

    def test_true_flag_and_header_survive(self):
        path = self.consumer / migration.RECEIPT
        path.write_text(path.read_text().replace("REQUIRED = False", "REQUIRED = True"))
        output = migration.render(self.consumer)
        self.assertTrue(output[migration.RECEIPT].startswith(PREFIX))
        self.assertIn("DETACHED_PROVENANCE_REQUIRED = True", output[migration.RECEIPT])

    def test_unknown_late_signer_prevents_all_cli_writes(self):
        path = self.consumer / migration.SIGNER
        path.write_text(path.read_text() + "# unknown\n")
        before = self.snapshot()
        self.assertEqual(self.cli(), 1)
        self.assertEqual(self.snapshot(), before)

    def test_mixed_set_and_unknown_partial_body_rejected(self):
        path = self.consumer / migration.SIGNER
        path.write_text(self.expected[migration.SIGNER])
        with self.assertRaisesRegex(ValueError, "Partially migrated"):
            migration.render(self.consumer)
        path.write_text(self.originals[migration.SIGNER].replace("policy)", "policy, unexpected)"))
        with self.assertRaisesRegex(ValueError, "Unknown or partially"):
            migration.render(self.consumer)

    def test_missing_required_signer_and_vendor_header_rejected(self):
        path = self.consumer / migration.SIGNER
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            migration.render(self.consumer)
        path.write_text(self.originals[migration.SIGNER].replace(migration.HEADER, ""))
        with self.assertRaisesRegex(ValueError, "vendor header"):
            migration.render(self.consumer)

    def test_unsigned_existing_pair_is_supported(self):
        self.policy["sign_releases"] = False
        self.write_policy()
        (self.consumer / migration.SIGNER).unlink()
        self.assertEqual(set(migration.render(self.consumer)), {migration.RECEIPT, migration.PUBLISHER})

    def test_existing_policy_scope_and_file_aliases(self):
        for key, value in (("versioning", None), ("visibility", "private"), ("ci_execution", "local"), ("sign_releases", "true")):
            with self.subTest(key=key), patch.dict(self.policy, {key: value}):
                self.write_policy()
                with self.assertRaisesRegex(ValueError, "public hosted versioned"):
                    migration.render(self.consumer)
        self.write_policy()
        path = self.consumer / migration.SIGNER
        outside = self.root / "outside.py"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "local regular"):
            migration.render(self.consumer)

    def test_check_mode_apply_idempotence_and_exclusive_modes(self):
        before = self.snapshot()
        self.assertEqual(self.cli("--check"), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.cli(), 0)
        after = self.snapshot()
        self.assertEqual(self.cli("--check"), 0)
        self.assertEqual(self.snapshot(), after)
        with self.assertRaises(SystemExit) as error:
            self.cli("--runners-only")
        self.assertEqual(error.exception.code, 2)


class ShippedDeltaTests(unittest.TestCase):
    def test_real_shipped_delta_roundtrips_canonical_sources(self):
        # Independent immutable source oracle: PR320 parent 368320994ed55ae68935dcadb4062000a4648b92.
        historical_sha256 = {
            migration.RECEIPT: "cbc93a0de3a460c84c20b0c014e27e53d7671159c819addabb1e242fb0941f4a",
            migration.PUBLISHER: "06de00cf0a9b43adffd79e825a68aee8b86d4b7d343c14ca10383d08d235e458",
            migration.SIGNER: "4f84a5238e9c51822752a71f42968bc5061c4a20e3956f5c1670d359d2e09e52",
        }
        specifications = json.loads(migration.DELTA.read_text())["files"]
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary)
            (consumer / "scripts").mkdir()
            (consumer / ".release-policy.json").write_text(json.dumps({
                "mode": "release", "versioning": {"schema": 1}, "sign_releases": True,
            }))
            expected = {}
            for name in (migration.RECEIPT, migration.PUBLISHER, migration.SIGNER):
                module = importlib.util.find_spec(Path(name).stem)
                self.assertIsNotNone(module)
                current = Path(module.origin).read_text()
                current = current.replace(migration.HEADER, "", 1)
                current = inventory.previous_canonical_source(current, name)
                specification = specifications[name]
                self.assertIn(sha(current), specification["current"])
                old = current
                for hunk in reversed(specification["hunks"]):
                    self.assertEqual(old.count(hunk["after"]), 1)
                    old = old.replace(hunk["after"], hunk["before"], 1)
                self.assertEqual(sha(old), historical_sha256[name])
                self.assertEqual(specification["original_to_current"][sha(old)], sha(current))
                (consumer / name).write_text(old.replace("#!/usr/bin/env python3\n", PREFIX, 1))
                expected[name] = current.replace("#!/usr/bin/env python3\n", PREFIX, 1)
            output = migration.render(consumer)
            self.assertEqual(output, expected)
            self.assertIn('result["producer"] = producer', output[migration.RECEIPT])
            self.assertIn("verify_producer_identity(receipt, expected_producer)", output[migration.RECEIPT])
            self.assertIn("verify_receipts(stage, plan, assets, policy, producer_identity(", output[migration.PUBLISHER])
            self.assertIn('cast(int, run["run_attempt"])', output[migration.PUBLISHER])
            self.assertIn("current_producer(source_sha))", output[migration.SIGNER])
            for name, content in output.items():
                (consumer / name).write_text(content)
            self.assertEqual(migration.render(consumer), {})


if __name__ == "__main__":
    unittest.main()
