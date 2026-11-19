"""Guarded compact backport boundaries, rollback and generator integration."""
from contextlib import redirect_stderr, redirect_stdout
import importlib
import io
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
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

    ADVANCED_SUCCESSOR_FILES = (
        "scripts/release_control.py", "scripts/release_versioned.py",
        ".github/release-tests/test_release_control.py",
        ".github/release-tests/test_versioned_release.py",
    )

    @staticmethod
    def _expected_postimage(entry):
        copy_layers = entry.get("copy_successor_layers", [])
        if copy_layers:
            return copy_layers[-1]["after_sha256"]
        production = entry["successor_layers"][-1]["after_sha256"] if entry.get("successor_layers") else entry["after_sha256"]
        overlays = [group for group in entry.get("test_only_overlay_successors", [])
                    if group.get("base_sha256") == production]
        if overlays:
            return overlays[-1]["layers"][-1]["after_sha256"]
        test_layers = entry.get("test_only_successor_layers", [])
        if test_layers and test_layers[0].get("before_sha256") == production:
            return test_layers[-1]["after_sha256"]
        return production

    def test_compact_consumer_adds_shared_process_supervisor_and_regressions(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            self._extract_pinned_ott_snapshot(consumer)
            changes = migration.render(consumer)
        helper = changes["scripts/async_process.py"]
        tests = changes[".github/release-tests/test_async_process.py"]
        publisher = changes["scripts/release_control.py"]
        self.assertIn("_SUPERVISOR_SOURCE", helper)
        self.assertIn("test_supervisor_control_pipe_eof_kills_owned_group", tests)
        self.assertIn("_launch_supervised_process", publisher)
        self.assertIn("supervisor status frame", helper)

    def test_async_helper_copy_successor_preserves_old_layer_and_rejects_unknown_hash(self):
        specification = json.loads(migration.DELTA.read_bytes())
        entry = specification["files"]["scripts/async_process.py"]
        layers = entry["copy_successor_layers"]
        layer = layers[-1]
        expected_before = layers[-2]["after_sha256"] if len(layers) > 1 else entry["after_sha256"]
        self.assertEqual(layer["before_sha256"], expected_before)
        helper_text = (ROOT / "scripts/async_process.py").read_text(encoding="utf-8")
        for hunk in reversed(layer["source_hunks"]):
            self.assertEqual(helper_text.count(hunk["after"]), 1)
            helper_text = helper_text.replace(hunk["after"], hunk["before"], 1)
        self.assertEqual(
            migration.digest(helper_text.encode()),
            layer["canonical_source_before_sha256"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            wrong_source_root = Path(temporary)
            (wrong_source_root / "scripts").mkdir()
            (wrong_source_root / "scripts/async_process.py").write_text(
                "# unknown source state\n", encoding="utf-8",
            )
            with patch.object(migration, "ROOT", wrong_source_root):
                with self.assertRaisesRegex(ValueError, "source changed"):
                    migration.canonical_copy(entry)
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            self._extract_pinned_ott_snapshot(consumer)
            unknown = consumer / "scripts/async_process.py"
            unknown.parent.mkdir(parents=True, exist_ok=True)
            unknown.write_bytes(b"unknown helper source\n")
            with self.assertRaisesRegex(ValueError, "Unknown compact migration preimage"):
                migration.render(consumer)

    def _historical_consumer(self, temporary, include_old_test_overlay=False):
        """Return the pinned consumer after applying the prior four-file layer."""
        specification = json.loads(migration.DELTA.read_bytes())
        historical = json.loads(json.dumps(specification))
        for name in self.ADVANCED_SUCCESSOR_FILES:
            rewinds = 4 if include_old_test_overlay else 1
            for _ in range(rewinds):
                historical["files"][name]["successor_layers"].pop()
        for name, entry in historical["files"].items():
            if entry.get("copy_successor_layers"):
                entry["copy_successor_layers"].pop()
        if not include_old_test_overlay:
            historical["files"][".github/release-tests/test_release_control.py"].pop(
                "test_only_successor_layers", None,
            )
            historical["files"][".github/release-tests/test_release_control.py"].pop(
                "test_only_overlay_successors", None,
            )
        for name in ("scripts/release_distribution.py", ".github/release-tests/test_release_distribution.py"):
            historical["files"][name]["successor_layers"].pop()
        historical["files"].pop("scripts/release_state.py")

        # These two compact files were original copies before this successor.
        # Reverse their exact vendored successor hunks to reconstruct the
        # original canonical bytes pinned by the prior migration.
        canonical = Path(temporary) / "historical-canonical"
        for name in ("scripts", "tests", "docs"):
            shutil.copytree(ROOT / name, canonical / name, dirs_exist_ok=True)
        for name, entry in specification["files"].items():
            if not entry.get("copy_successor_layers"):
                continue
            source = canonical / entry["source"]
            source_text = source.read_text(encoding="utf-8")
            latest = entry["copy_successor_layers"][-1]
            for hunk in reversed(latest["source_hunks"]):
                if not hunk["after"] or source_text.count(hunk["after"]) != 1:
                    raise AssertionError("copied-source successor cannot be reversed exactly: " + name)
                source_text = source_text.replace(hunk["after"], hunk["before"], 1)
            self.assertEqual(
                migration.digest(source_text.encode()),
                latest["canonical_source_before_sha256"],
            )
            source.write_text(source_text, encoding="utf-8")
        for name in ("scripts/release_distribution.py", ".github/release-tests/test_release_distribution.py"):
            entry = specification["files"][name]
            source = entry["source"]
            raw = (ROOT / source).read_text(encoding="utf-8")
            self.assertEqual(migration.digest(raw.encode()),
                             entry["successor_layers"][-1]["canonical_source_sha256"])
            if source.startswith("tests/"):
                raw = raw.replace("Path(__file__).parents[1]", "Path(__file__).parents[2]")
                raw = raw.replace("Path(__file__).resolve().parents[1]", "Path(__file__).resolve().parents[2]")
            vendored = installer.consumer_python(raw)
            for layer in reversed(entry["successor_layers"]):
                for hunk in reversed(layer["hunks"]):
                    self.assertTrue(hunk["after"])
                    self.assertEqual(vendored.count(hunk["after"]), 1, name)
                    vendored = vendored.replace(hunk["after"], hunk["before"], 1)
            self.assertTrue(vendored.startswith(migration.HEADER))
            raw = vendored[len(migration.HEADER):]
            if source.startswith("tests/"):
                raw = raw.replace("Path(__file__).resolve().parents[2]", "Path(__file__).resolve().parents[1]")
                raw = raw.replace("Path(__file__).parents[2]", "Path(__file__).parents[1]")
            self.assertEqual(migration.digest(raw.encode()), entry["source_sha256"], name)
            target = canonical / source
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(raw, encoding="utf-8")

        consumer = Path(temporary) / "consumer"
        consumer.mkdir()
        self._extract_pinned_ott_snapshot(consumer)
        old_delta = Path(temporary) / "historical-delta.json"
        old_delta.write_text(json.dumps(historical))
        with patch.object(migration, "DELTA", old_delta), patch.object(migration, "ROOT", canonical):
            old_changes = migration.render(consumer)
            migration.apply(consumer, old_changes)
        return consumer, specification, old_changes

    def test_rendered_publication_dispatches_schema_one_and_two(self):
        """Exercise both paths in the actual pinned OTT consumer postimage."""
        import importlib.util
        import types

        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            self._extract_pinned_ott_snapshot(consumer)
            migration.apply(consumer, migration.render(consumer))

            control = types.ModuleType("release_control")
            for name, value in {
                "MANIFEST": "release-manifest.json",
                "DownloadBudget": object,
                "GitHub": object,
                "ReleaseError": RuntimeError,
                "download_asset": lambda *args, **kwargs: None,
                "positive": lambda value, label: value,
                "repository_info": lambda gh: {},
                "require": lambda *args, **kwargs: None,
                "require_release_policy": lambda *args, **kwargs: None,
                "source_policy_snapshot": lambda *args, **kwargs: {},
                "validate_manifest": lambda *args, **kwargs: {},
                "validate_run": lambda *args, **kwargs: None,
                "verify_evidence": lambda *args, **kwargs: None,
            }.items():
                setattr(control, name, value)
            versioned = types.ModuleType("release_versioned")
            versioned.read_verified_distribution = lambda *args, **kwargs: ({"schema": 2}, b"", {})
            module_name = "_pinned_ott_publish_verified"
            spec = importlib.util.spec_from_file_location(
                module_name, consumer / "scripts/publish_verified.py",
            )
            self.assertIsNotNone(spec)
            module = importlib.util.module_from_spec(spec)
            with patch.dict(sys.modules, {"release_control": control, "release_versioned": versioned}):
                sys.modules[module_name] = module
                self.addCleanup(sys.modules.pop, module_name, None)
                spec.loader.exec_module(module)

                gh, directory = object(), Path(temporary)
                legacy = {"schema": 1}
                manifest, policy = {"tag": "v1.2.3"}, {"data": {}}
                with (
                    patch.object(module, "_read_stable_release", return_value=(
                        {}, {}, [], b"legacy", legacy,
                    )) as reader,
                    patch.object(module, "_validate_legacy_stable_provenance",
                                 return_value=(manifest, policy)) as validate,
                    patch.object(module, "_download_stable_inventory") as download,
                    patch.object(module, "_verify_stable_receipts") as receipts,
                ):
                    self.assertEqual(module.verified_assets(gh, "v1.2.3", directory), manifest)
                    reader.assert_called_once_with(gh, "v1.2.3")
                    validate.assert_called_once_with(gh, "v1.2.3", {}, {}, b"legacy", legacy)
                    download.assert_called_once_with(gh, [], manifest, directory)
                    receipts.assert_called_once_with(directory, manifest, policy)

                compact_manifest = {"schema": 2, "source": "retained-rc"}
                with (
                    patch.object(module, "_read_stable_release", return_value=(
                        {}, {}, [], b"compact", {"schema": 2},
                    )),
                    patch.object(module, "_validate_legacy_stable_provenance") as legacy_validate,
                    patch.object(module, "_download_stable_inventory") as legacy_download,
                    patch.object(module, "_verify_stable_receipts") as legacy_receipts,
                    patch.object(versioned, "read_verified_distribution",
                                 return_value=(compact_manifest, b"", {})) as read_compact,
                ):
                    self.assertEqual(module.verified_assets(gh, "v1.2.3", directory), compact_manifest)
                    read_compact.assert_called_once_with(gh, "v1.2.3", b"compact", stage=directory)
                    legacy_validate.assert_not_called()
                    legacy_download.assert_not_called()
                    legacy_receipts.assert_not_called()

    def _extract_pinned_ott_snapshot(self, root):
        fixture_dir = ROOT / "tests/fixtures/release_distribution"
        metadata = json.loads((fixture_dir / "ottplay-foss-7677cfb.json").read_bytes())
        self.assertEqual(metadata["repository"], "open-ott-play/ottplay-foss")
        self.assertEqual(metadata["commit"], "7677cfbbe1395169403790307d3d9dd0fc25a43b")
        expected = {item["path"]: item for item in metadata["members"]}
        archive = fixture_dir / "ottplay-foss-7677cfb.tar.gz"
        with tarfile.open(archive, "r:gz") as source:
            members = source.getmembers()
            self.assertEqual({item.name for item in members}, set(expected))
            for item in members:
                self.assertTrue(item.isfile(), item.name)
                self.assertEqual(item.name, Path(item.name).as_posix())
                self.assertNotIn("..", Path(item.name).parts)
                stream = source.extractfile(item)
                self.assertIsNotNone(stream)
                data = stream.read()
                self.assertEqual(len(data), expected[item.name]["size"], item.name)
                self.assertEqual(hashlib.sha256(data).hexdigest(), expected[item.name]["sha256"], item.name)
                destination = root / item.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        self.assertEqual(set(expected) - {".release-policy.json"},
                         {name for name, entry in json.loads(migration.DELTA.read_bytes())["files"].items()
                          if entry["before_sha256"] is not None})

    def test_pinned_ott_base_applies_exact_postimages_and_wrong_base_is_rejected(self):
        specification = json.loads(migration.DELTA.read_bytes())
        self.assertEqual(specification["consumer_source"], "7677cfbbe1395169403790307d3d9dd0fc25a43b")
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            self._extract_pinned_ott_snapshot(consumer)
            policy_before = (consumer / ".release-policy.json").read_bytes()
            preimage = {
                str(path.relative_to(consumer)): path.read_bytes()
                for path in consumer.rglob("*") if path.is_file()
            }
            changes = migration.render(consumer)
            self.assertEqual(set(changes), set(specification["files"]))
            for name, entry in specification["files"].items():
                expected_sha = self._expected_postimage(entry)
                self.assertEqual(hashlib.sha256(changes[name].encode("utf-8")).hexdigest(),
                                 expected_sha, name)
            self.assertEqual({str(path.relative_to(consumer)): path.read_bytes()
                              for path in consumer.rglob("*") if path.is_file()}, preimage)
            migration.apply(consumer, changes)
            self.assertEqual(migration.render(consumer), {})
            self.assertEqual((consumer / ".release-policy.json").read_bytes(), policy_before)
            for name, entry in specification["files"].items():
                expected_sha = self._expected_postimage(entry)
                self.assertEqual(hashlib.sha256((consumer / name).read_bytes()).hexdigest(),
                                 expected_sha, name)
            self.assertEqual({str(path.relative_to(consumer)) for path in consumer.rglob("*") if path.is_file()},
                             set(preimage) | set(specification["files"]))

        with tempfile.TemporaryDirectory() as temporary:
            wrong_base = Path(temporary) / "consumer"
            wrong_base.mkdir()
            self._extract_pinned_ott_snapshot(wrong_base)
            changed = wrong_base / "scripts/release_control.py"
            changed.write_bytes(changed.read_bytes() + b"\n# not the pinned base\n")
            before = {str(path.relative_to(wrong_base)): path.read_bytes()
                      for path in wrong_base.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(ValueError, "Unknown compact migration preimage"):
                migration.render(wrong_base)
            self.assertEqual({str(path.relative_to(wrong_base)): path.read_bytes()
                              for path in wrong_base.rglob("*") if path.is_file()}, before)

    def test_source_pins_and_target_boundary(self):
        specification = json.loads(migration.DELTA.read_bytes())
        self.assertEqual(specification["consumer_source"], "7677cfbbe1395169403790307d3d9dd0fc25a43b")
        self.assertEqual(set(specification["absent_paths"]), {"scripts/release_closed.py", "scripts/release_promotion.py"})
        names = set(specification["files"])
        self.assertTrue({"scripts/release_distribution.py", ".github/release-tests/test_release_distribution.py", ".github/release-tests/test_versioned_release.py"} <= names)
        for name, entry in specification["files"].items():
            self.assertTrue(migration.destination(name), name)
            if entry["before_sha256"] is None:
                copy_layers = entry.get("copy_successor_layers", [])
                latest = (copy_layers[-1]["after_sha256"] if copy_layers else
                          entry["successor_layers"][-1]["after_sha256"]
                          if entry.get("successor_layers") else entry["after_sha256"])
                self.assertEqual(migration.digest(migration.canonical_copy(entry)), latest)
                continue
            source = (ROOT / entry["canonical_source"]).read_bytes()
            independent_test_layers = entry.get("test_only_successor_layers", [])
            independent_test_overlays = entry.get("test_only_overlay_successors", [])
            production_layers = entry.get("successor_layers", [])
            production_base = production_layers[-1]["after_sha256"] if production_layers else entry["after_sha256"]
            production_overlay = next(
                (group for group in reversed(independent_test_overlays)
                 if group.get("base_sha256") == production_base),
                None,
            )
            source_pin = (
                entry["copy_successor_layers"][-1].get("canonical_source_sha256")
                if entry.get("copy_successor_layers") else
                production_overlay["layers"][-1].get("canonical_source_sha256")
                if production_overlay else
                independent_test_layers[-1].get("canonical_source_sha256")
                if independent_test_layers and independent_test_layers[0].get("before_sha256") == production_base else
                production_layers[-1].get("canonical_source_sha256") if production_layers else None
            )
            self.assertEqual(migration.digest(source),
                             source_pin or entry["canonical_source_sha256"], name)
            text = source.decode()
            if entry["canonical_source"].startswith("tests/"):
                text = text.replace("Path(__file__).parents[1]", "Path(__file__).parents[2]")
                text = text.replace("Path(__file__).resolve().parents[1]", "Path(__file__).resolve().parents[2]")
            if name == "docs/release-workflow.md":
                # The compact migration deliberately retains its prior one-POST
                # guide until its selective consumer also adopts this uploader.
                continue
            if name.endswith(".py"):
                text = installer.consumer_python(text)
            if not entry.get("successor_layers"):
                for hunk in entry["hunks"]:
                    for fragment in hunk.get("canonical_fragments", [hunk["after"]]):
                        self.assertEqual(text.count(fragment), 1, name)
            for layer in entry.get("successor_layers", []):
                if layer.get("canonical_source_sha256") != migration.digest(source):
                    continue
                for fragment in layer.get("canonical_fragments", []):
                    self.assertEqual(text.count(fragment), 1, name)
            for layer in independent_test_layers:
                if layer.get("canonical_source_sha256") != migration.digest(source):
                    continue
                for fragment in layer.get("canonical_fragments", []):
                    self.assertEqual(text.count(fragment), 1, name)
            for overlay in independent_test_overlays:
                for layer in overlay["layers"]:
                    if layer.get("canonical_source_sha256") != migration.digest(source):
                        continue
                    for fragment in layer.get("canonical_fragments", []):
                        self.assertEqual(text.count(fragment), 1, name)
        workflow = specification["files"]["docs/release-workflow.md"]
        self.assertEqual(workflow["after_sha256"],
                         "8d9576bd62cfeaaebe4dfde00e7bb512c875136c630b7bfdf2148dd00d2f9144")
        self.assertIn("using one bounded request\nper file", workflow["hunks"][0]["after"])
        self.assertNotIn("Only an explicit HTTP 500", workflow["hunks"][0]["after"])

    def test_historical_postimage_advances_as_one_successor_layer(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer, current_specification, old_changes = self._historical_consumer(temporary)
            new_changes = migration.render(consumer)
            self.assertEqual(set(new_changes), set(self.ADVANCED_SUCCESSOR_FILES) | {
                "scripts/release_state.py", "scripts/release_distribution.py",
                ".github/release-tests/test_release_distribution.py",
                "scripts/async_process.py", ".github/release-tests/test_async_process.py",
            })
            for name in self.ADVANCED_SUCCESSOR_FILES:
                entry = current_specification["files"][name]
                self.assertEqual(hashlib.sha256(old_changes[name].encode()).hexdigest(),
                                 entry["successor_layers"][-2]["after_sha256"])
            migration.apply(consumer, new_changes)
            self.assertEqual(migration.render(consumer), {})
            for name, entry in current_specification["files"].items():
                final_sha = self._expected_postimage(entry)
                self.assertEqual(hashlib.sha256((consumer / name).read_bytes()).hexdigest(), final_sha)

    def test_new_successor_rejects_partial_and_mutated_four_file_bundles(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer, _, _ = self._historical_consumer(temporary)

            first = consumer / self.ADVANCED_SUCCESSOR_FILES[0]
            first.write_text(migration.render(consumer)[self.ADVANCED_SUCCESSOR_FILES[0]], encoding="utf-8")
            snapshot = {str(path.relative_to(consumer)): path.read_bytes()
                        for path in consumer.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(ValueError, "Partially migrated"):
                migration.render(consumer)
            self.assertEqual({str(path.relative_to(consumer)): path.read_bytes()
                              for path in consumer.rglob("*") if path.is_file()}, snapshot)

            first.write_bytes(first.read_bytes() + b"# unreviewed mutation\n")
            snapshot = {str(path.relative_to(consumer)): path.read_bytes()
                        for path in consumer.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(ValueError, "Unknown compact migration preimage"):
                migration.render(consumer)
            self.assertEqual({str(path.relative_to(consumer)): path.read_bytes()
                              for path in consumer.rglob("*") if path.is_file()}, snapshot)

    def test_old_production_overlay_advances_to_new_atomic_bundle_and_overlay(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer, specification, _ = self._historical_consumer(
                temporary, include_old_test_overlay=True,
            )
            target = ".github/release-tests/test_release_control.py"
            old_overlay = specification["files"][target]["test_only_overlay_successors"][-1]
            self.assertEqual(
                hashlib.sha256((consumer / target).read_bytes()).hexdigest(),
                old_overlay["layers"][-1]["after_sha256"],
            )
            changes = migration.render(consumer)
            self.assertLessEqual(set(self.ADVANCED_SUCCESSOR_FILES), set(changes))
            migration.apply(consumer, changes)
            self.assertEqual(migration.render(consumer), {})
            self.assertEqual(
                hashlib.sha256((consumer / target).read_bytes()).hexdigest(),
                self._expected_postimage(specification["files"][target]),
            )

    def test_unknown_overlay_hash_and_partial_overlay_bundle_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            self._extract_pinned_ott_snapshot(consumer)
            target = ".github/release-tests/test_release_control.py"

            unknown = consumer / target
            unknown.write_bytes(unknown.read_bytes() + b"# unknown overlay state\n")
            snapshot = {str(path.relative_to(consumer)): path.read_bytes()
                        for path in consumer.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(ValueError, "Unknown compact migration preimage"):
                migration.render(consumer)
            self.assertEqual({str(path.relative_to(consumer)): path.read_bytes()
                              for path in consumer.rglob("*") if path.is_file()}, snapshot)

            invalid_spec = json.loads(migration.DELTA.read_bytes())
            invalid_spec["files"][target]["test_only_overlay_successors"][-1][
                "base_sha256"
            ] = "f" * 64
            invalid_delta = Path(temporary) / "invalid-overlay-delta.json"
            invalid_delta.write_text(json.dumps(invalid_spec))
            with patch.object(migration, "DELTA", invalid_delta), \
                    self.assertRaisesRegex(ValueError, "unknown production base"):
                migration.render(consumer)

        with tempfile.TemporaryDirectory() as temporary:
            consumer, _, _ = self._historical_consumer(temporary, include_old_test_overlay=True)
            target = ".github/release-tests/test_release_control.py"
            current = migration.render(consumer)
            # Advance just the test overlay target, while leaving the four-file
            # production generation old. This must not authorize a partial state.
            consumer_path = consumer / target
            consumer_path.write_text(current[target], encoding="utf-8")
            snapshot = {str(path.relative_to(consumer)): path.read_bytes()
                        for path in consumer.rglob("*") if path.is_file()}
            with self.assertRaisesRegex(ValueError, "Partially migrated"):
                migration.render(consumer)
            self.assertEqual({str(path.relative_to(consumer)): path.read_bytes()
                              for path in consumer.rglob("*") if path.is_file()}, snapshot)


    def test_old_test_overlay_is_not_reapplied_to_new_production_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary) / "consumer"
            consumer.mkdir()
            self._extract_pinned_ott_snapshot(consumer)
            specification = json.loads(migration.DELTA.read_bytes())
            target = ".github/release-tests/test_release_control.py"
            base_only = json.loads(json.dumps(specification))
            base_only["files"][target].pop("test_only_successor_layers", None)
            base_only["files"][target].pop("test_only_overlay_successors", None)
            base_delta = Path(temporary) / "base-only-delta.json"
            base_delta.write_text(json.dumps(base_only))
            with patch.object(migration, "DELTA", base_delta):
                migration.apply(consumer, migration.render(consumer))

            self.assertEqual(migration.render(consumer), {})
            self.assertEqual(
                hashlib.sha256((consumer / target).read_bytes()).hexdigest(),
                self._expected_postimage(specification["files"][target]),
            )

    def test_mixed_successor_and_unknown_postimage_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            consumer, specification, _ = self._historical_consumer(temporary)

            control = consumer / "scripts/release_control.py"
            control.write_text(migration.render(consumer)["scripts/release_control.py"])
            with self.assertRaisesRegex(ValueError, "Partially migrated"):
                migration.render(consumer)

            versioned = consumer / "scripts/release_versioned.py"
            versioned.write_text(specification["files"]["scripts/release_versioned.py"]["successor_layers"][0]["hunks"][0]["after"],
                                 encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Unknown compact migration preimage"):
                migration.render(consumer)


if __name__ == "__main__":
    unittest.main()
