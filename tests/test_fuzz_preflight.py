"""Cheap fuzz preflight, durable metrics and bounded corpus evidence."""
import json
import hashlib
import os
from pathlib import Path
import sys
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fuzz_python_preflight as python_preflight  # noqa: E402
import release_fuzz as fuzz  # noqa: E402
import install_release as installer  # noqa: E402


class FuzzPreflightTests(unittest.TestCase):
    def test_schema_two_requires_actual_work_and_declared_instrumentation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".github").mkdir()
            config = {"schema": 1, "language": "python", "setup": [], "targets": [{
                "name": "parse", "engine": "atheris", "command": ["python", "fuzz.py"], "seconds": 1,
                "instrumentation": [{"module": "owned", "functions": ["parse"]}]}]}
            source = root / fuzz.MANIFEST
            source.write_text(json.dumps(config))
            proof = {"target": "parse", "instrumentation": [{"module": "owned", "function": "parse"}]}
            report = {"schema": 2, "repository": "o/r", "source_sha": "a" * 40,
                "run_id": 1, "run_attempt": 1, "channel": "rc", "private": False, "status": "passed",
                "network": "isolated", "manifest_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "targets": [{"name": "parse", "seconds": 1, "exit_code": 0, "engine": "atheris", "executions": 100}],
                "preflight": {"python_targets": [proof], "commands": 0}}
            for count, proofs, accepted in ((100, [proof], True), (0, [proof], False),
                                            (True, [proof], False), (100, [], False)):
                report["targets"][0]["executions"] = count
                report["preflight"]["python_targets"] = proofs
                (root / fuzz.REPORT).write_text(json.dumps(report))
                if accepted:
                    fuzz.verify_report(root, root, "o/r", "a" * 40, 1, 1)
                else:
                    with self.assertRaises(ValueError):
                        fuzz.verify_report(root, root, "o/r", "a" * 40, 1, 1)
            report["schema"] = 1
            (root / fuzz.REPORT).write_text(json.dumps(report))
            with self.assertRaisesRegex(ValueError, "schema-2"):
                fuzz.verify_report(root, root, "o/r", "a" * 40, 1, 1)

    def test_targeted_migration_preserves_custom_workflow_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in (".github/fuzz.json", ".release-policy.json", ".github/requirements-fuzz.txt",
                         ".github/workflows/quality-gate.yml", ".github/workflows/release-pipeline.yml"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text((ROOT / name).read_text())
            source = root / ".github/workflows/release-pipeline.yml"
            source.write_text(source.read_text() + "# retain this reviewed consumer comment\n")
            first = installer.render_fuzz(root)
            self.assertIn("# retain this reviewed consumer comment", first[".github/workflows/release-pipeline.yml"])
            self.assertNotIn("scripts/release_control.py", first)
            for name, content in first.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            self.assertEqual(first, installer.render_fuzz(root))
            self.assertIn("parents[2]", first[".github/workflow-tests/test_workflow_yaml_contracts.py"])
            subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", ".github/workflow-tests"],
                           cwd=root, check=True, capture_output=True)
            policy_path = root / ".release-policy.json"
            policy = json.loads(policy_path.read_text())
            policy.pop("single_entry_ci")
            policy_path.write_text(json.dumps(policy))
            manifest_path = root / ".github/fuzz.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["language"] = "javascript"
            manifest["targets"] = [{"name": "parse", "engine": "libfuzzer", "seconds": 1, "command": ["jazzer"]}]
            manifest_path.write_text(json.dumps(manifest))
            native = installer.render_fuzz(root)
            self.assertIn(".github/workflows/release-pipeline.yml", native)
            self.assertNotIn(".github/workflows/quality-gate.yml", native)
            self.assertIn(installer.CONTRACT_REQUIREMENTS, native)

    def test_preflight_diff_rejects_arguments_at_the_process_boundary(self):
        import change_scope

        for revisions in (("--output=/tmp/unsafe", "b" * 40, ".."),
                          ("a" * 40, "--ext-diff", "..."),
                          ("a" * 40, "b" * 40, " -- ")):
            with patch.object(change_scope, "event_revisions", return_value=revisions), patch.object(
                    fuzz.subprocess, "check_output") as query:
                with self.assertRaises(ValueError):
                    fuzz.preflight_changed_paths(ROOT, "pull_request", {})
                query.assert_not_called()

    def test_corpus_retains_regular_inputs_with_hard_storage_caps(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "cache/fuzz/example/FuzzParse"
            source.mkdir(parents=True)
            (source / "a").write_bytes(b"first")
            (source / "b").write_bytes(b"second")
            (source / "external").symlink_to(root / "secret")
            (root / "secret").write_bytes(b"never copy")
            with patch.object(fuzz, "CORPUS_BYTES", 6):
                report = fuzz.retain_corpus(root / "cache/fuzz", root / "out")
            self.assertEqual(report, {"files": 1, "bytes": 5, "truncated": True})
            self.assertEqual((root / "out/example/FuzzParse/a").read_bytes(), b"first")
            self.assertFalse((root / "out/example/FuzzParse/external").exists())

    def test_go_target_retains_generated_cache_outside_testdata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            generated = root / "cache/fuzz/example/FuzzParse"
            generated.mkdir(parents=True)
            (generated / "interesting").write_text('go test fuzz v1\n[]byte("x")\n')
            def execute(_command, _cwd, output, _timeout, _isolated):
                output.write_text("fuzz: elapsed: 1s, execs: 23, new interesting: 1\n")
                return 0, 1.0
            with patch.object(fuzz, "execute", side_effect=execute), patch.object(
                    fuzz.subprocess, "check_output", return_value=str(root / "cache")):
                report = fuzz.run_target(root, root, {"name": "parse", "seconds": 1,
                    "engine": "go", "command": ["go", "test", "-fuzz=FuzzParse"]})
            self.assertEqual(report["executions"], 23)
            self.assertEqual(report["generated_corpus"]["files"], 1)
            self.assertTrue((root / "parse/go-generated-corpus/example/FuzzParse/interesting").is_file())

    def test_libfuzzer_metrics_use_completed_engine_work(self):
        result = fuzz.engine_metrics("atheris", "#123 DONE cov: 10 ft: 20 corp: 3/10b\nDone 123 runs in 1 second(s)")
        self.assertEqual(result, {"engine": "atheris", "executions": 123, "coverage_edges": 10, "coverage_features": 20})
        self.assertEqual(fuzz.engine_metrics("atheris", "no target")['executions'], 0)

    def test_instrumentation_requires_loaded_project_functions_and_idempotence(self):
        def owned():
            return 1
        declaration = [{"module": "audit_owned", "functions": ["owned"]}]
        with patch.dict(sys.modules, {"audit_owned": SimpleNamespace(owned=owned)}):
            good = SimpleNamespace(instrument_func=lambda function: function)
            self.assertEqual(len(python_preflight.verify_instrumentation(declaration, ROOT, good)), 1)
            def missing(function):
                function.__code__ = (lambda: 2).__code__
            with self.assertRaisesRegex(ValueError, "Missing production instrumentation"):
                python_preflight.verify_instrumentation(declaration, ROOT, SimpleNamespace(instrument_func=missing))
            with self.assertRaisesRegex(ValueError, "project-local"):
                python_preflight.verify_instrumentation(declaration, ROOT / "other", good)
        with self.assertRaisesRegex(ValueError, "not loaded"):
            python_preflight.verify_instrumentation(declaration, ROOT, good)

    def test_preflight_change_selection_is_public_scoped_and_fail_open_on_missing_diff(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            event = root / "event.json"
            event.write_text(json.dumps({"repository": {"private": False, "full_name": "o/r"},
                "pull_request": {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}}))
            with patch.dict(os.environ, {"GITHUB_EVENT_PATH": str(event), "GITHUB_EVENT_NAME": "pull_request", "GITHUB_REPOSITORY": "o/r"}):
                for paths, expected in ((b"docs/guide.md\0", False), (b"app/parser.py\0", True),
                                        (b".github/fuzz.json\0", True), (b"app2/parser.py\0", False)):
                    with patch.object(fuzz.subprocess, "check_output", return_value=paths):
                        self.assertEqual(fuzz.preflight_needed(root, {"preflight-paths": ["app"]}), expected)
                event.write_text(json.dumps({"repository": {"private": True, "full_name": "o/r"}}))
                with self.assertRaisesRegex(ValueError, "public repository"):
                    fuzz.preflight_needed(root, {})

    def test_nonpython_preflight_never_runs_campaign_setup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(fuzz, "execute") as execute:
                evidence = fuzz.run_preflight(root, root / "preflight", {"language": "rust", "setup": [["expensive-build"]]}, setup=True)
            execute.assert_not_called()
            self.assertEqual(evidence, {"python_targets": [], "commands": 0})


if __name__ == "__main__":
    unittest.main()
