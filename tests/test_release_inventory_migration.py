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
        inventory_plan = self.expected[migration.PLAN]
        casefold_plan = inventory_plan.replace("'inventory'", "'casefold'")
        casefold = {
            "hunks": [{"before": "return 'inventory'", "after": "return 'casefold'"}],
            "original_to_current": {
                receipts.digest(inventory_plan.replace(receipts.HEADER, "", 1)):
                receipts.digest(casefold_plan.replace(receipts.HEADER, "", 1))},
            "current": [receipts.digest(casefold_plan.replace(receipts.HEADER, "", 1))],
        }
        limited_plan = casefold_plan.replace("'casefold'", "'limited'")
        name_limit = {
            "hunks": [{"before": "return 'casefold'", "after": "return 'limited'"}],
            "original_to_current": {
                receipts.digest(casefold_plan.replace(receipts.HEADER, "", 1)):
                receipts.digest(limited_plan.replace(receipts.HEADER, "", 1))},
            "current": [receipts.digest(limited_plan.replace(receipts.HEADER, "", 1))],
        }
        self.expected[migration.PLAN] = limited_plan
        self.enterContext(patch.object(migration, "CASEFOLD", casefold))
        self.enterContext(patch.object(migration, "NAME_LIMIT", name_limit))
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
        for value in (None, [], "release-inputs-package.json", ["../package.json"], ["release-inputs-fuzz.json"], ["release-inputs-package.json"] * 2, ["release-inputs-" + "x" * 181 + ".json"]):
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
                inventory_v1 = migration._before_casefold(current) if name == migration.PLAN else current
                if name == receipts.PUBLISHER:
                    inventory_v1 = migration._before_notes_provenance(current)
                self.assertEqual(result, inventory_v1.replace("#!/usr/bin/env python3\n", PREFIX, 1))
                with self.assertRaisesRegex(ValueError, "Unknown current"):
                    migration.previous_canonical_source(current + "# unknown\n", name)


class PlanLayerCompatibilityTests(unittest.TestCase):
    def setUp(self):
        """Freeze real canonical states, including the literal accepted v1 plan hash."""
        self.current = {name: (ROOT / name).read_text() for name in migration.FILES}
        self.notes_publisher = self.current[receipts.PUBLISHER]
        # Inventory-only upgrades retain their literal pre-notes publisher scope.
        self.current[receipts.PUBLISHER] = migration._before_notes_provenance(self.notes_publisher)
        self.casefold = dict(self.current)
        self.casefold[migration.PLAN] = migration._before_name_limit(self.current[migration.PLAN])
        self.assertEqual(receipts.digest(self.casefold[migration.PLAN]),
                         "212ad31b8fa380b0ef556b82fd4ed6f2d49cd59ee4516956e98bb8a9120483e9")
        self.v1 = dict(self.current)
        self.v1[migration.PLAN] = migration._before_casefold(self.current[migration.PLAN])
        self.assertEqual(receipts.digest(self.v1[migration.PLAN]),
                         "fbc56a9d494678f6b5d9180be02d587897c2b5276cf46e9102ac61089c91dde2")
        self.original = {name: migration.previous_canonical_source(source, name)
                         for name, source in self.current.items()}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        (self.root / ".release-policy.json").write_text(json.dumps({"mode": "release",
            "versioning": {"schema": 1,
                "required_build_receipts": ["release-inputs-package.json"]}}))

    def write_state(self, state):
        for name, source in state.items():
            (self.root / name).write_text(source.replace("#!/usr/bin/env python3\n", PREFIX, 1))

    def snapshot(self):
        return {name: (self.root / name).read_bytes() for name in migration.FILES}

    def expected(self, names):
        return {name: self.current[name].replace("#!/usr/bin/env python3\n", PREFIX, 1)
                for name in names}

    def test_pre_inventory_source_upgrades_to_current(self):
        self.write_state(self.original)
        before = self.snapshot()
        self.assertEqual(migration.render(self.root), self.expected(migration.FILES))
        self.assertEqual(self.snapshot(), before)

    def test_complete_inventory_v1_changes_only_the_plan_validator(self):
        self.write_state(self.v1)
        before = self.snapshot()
        changes = migration.render(self.root)
        self.assertEqual(changes, self.expected([migration.PLAN]))
        self.assertEqual(self.snapshot(), before)
        for name, text in changes.items():
            (self.root / name).write_text(text)
        self.assertEqual(migration.render(self.root), {})

    def test_casefold_inventory_adds_only_the_length_guard(self):
        self.write_state(self.casefold)
        before = self.snapshot()
        changes = migration.render(self.root)
        self.assertEqual(changes, self.expected([migration.PLAN]))
        self.assertEqual(self.snapshot(), before)
        for name, text in changes.items():
            (self.root / name).write_text(text)
        self.assertEqual(migration.render(self.root), {})

    def test_notes_layer_reverses_exactly_without_changing_the_inventory_oracle(self):
        self.assertEqual(receipts.digest(self.current[receipts.PUBLISHER]),
                         "179a6fe589801c59bccfea0c9a0c2e1b5d0be1915e87e1b4ec9313f32380aa7a")
        vendored = self.notes_publisher.replace("#!/usr/bin/env python3\n", PREFIX, 1)
        self.assertEqual(migration._before_notes_provenance(vendored),
                         self.current[receipts.PUBLISHER].replace("#!/usr/bin/env python3\n", PREFIX, 1))
        self.assertEqual(migration.previous_canonical_source(self.notes_publisher, receipts.PUBLISHER),
                         self.original[receipts.PUBLISHER])

    def test_inventory_only_preserves_but_does_not_install_the_notes_fix(self):
        self.write_state({**self.current, receipts.PUBLISHER: self.notes_publisher})
        self.assertEqual(migration.render(self.root), {})
        self.write_state({**self.casefold, receipts.PUBLISHER: self.notes_publisher})
        before = self.snapshot()
        self.assertEqual(migration.render(self.root), self.expected([migration.PLAN]))
        self.assertEqual(self.snapshot(), before)
        self.write_state(self.original)
        changes = migration.render(self.root)
        self.assertEqual(changes[receipts.PUBLISHER], self.expected([receipts.PUBLISHER])[receipts.PUBLISHER])
        self.assertNotIn("def candidate_provenance(", changes[receipts.PUBLISHER])

    def test_create_readback_layer_reverses_to_exact_notes_only_source(self):
        notes_only = migration._before_create_readback(self.notes_publisher)
        self.assertEqual(receipts.digest(notes_only),
                         "579c4409b768f90311a58eb6e9afef2eb423fa4ccdd75f9056e31d0e3497ee99")
        self.assertNotIn("def create_marker(", notes_only)
        state, result = receipts.transform(
            notes_only.replace("#!/usr/bin/env python3\n", PREFIX, 1),
            receipts.PUBLISHER, migration.CREATE_READBACK,
        )
        self.assertEqual(state, "original")
        self.assertEqual(result, migration._before_create_identity(self.notes_publisher).replace(
            "#!/usr/bin/env python3\n", PREFIX, 1))

    def test_inventory_only_preserves_but_does_not_install_create_readback(self):
        notes_only = migration._before_create_readback(self.notes_publisher)
        for publisher in (notes_only, self.notes_publisher):
            with self.subTest(publisher_hash=receipts.digest(publisher)):
                self.write_state({**self.current, receipts.PUBLISHER: publisher})
                self.assertEqual(migration.render(self.root), {})
                self.write_state({**self.casefold, receipts.PUBLISHER: publisher})
                before = self.snapshot()
                self.assertEqual(migration.render(self.root), self.expected([migration.PLAN]))
                self.assertEqual(self.snapshot(), before)
        self.write_state(self.original)
        self.assertNotIn("def create_marker(", migration.render(self.root)[receipts.PUBLISHER])

    def test_create_identity_layer_reverses_exactly_and_preserves_each_version(self):
        previous = migration._before_create_identity(self.notes_publisher)
        self.assertEqual(receipts.digest(previous),
                         "2d2e1b65544c817107a4d7a5008fcc11335a9408faec4cb159c87d454d58c7dd")
        state, current = receipts.transform(previous.replace("#!/usr/bin/env python3\n", PREFIX, 1),
                                            receipts.PUBLISHER, migration.CREATE_IDENTITY)
        self.assertEqual(state, "original")
        self.assertEqual(current, migration._before_automatic_source(self.notes_publisher).replace("#!/usr/bin/env python3\n", PREFIX, 1))
        for publisher in (previous, self.notes_publisher):
            self.write_state({**self.current, receipts.PUBLISHER: publisher})
            before = self.snapshot()
            self.assertEqual(migration.render(self.root), {})
            self.assertEqual(self.snapshot(), before)
            self.write_state({**self.casefold, receipts.PUBLISHER: publisher})
            self.assertEqual(migration.render(self.root), self.expected([migration.PLAN]))

    def test_automatic_source_layer_preserves_old_and_new_without_installing(self):
        previous = migration._before_automatic_source(self.notes_publisher)
        self.assertEqual(receipts.digest(previous),
                         "978783bc3465c174f45669fe38568337d8c886161749f8d1c050ab67d8999e9d")
        state, current = receipts.transform(previous.replace("#!/usr/bin/env python3\n", PREFIX, 1),
                                            receipts.PUBLISHER, migration.AUTOMATIC_SOURCE)
        self.assertEqual(state, "original")
        self.assertEqual(current, migration._before_execution_job(self.notes_publisher).replace(
            "#!/usr/bin/env python3\n", PREFIX, 1))
        for publisher in (previous, self.notes_publisher):
            self.write_state({**self.current, receipts.PUBLISHER: publisher})
            before = self.snapshot()
            self.assertEqual(migration.render(self.root), {})
            self.assertEqual(self.snapshot(), before)
            self.write_state({**self.casefold, receipts.PUBLISHER: publisher})
            self.assertEqual(migration.render(self.root), self.expected([migration.PLAN]))
        self.write_state(self.original)
        self.assertNotIn("confirm_automatic_source", migration.render(self.root)[receipts.PUBLISHER])

    def test_execution_layer_roundtrips_exact_accepted_source_and_vendor_header(self):
        previous = migration._before_execution_job(self.notes_publisher)
        self.assertEqual(receipts.digest(previous),
                         "19cb27817c0aaf75867cdfba01a72dacb1026f71773bf42acb6527fc8553cb42")
        self.assertEqual(receipts.digest(self.notes_publisher),
                         "673f336995440c7b1c616322be470945e8d645a5144b102f686e037d6526564d")
        self.assertNotIn("ExecutionJobProof", previous)
        self.assertNotIn("execution_proof", previous)
        vendored_previous = previous.replace("#!/usr/bin/env python3\n", PREFIX, 1)
        vendored_current = self.notes_publisher.replace("#!/usr/bin/env python3\n", PREFIX, 1)
        state, result = receipts.transform(vendored_previous, receipts.PUBLISHER, migration.EXECUTION_JOB)
        self.assertEqual((state, result), ("original", vendored_current))
        self.assertEqual(migration._before_execution_job(vendored_current), vendored_previous)
        self.assertEqual(migration._before_execution_job(previous), previous)
        self.assertEqual(receipts.transform(result, receipts.PUBLISHER, migration.EXECUTION_JOB),
                         ("current", result))
        for source in (previous, self.notes_publisher):
            self.assertEqual(receipts.digest(migration.previous_canonical_source(source, receipts.PUBLISHER)),
                             "e27e76d2a6080bb257ce60420cf9ead4a4a86f1b4a86f568225aa6ee9706875e")

    def test_inventory_preserves_execution_layer_without_installing_it(self):
        previous = migration._before_execution_job(self.notes_publisher)
        for publisher in (previous, self.notes_publisher):
            with self.subTest(publisher_hash=receipts.digest(publisher)):
                self.write_state({**self.current, receipts.PUBLISHER: publisher})
                before = self.snapshot()
                self.assertEqual(migration.render(self.root), {})
                self.assertEqual(self.snapshot(), before)
                self.write_state({**self.casefold, receipts.PUBLISHER: publisher})
                before = self.snapshot()
                changes = migration.render(self.root)
                self.assertEqual(changes, self.expected([migration.PLAN]))
                self.assertEqual(self.snapshot(), before)
                for name, text in changes.items():
                    (self.root / name).write_text(text)
                self.assertEqual(migration.render(self.root), {})
                self.assertEqual((self.root / receipts.PUBLISHER).read_bytes(), before[receipts.PUBLISHER])
        self.write_state(self.original)
        publisher = migration.render(self.root)[receipts.PUBLISHER]
        self.assertEqual(publisher, self.expected([receipts.PUBLISHER])[receipts.PUBLISHER])
        self.assertNotIn("ExecutionJobProof", publisher)
        self.assertNotIn("execution_proof", publisher)

    def test_partial_or_tampered_execution_layer_rejects_before_any_write(self):
        variants = [self.notes_publisher + " "]
        for hunk in migration.EXECUTION_JOB["hunks"]:
            variants.append(self.notes_publisher.replace(hunk["after"], hunk["before"], 1))
        for publisher in variants:
            with self.subTest(publisher_hash=receipts.digest(publisher)):
                self.assertNotEqual(publisher, self.notes_publisher)
                self.write_state({**self.casefold, receipts.PUBLISHER: publisher})
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "Unknown or partially migrated"):
                    migration.render(self.root)
                with self.assertRaisesRegex(ValueError, "Unknown current"):
                    migration.previous_canonical_source(publisher, receipts.PUBLISHER)
                with patch.object(sys, "argv", ["install_release.py", str(self.root),
                                               "--release-inventory-only"]), \
                        redirect_stderr(io.StringIO()), redirect_stdout(io.StringIO()):
                    self.assertEqual(installer.main(), 1)
                self.assertEqual(self.snapshot(), before)

    def test_partial_or_unknown_automatic_source_layer_is_rejected(self):
        first = migration.AUTOMATIC_SOURCE["hunks"][0]
        variants = (self.notes_publisher.replace(first["after"], first["before"], 1),
                    self.notes_publisher.replace("def confirm_automatic_source()", "def changed_guard()", 1))
        for publisher in variants:
            self.assertNotEqual(publisher, self.notes_publisher)
            self.write_state({**self.current, receipts.PUBLISHER: publisher})
            before = self.snapshot()
            with self.assertRaisesRegex(ValueError, "Unknown or partially migrated"):
                migration.render(self.root)
            self.assertEqual(self.snapshot(), before)
            with self.assertRaisesRegex(ValueError, "Unknown current"):
                migration.previous_canonical_source(publisher, receipts.PUBLISHER)

    def test_partial_or_changed_create_identity_is_rejected(self):
        first = migration.CREATE_IDENTITY["hunks"][0]
        for publisher in (self.notes_publisher.replace('info = readback.json("")', 'info = {}', 1),
                          self.notes_publisher.replace(first["after"], first["before"] + first["after"], 1)):
            self.assertNotEqual(publisher, self.notes_publisher)
            self.write_state({**self.current, receipts.PUBLISHER: publisher})
            before = self.snapshot()
            with self.assertRaisesRegex(ValueError, "Unknown or partially migrated"):
                migration.render(self.root)
            self.assertEqual(self.snapshot(), before)
            with self.assertRaisesRegex(ValueError, "Unknown current"):
                migration.previous_canonical_source(publisher, receipts.PUBLISHER)

    def test_partial_or_changed_create_readback_layer_is_rejected(self):
        first = migration.CREATE_READBACK["hunks"][0]
        partial = self.notes_publisher.replace(first["after"], first["before"], 1)
        self.assertNotEqual(partial, self.notes_publisher)
        for publisher in (partial, self.notes_publisher + "# unreviewed readback\n",
                          self.notes_publisher.replace("def create_marker(", "def modified_marker(", 1)):
            with self.subTest(publisher_hash=receipts.digest(publisher)):
                self.write_state({**self.current, receipts.PUBLISHER: publisher})
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "Unknown or partially migrated"):
                    migration.render(self.root)
                self.assertEqual(self.snapshot(), before)
                with self.assertRaisesRegex(ValueError, "Unknown current"):
                    migration.previous_canonical_source(publisher, receipts.PUBLISHER)

    def test_partial_or_changed_notes_layer_is_not_a_recognized_source(self):
        for source in (self.notes_publisher + "# unreviewed\n",
                       self.notes_publisher.replace("def candidate_provenance(", "def modified_provenance(", 1)):
            with self.subTest(source_hash=receipts.digest(source)):
                self.write_state({**self.current, receipts.PUBLISHER: source})
                before = self.snapshot()
                with self.assertRaisesRegex(ValueError, "Unknown or partially migrated"):
                    migration.render(self.root)
                self.assertEqual(self.snapshot(), before)
                with self.assertRaisesRegex(ValueError, "Unknown current"):
                    migration.previous_canonical_source(source, receipts.PUBLISHER)

    def test_current_inventory_is_idempotent(self):
        self.write_state(self.current)
        self.assertEqual(migration.render(self.root), {})

    def test_partial_inventory_is_still_rejected_for_all_plan_versions(self):
        for plan in (self.v1[migration.PLAN], self.casefold[migration.PLAN], self.current[migration.PLAN]):
            self.write_state({**self.original, migration.PLAN: plan})
            before = self.snapshot()
            with self.assertRaisesRegex(ValueError, "Partially migrated"):
                migration.render(self.root)
            self.assertEqual(self.snapshot(), before)

    def test_unrecognized_source_does_not_produce_partial_edits(self):
        for name in migration.FILES:
            self.write_state(self.current)
            path = self.root / name
            path.write_text(path.read_text() + "# unreviewed customization\n")
            before = self.snapshot()
            with self.assertRaisesRegex(ValueError, "Unknown or partially migrated"):
                migration.render(self.root)
            self.assertEqual(self.snapshot(), before)

    def test_all_plan_versions_reverse_to_the_unchanged_original_oracle(self):
        for source in (self.v1[migration.PLAN], self.casefold[migration.PLAN], self.current[migration.PLAN]):
            self.assertEqual(receipts.digest(migration.previous_canonical_source(source, migration.PLAN)),
                             "e29350ddaa08667bd9ecca2d5a18c6b1e188826fd69c4e77027372aad0cd2d1e")


if __name__ == "__main__":
    unittest.main()
