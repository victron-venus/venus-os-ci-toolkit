"""Keep automatic test-only release admission reproducible and fail closed."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import install_release as installer  # noqa: E402
import workflow_contracts as contracts  # noqa: E402

PATHS = [
    "tests/helpers/aspect-readiness-diagnostics.cjs",
    "tests/test_hls_aspect_fixture.cjs",
    "tests/test_native_local_media_siblings.py",
]


def normalized(value):
    return yaml.load(yaml.safe_dump(value), Loader=yaml.BaseLoader)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        from test_channel_validation import ChannelValidationTests

        self.fixture = ChannelValidationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.policy = self.fixture.policy

    def enable(self):
        self.policy["change_scope"]["automatic_release_test_paths"] = PATHS

    def test_only_opted_in_prepare_receipt_changes_and_other_callers_stay_full(self):
        before = installer.release(self.policy)
        quality = installer.quality(self.policy, self.root)
        release_quality = installer.release_quality(self.policy, self.root)
        self.enable()
        after = installer.release(self.policy)
        self.assertEqual(installer.quality(self.policy, self.root), quality)
        self.assertEqual(
            installer.release_quality(self.policy, self.root), release_quality
        )
        receipt = after["jobs"]["prepare"]["steps"].pop()
        self.assertEqual(
            receipt, installer.release_push_scope.receipt_step(installer.UPLOAD)
        )
        self.assertEqual(after, before)
        classifier = next(
            s for s in after["jobs"]["scope"]["steps"] if s.get("id") == "scope"
        )
        self.assertEqual(classifier["env"]["FORCE_FULL"], "true")

    def test_full_render_vendors_helper_tests_and_exact_drift_contract(self):
        self.enable()
        files = self.fixture.render()
        self.assertEqual(
            files["scripts/release_push_scope.py"],
            installer.consumer_python(
                (ROOT / "scripts/release_push_scope.py").read_text()
            ),
        )
        self.assertIn(".github/release-tests/test_release_push_scope.py", files)
        self.assertIn("automatic_release_test_paths", files["docs/release-workflow.md"])
        contracts.validate(self.root)
        self.assertEqual(installer.render(self.root), files)

    def test_invalid_opt_in_required_paths_and_validation_only_are_rejected(self):
        for paths in (
            [],
            ["src/index.ts"],
            ["tests/**"],
            ["tests/../src/a.py"],
            ["tests/.secret.py"],
            ["tests/media.mp4"],
            [PATHS[0]] * 2,
            "tests/test.py",
        ):
            with self.subTest(paths=paths):
                self.policy["change_scope"]["automatic_release_test_paths"] = paths
                with self.assertRaises(ValueError):
                    installer.release(self.policy)
        self.enable()
        self.policy["change_scope"]["required_paths"] = [PATHS[0]]
        with self.assertRaises(ValueError):
            installer.release(self.policy)
        self.policy["change_scope"]["required_paths"] = []
        self.policy["mode"] = "validation-only"
        with self.assertRaises(ValueError):
            installer.scope_policy(self.policy)

    def test_lost_or_broadened_hook_is_rejected_by_actual_consumer_contract(self):
        self.enable()
        workflow = normalized(installer.release(self.policy))

        def reject(change):
            altered = copy.deepcopy(workflow)
            change(altered)
            with self.assertRaises(ValueError):
                contracts.validate_automatic_push_admission(
                    self.policy, {"release-pipeline.yml": altered}
                )

        reject(lambda w: w["jobs"]["prepare"]["steps"].pop())
        reject(lambda w: w["jobs"]["scope"]["outputs"].update(run="false"))
        reject(lambda w: w["jobs"]["prepare"].update({"if": "${{ always() }}"}))
        reject(
            lambda w: next(
                s for s in w["jobs"]["scope"]["steps"] if s.get("id") == "scope"
            )["env"].update(FORCE_FULL="false")
        )
        reject(
            lambda w: w["jobs"]["prepare"]["steps"][-1].update(
                {"if": "${{ always() }}"}
            )
        )
        reject(
            lambda w: next(
                s for s in w["jobs"]["prepare"]["steps"] if s.get("id") == "metadata"
            ).update(run="echo bypass-prepare")
        )
        undeclared = copy.deepcopy(self.policy)
        del undeclared["change_scope"]["automatic_release_test_paths"]
        with self.assertRaises(ValueError):
            contracts.validate_automatic_push_admission(
                undeclared, {"release-pipeline.yml": workflow}
            )
        with self.assertRaises(ValueError):
            contracts.validate_automatic_push_admission(
                self.policy, {"quality-gate.yml": workflow}
            )

    def test_documentation_scope_migration_rejects_unsupported_topology_without_writes(
        self,
    ):
        self.enable()
        self.fixture.render()
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        with self.assertRaises(ValueError):
            installer.render_change_scope(self.root)
        self.assertEqual(
            before,
            {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )

    def test_narrow_adoption_preserves_publication_jobs_and_is_idempotent(self):
        self.fixture.render()
        pipeline = self.root / ".github/workflows/release-pipeline.yml"
        workflow = yaml.safe_load(pipeline.read_text())
        workflow["jobs"]["candidate"]["steps"].append(
            {"run": "echo keep-consumer-publication-guard"}
        )
        pipeline.write_text(installer.dump(workflow))
        self.enable()
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        files = installer.render_release_push_admission(self.root)
        actual = yaml.safe_load(files[".github/workflows/release-pipeline.yml"])
        actual["jobs"]["prepare"]["steps"].pop()
        self.assertEqual(actual, workflow)
        self.assertEqual(
            set(files),
            {
                ".github/workflows/release-pipeline.yml",
                "scripts/release_push_scope.py",
                "scripts/release_versioned.py",
                ".github/release-tests/test_release_push_prepare.py",
                "scripts/workflow_contracts.py",
                "docs/automatic-release-test-paths.md",
                ".github/release-tests/test_release_push_scope.py",
            },
        )
        for name, content in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        self.assertEqual(installer.render_release_push_admission(self.root), files)
        contracts.validate(self.root)

    def test_narrow_adoption_rejects_unknown_scope_or_missing_marker_without_writes(
        self,
    ):
        self.fixture.render()
        self.enable()
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        pipeline = self.root / ".github/workflows/release-pipeline.yml"
        original = pipeline.read_text()
        cases = [
            original.replace("# Generated by", "# Changed by", 1),
            original.replace("FORCE_FULL: 'true'", "FORCE_FULL: 'false'"),
            original.replace(
                "args=()", "echo unknown-scope-command\n          args=()"
            ),
        ]
        for source in cases:
            with self.subTest(source=source[:80]):
                self.assertNotEqual(source, original)
                pipeline.write_text(source)
                before = {
                    str(p.relative_to(self.root)): p.read_bytes()
                    for p in self.root.rglob("*")
                    if p.is_file()
                }
                with self.assertRaises(ValueError):
                    installer.render_release_push_admission(self.root)
                self.assertEqual(
                    before,
                    {
                        str(p.relative_to(self.root)): p.read_bytes()
                        for p in self.root.rglob("*")
                        if p.is_file()
                    },
                )

    def test_narrow_runtime_migration_rejects_unknown_functions_and_preserves_publishers(
        self,
    ):
        import ast

        self.fixture.render()
        self.enable()
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        runtime = self.root / "scripts/release_versioned.py"
        before = runtime.read_text()

        def functions(source):
            return {
                n.name: ast.dump(n, include_attributes=False)
                for n in ast.parse(source).body
                if isinstance(n, ast.FunctionDef)
            }

        files = installer.render_release_push_admission(self.root)
        original, actual = (
            functions(before),
            functions(files["scripts/release_versioned.py"]),
        )
        for name in original:
            if name not in {"prepare", "scheduled_reuse", "automatic_test_push"}:
                self.assertEqual(original[name], actual[name], name)
        runtime.write_text(
            before.replace("inputs = event_inputs()", 'inputs = {"unknown": True}', 1)
        )
        with self.assertRaises(ValueError):
            installer.render_release_push_admission(self.root)

    def test_old_runtime_decorators_are_rejected_before_replacement(self):
        import ast

        source = (ROOT / "scripts/release_versioned.py").read_text()
        helper = next(
            n
            for n in ast.parse(source).body
            if isinstance(n, ast.FunctionDef) and n.name == "automatic_test_push"
        )
        legacy = source.replace(ast.get_source_segment(source, helper), "")
        legacy = legacy.replace("    *, source_sha: str | None = None,\n", "")
        legacy = legacy.replace(
            '    """Prove a publication while guarding the current run against main movement."""\n'
            '    source_sha = source_sha or cast(str, run["head_sha"])',
            '    """Find proved same-input publication after fresh scheduled checks and builds."""',
        )
        legacy = legacy.replace(
            'record["plan"]["source_sha"] == source_sha',
            'record["plan"]["source_sha"] == cast(str, run["head_sha"])',
        )
        legacy = legacy.replace(
            "    if admitted := automatic_test_push(gh, info, run, snapshot, base):\n"
            "        return admitted\n",
            "",
        )
        # This exact old preimage is supported; only the added wrapper makes it unknown.
        installer.migrate_push_runtime(legacy)
        for name in ("prepare", "scheduled_reuse"):
            decorated = legacy.replace(
                "def " + name + "(", "@consumer_wrapper\ndef " + name + "(", 1
            )
            with (
                self.subTest(function=name),
                self.assertRaisesRegex(ValueError, "decorated"),
            ):
                installer.migrate_push_runtime(decorated)

    def test_fuzz_refresh_preserves_existing_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                ".github/fuzz.json",
                ".release-policy.json",
                ".github/requirements-fuzz.txt",
                ".github/workflows/quality-gate.yml",
                ".github/workflows/release-pipeline.yml",
            ):
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((ROOT / name).read_bytes())
            policy = json.loads((root / ".release-policy.json").read_text())
            policy.setdefault("change_scope", {})["automatic_release_test_paths"] = (
                PATHS
            )
            (root / ".release-policy.json").write_text(json.dumps(policy))
            pipeline = root / ".github/workflows/release-pipeline.yml"
            workflow = yaml.safe_load(pipeline.read_text())
            installer.automatic_push_admission(policy, workflow["jobs"]["prepare"])
            pipeline.write_text(installer.dump(workflow))
            scope = normalized(workflow["jobs"]["prepare"])
            files = installer.render_fuzz(root)
            actual = yaml.load(
                files[".github/workflows/release-pipeline.yml"], Loader=yaml.BaseLoader
            )
            self.assertEqual(actual["jobs"]["prepare"], scope)

    def test_attempt_receipt_inventory_migrations_preserve_admission(self):
        from test_release_attempt_migration import ReleaseAttemptMigrationTests
        from test_release_receipt_migration import MigrationTests
        from test_release_inventory_migration import InventoryMigrationTests

        self.enable()
        scope = installer.release(self.policy)["jobs"]["scope"]
        fixture = ReleaseAttemptMigrationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.pipeline["jobs"]["scope"] = copy.deepcopy(scope)
        fixture.pipeline["jobs"]["prepare"]["steps"] = []
        installer.automatic_push_admission(
            self.policy, fixture.pipeline["jobs"]["prepare"]
        )
        fixture.policy["change_scope"] = {"automatic_release_test_paths": PATHS}
        fixture.write()
        expected = yaml.safe_load(
            (fixture.root / ".github/workflows/release-pipeline.yml").read_text()
        )["jobs"]["prepare"]
        files = fixture.render()
        candidate = files.get(
            ".github/workflows/release-pipeline.yml",
            (fixture.root / ".github/workflows/release-pipeline.yml").read_text(),
        )
        self.assertEqual(yaml.safe_load(candidate)["jobs"]["prepare"], expected)
        for case, render in (
            (MigrationTests, installer.render_release_receipts),
            (InventoryMigrationTests, installer.render_release_inventory),
        ):
            fixture = case()
            fixture.setUp()
            try:
                fixture.policy["change_scope"] = {"automatic_release_test_paths": PATHS}
                fixture.write_policy()
                pipeline = fixture.consumer / ".github/workflows/release-pipeline.yml"
                pipeline.parent.mkdir(parents=True)
                pipeline.write_text(installer.dump({"jobs": {"scope": scope}}))
                before = pipeline.read_bytes()
                self.assertNotIn(
                    ".github/workflows/release-pipeline.yml", render(fixture.consumer)
                )
                self.assertEqual(pipeline.read_bytes(), before)
            finally:
                fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
