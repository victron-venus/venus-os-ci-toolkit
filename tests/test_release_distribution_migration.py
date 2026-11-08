"""Guarded compact backport boundaries, rollback and generator integration."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
migration = importlib.import_module("release_distribution_migration")
installer = importlib.import_module("install_release")


class CompactMigrationTests(unittest.TestCase):
    """Exercise actual writes only in an owned temporary consumer fixture."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.consumer, self.canonical = self.root / "consumer", self.root / "canonical"
        for root in (self.consumer, self.canonical):
            for name in ("scripts", "tests", "docs", ".github/release-tests", ".github/workflows/nested"):
                (root / name).mkdir(parents=True, exist_ok=True)
        self.policy = {"repository": "owner/reviewed", "sign_releases": False,
                       "versioning": {"schema": 1, "promotion": "promote-bytes",
                                      "toolkit_revision": "b" * 40, "build_number_floor": 1000}}
        self.write_policy()
        self.files = {}
        self.expected = {}
        self.enterContext(patch.object(migration, "ROOT", self.canonical))
        for name in ("scripts/release_control.py", "scripts/release_versioned.py", ".github/release-tests/test_versioned_release.py"):
            old = migration.HEADER + "# preserve bounded readback and signing policy\nVALUE = 'old'\n"
            new = old.replace("VALUE = 'old'", "VALUE = 'compact'")
            (self.consumer / name).write_text(old)
            self.files[name] = {"before_sha256": migration.digest(old.encode()), "after_sha256": migration.digest(new.encode()), "hunks": [{"before": "VALUE = 'old'", "after": "VALUE = 'compact'"}]}
            self.expected[name] = new
        for source, target, content in (
            ("scripts/release_distribution.py", "scripts/release_distribution.py", "SCHEMA = 2\n"),
            ("tests/test_release_distribution.py", ".github/release-tests/test_release_distribution.py", "from pathlib import Path\nROOT = Path(__file__).parents[1]\n"),
            ("docs/release-distribution.md", "docs/release-distribution.md", "Compact opt-in only.\n"),
        ):
            (self.canonical / source).write_text(content)
            spec = {"before_sha256": None, "source": source, "source_sha256": migration.digest(content.encode())}
            expected = migration.canonical_copy(spec)
            spec["after_sha256"] = migration.digest(expected)
            self.files[target] = spec
            self.expected[target] = expected.decode()
        self.files["scripts/release_closed.py"] = {**self.files["scripts/release_control.py"], "optional": True}
        self.delta = self.root / "delta.json"
        self.write_delta()
        self.enterContext(patch.object(migration, "DELTA", self.delta))
        for name, content in {".github/workflows/ci.yml": "unchanged pinned CI\n", ".github/workflows/nested/security.yaml": "unchanged security pins\n", ".gitignore": "foreign edits\n", ".partial-toolkit-revision": "older reviewed revision\n"}.items():
            (self.consumer / name).write_text(content)

    def write_policy(self):
        (self.consumer / ".release-policy.json").write_text(json.dumps(self.policy))

    def write_delta(self):
        self.delta.write_text(json.dumps({"schema": 1, "repository": "owner/reviewed", "files": self.files}))

    def snapshot(self):
        return {str(p.relative_to(self.consumer)): (p.read_bytes(), p.stat().st_mode) for p in self.consumer.rglob("*") if p.is_file()}

    def cli(self, *extra):
        with patch.object(sys, "argv", ["install_release.py", str(self.consumer), "--release-distribution-only", *extra]), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return installer.main()

    def test_exact_bundle_and_atomic_apply_preserve_every_unowned_file(self):
        before = self.snapshot()
        changes = migration.render(self.consumer)
        self.assertEqual(changes, self.expected)
        self.assertEqual(self.snapshot(), before)
        migration.apply(self.consumer, changes)
        self.assertEqual(migration.render(self.consumer), {})
        for name, original in before.items():
            if name not in changes:
                self.assertEqual(self.snapshot()[name], original)
        self.assertFalse((self.consumer / "scripts/release_closed.py").exists())
        self.assertIn("parents[2]", (self.consumer / ".github/release-tests/test_release_distribution.py").read_text())

    def test_check_apply_idempotence_and_mode_exclusivity(self):
        before = self.snapshot()
        self.assertEqual(self.cli("--check"), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.cli(), 0)
        after = self.snapshot()
        self.assertEqual(self.cli("--check"), 0)
        self.assertEqual(self.cli(), 0)
        self.assertEqual(self.snapshot(), after)
        with self.assertRaises(SystemExit):
            self.cli("--release-inventory-only")

    def test_unknown_late_preimage_rejects_before_any_write(self):
        path = self.consumer / ".github/release-tests/test_versioned_release.py"
        path.write_text(path.read_text() + "# unreviewed\n")
        before = self.snapshot()
        self.assertEqual(self.cli(), 1)
        self.assertEqual(self.snapshot(), before)

    def test_mixed_bundle_is_rejected(self):
        name = "scripts/release_control.py"
        (self.consumer / name).write_text(self.expected[name])
        with self.assertRaisesRegex(ValueError, "Partially migrated"):
            migration.render(self.consumer)

    def test_changed_canonical_copy_and_postimage_are_rejected(self):
        path = self.canonical / "scripts/release_distribution.py"
        original = path.read_text()
        path.write_text(original + "# changed\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            migration.render(self.consumer)
        path.write_text(original)
        self.files["scripts/release_control.py"]["after_sha256"] = "0" * 64
        self.write_delta()
        with self.assertRaisesRegex(ValueError, "postimage changed"):
            migration.render(self.consumer)

    def test_optional_absent_module_is_not_installed_but_unknown_present_rejected(self):
        self.assertNotIn("scripts/release_closed.py", migration.render(self.consumer))
        (self.consumer / "scripts/release_closed.py").write_text("# unrelated local implementation\n")
        with self.assertRaisesRegex(ValueError, "Unknown compact"):
            migration.render(self.consumer)

    def test_unknown_policy_values_do_not_change_policy(self):
        for value in (None, True, False, "unknown", 1):
            self.policy["stable_distribution"] = value
            self.write_policy()
            before = self.snapshot()
            self.assertEqual(self.cli(), 1)
            self.assertEqual(self.snapshot(), before)
        self.policy["stable_distribution"] = "rc-payloads-v1"
        self.write_policy()
        original = (self.consumer / ".release-policy.json").read_bytes()
        self.assertEqual(self.cli(), 0)
        self.assertEqual((self.consumer / ".release-policy.json").read_bytes(), original)

    def test_aliases_and_workflow_destinations_are_rejected(self):
        path = self.consumer / "scripts/release_control.py"
        original = path.read_bytes()
        outside = self.root / "outside.py"
        path.rename(outside)
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "aliases"):
            migration.render(self.consumer)
        path.unlink()
        path.write_bytes(original)
        os.link(path, self.root / "hardlink.py")
        with self.assertRaisesRegex(ValueError, "regular files"):
            migration.render(self.consumer)
        self.assertFalse(migration.destination(".github/workflows/release.py"))
        self.assertFalse(migration.destination("scripts/../policy.py"))

    def test_stale_plan_leaves_no_writes(self):
        changes = migration.render(self.consumer)
        path = self.consumer / "scripts/release_control.py"
        path.write_text(path.read_text() + "# concurrent edit\n")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            migration.apply(self.consumer, changes)
        self.assertEqual(self.snapshot(), before)

    def test_staging_failure_leaves_no_writes_or_sibling_temporaries(self):
        changes = migration.render(self.consumer)
        before = self.snapshot()
        original = migration.stage
        calls = 0

        def interrupted(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic staging failure")
            return original(*args)

        with patch.object(migration, "stage", side_effect=interrupted), self.assertRaises(OSError):
            migration.apply(self.consumer, changes)
        self.assertEqual(self.snapshot(), before)

    def test_atomic_replace_failure_rolls_back_earlier_files(self):
        changes = migration.render(self.consumer)
        before = self.snapshot()
        replace = os.replace
        calls = 0

        def interrupted(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("synthetic rename failure")
            return replace(source, destination)

        with patch.object(migration.os, "replace", side_effect=interrupted), self.assertRaises(OSError):
            migration.apply(self.consumer, changes)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_later_edit_is_preserved_and_earlier_write_rolled_back(self):
        changes = migration.render(self.consumer)
        names = list(changes)
        before = self.snapshot()
        replace = os.replace
        calls = 0
        changed = self.consumer / names[1]

        def concurrent(source, destination):
            nonlocal calls
            calls += 1
            if calls == 1:
                changed.write_text("# concurrent user edit\n")
            return replace(source, destination)

        with patch.object(migration.os, "replace", side_effect=concurrent), self.assertRaisesRegex(ValueError, "before replacement"):
            migration.apply(self.consumer, changes)
        after = self.snapshot()
        self.assertEqual(after[names[0]], before[names[0]])
        self.assertEqual(changed.read_text(), "# concurrent user edit\n")
        self.assertFalse(any(".release-distribution-" in name for name in after))


class DistributionGeneratorTests(unittest.TestCase):
    def test_versioned_full_renderer_ships_helper_and_portable_test(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / ".github/workflows", root / ".github/workflows")
            shutil.copyfile(ROOT / ".release-policy.json", root / ".release-policy.json")
            shutil.copyfile(ROOT / ".github/fuzz.json", root / ".github/fuzz.json")
            files = installer.render(root)
            self.assertIn("scripts/release_distribution.py", files)
            self.assertIn(".github/release-tests/test_release_distribution.py", files)
            self.assertIn("docs/release-distribution.md", files)
            self.assertIn(migration.HEADER, files["scripts/release_distribution.py"])

    def test_policy_opt_in_is_exact_and_optional(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {"repository": "owner/repo"}
            installer.validate_policy(root, base)
            versioning = {"schema": 1, "promotion": "promote-bytes", "files": [{"path": "version", "format": "text"}]}
            installer.validate_policy(root, {**base, "versioning": versioning, "stable_distribution": "rc-payloads-v1"})
            for value in (None, True, False, 1, "unknown"):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "stable_distribution"):
                    installer.validate_policy(root, {**base, "stable_distribution": value})

    def test_compact_requires_promote_bytes_without_changing_other_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            for versioning in (None, {"schema": 1, "promotion": "final-build"}):
                with self.subTest(versioning=versioning), self.assertRaisesRegex(ValueError, "promote-bytes"):
                    installer.validate_policy(Path(temporary), {"repository": "owner/repo", "versioning": versioning, "stable_distribution": "rc-payloads-v1"})

    def test_compact_inventory_signing_rejected_before_signer_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            policy = {"repository": "owner/repo", "stable_distribution": "rc-payloads-v1", "sign_releases": True}
            with self.assertRaisesRegex(ValueError, "full-inventory signing"):
                installer.validate_policy(Path(temporary), policy)


class ShippedDistributionDeltaTests(unittest.TestCase):
    """Keep the narrow delta tied to reviewed canonical source sections."""

    def test_source_pins_and_target_boundary(self):
        specification = json.loads(migration.DELTA.read_bytes())
        self.assertEqual(specification["consumer_source"], "7677cfbbe1395169403790307d3d9dd0fc25a43b")
        self.assertEqual(set(specification["absent_paths"]), {"scripts/release_closed.py", "scripts/release_promotion.py"})
        names = set(specification["files"])
        self.assertTrue({"scripts/release_distribution.py", ".github/release-tests/test_release_distribution.py", ".github/release-tests/test_versioned_release.py"} <= names)
        for name, entry in specification["files"].items():
            self.assertTrue(migration.destination(name), name)
            if entry["before_sha256"] is None:
                self.assertEqual(migration.digest(migration.canonical_copy(entry)), entry["after_sha256"])
                continue
            source = (ROOT / entry["canonical_source"]).read_bytes()
            self.assertEqual(migration.digest(source), entry["canonical_source_sha256"], name)
            text = source.decode()
            if entry["canonical_source"].startswith("tests/"):
                text = text.replace("Path(__file__).parents[1]", "Path(__file__).parents[2]")
                text = text.replace("Path(__file__).resolve().parents[1]", "Path(__file__).resolve().parents[2]")
            if name.endswith(".py"):
                text = installer.consumer_python(text)
            for hunk in entry["hunks"]:
                self.assertEqual(text.count(hunk["after"]), 1, name)


if __name__ == "__main__":
    unittest.main()
