"""RC cost boundary, target execution evidence and promotion rejection contracts."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import install_release as installer  # noqa: E402
import release_fuzz as fuzz  # noqa: E402
import release_control as control  # noqa: E402
import release_versioned as lifecycle  # noqa: E402
import version_plan  # noqa: E402
import version_receipt  # noqa: E402


def policy():
    return {"mode": "release", "repository": "owner/repo", "visibility": "public",
            "versioning": {"example": True}, "fuzzing": {"toolkit_ref": "a" * 40}}


def config():
    return {"schema": 1, "language": "go", "setup": [["go", "mod", "download"]],
            "targets": [{"name": "parse", "engine": "go", "command": ["go", "test", "-fuzz=^FuzzParse$"], "seconds": 60}]}


class FuzzContracts(unittest.TestCase):
    def test_jvm_workflow_provisions_java_only_for_jvm_manifests(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/fuzz.yml").read_text())
        steps = workflow["jobs"]["campaign"]["steps"]
        java = [step for step in steps if step.get("uses", "").startswith("actions/setup-java@")]
        self.assertEqual(len(java), 1)
        self.assertEqual(java[0]["if"], "${{ steps.config.outputs.language == 'jvm' }}")
        self.assertEqual(java[0]["uses"], "actions/setup-java@de7274f081f381c8f8158605e0321c36c376e2e6")
        self.assertEqual(java[0]["with"], {"distribution": "temurin", "java-version": "17"})
        self.assertLess(steps.index(java[0]), next(i for i, step in enumerate(steps)
                                                if step["name"] == "Build and run isolated bounded targets"))

    def test_jvm_target_uses_shared_libfuzzer_budget_and_execution_proof(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".github").mkdir()
            target = {"name": "parser", "engine": "libfuzzer", "seconds": 60,
                      "command": ["./jazzer", "--cp=build/target.jar", "--target_class=example.ParserFuzz"]}
            (root / fuzz.MANIFEST).write_text(json.dumps(
                {"schema": 1, "language": "jvm", "setup": [], "targets": [target]}))
            self.assertEqual(fuzz.manifest(root)["language"], "jvm")

            def executed(command, cwd, output, timeout, isolated):
                self.assertEqual(command[:3], target["command"])
                self.assertIn("-max_total_time=60", command)
                self.assertIn("-timeout=10", command)
                self.assertIn("-rss_limit_mb=2048", command)
                self.assertEqual(cwd, root)
                self.assertEqual(timeout, 360)
                self.assertTrue(isolated)
                output.write_text("Done 123 runs in 60 second(s)\n")
                return 0, 60.0

            with patch.object(fuzz, "execute", side_effect=executed):
                self.assertEqual(fuzz.run_target(root, root, target)["exit_code"], 0)

    def test_isolated_environment_retains_jdk_location_without_ambient_jvm_options(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            real_popen = subprocess.Popen

            def namespace_stub(command, **kwargs):
                self.assertIn("--net", command)
                self.assertIn(f"--setuid={os.getuid()}", command)
                return real_popen(command[command.index("/usr/bin/env"):], **kwargs)

            environment = {"JAVA_HOME": "/synthetic/jdk-17", "JAVA_TOOL_OPTIONS": "-Xmx99g",
                           "JDK_JAVA_OPTIONS": "--invalid-option", "GH_TOKEN": "synthetic-test-value"}
            code = "import json,os; print(json.dumps({k:os.environ.get(k) for k in " + repr(list(environment)) + "}))"
            output = root / "run.log"
            with patch.dict(os.environ, environment), patch.object(fuzz.subprocess, "Popen", side_effect=namespace_stub):
                result, _ = fuzz.execute([sys.executable, "-c", code], root, output, 20, True)
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.read_text()), {
                "JAVA_HOME": "/synthetic/jdk-17", "JAVA_TOOL_OPTIONS": None,
                "JDK_JAVA_OPTIONS": None, "GH_TOKEN": None})

    def test_isolated_command_preserves_real_virtual_environment_entrypoint(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            environment = root / ".fuzz-venv"
            venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
            real_popen = subprocess.Popen
            commands = []

            def namespace_stub(command, **kwargs):
                commands.append(command)
                # Exercise the real env/interpreter subprocess without requiring
                # privileged Linux namespaces on a developer's macOS machine.
                return real_popen(command[command.index("/usr/bin/env"):], **kwargs)

            output = root / "run.log"
            with patch.object(fuzz.subprocess, "Popen", side_effect=namespace_stub):
                code, _ = fuzz.execute([".fuzz-venv/bin/python", "-c", "import sys; print(sys.prefix)"], root, output, 20, True)
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text().strip(), str(environment))
            self.assertIn("--net", commands[0])
            self.assertIn(f"--setuid={os.getuid()}", commands[0])

    def test_fuzz_receipt_is_verified_but_not_a_final_platform_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "version").write_text("1.2.3\n")
            project = {"repository": "owner/repo", "mode": "release", "version_file": "version",
                       "versioning": {"schema": 1, "promotion": "final-build",
                                      "files": [{"path": "version", "format": "text", "value": "package"}]}}
            plan = version_plan.create_plan("1.2.3", "rc", 1, "a" * 40, project, 51)
            files = version_plan.sync_versions(root, project, plan)
            evidence = {"source_sha": plan["source_sha"], "plan_sha256": version_plan.plan_digest(plan),
                        "effective_inputs_sha256": version_plan.effective_inputs_digest(files), "files": files}
            (root / ".release-plan.json").write_text(json.dumps(plan))
            (root / ".release-inputs.json").write_text(json.dumps(evidence))
            combined = root / "combined"
            combined.mkdir()
            for target, payload in (("linux", "app.bin"), ("fuzz", fuzz.REPORT)):
                folder = root / target
                folder.mkdir()
                (folder / payload).write_text(target)
                with patch.object(version_receipt, "capture_toolchain", return_value={"python": target}):
                    version_receipt.create_receipt(root / ".release-plan.json", root / ".release-inputs.json",
                                                   folder, folder / f"release-inputs-{target}.json")
                for path in folder.iterdir():
                    (combined / path.name).write_bytes(path.read_bytes())
            inventory = [{"name": path.name, "size": path.stat().st_size,
                          "sha256": version_receipt.sha256(path.read_bytes())} for path in combined.iterdir()]
            receipts = version_receipt.verify_receipts(combined, plan, inventory, project)
            builds, validations = fuzz.split_receipts(receipts)
            self.assertEqual([r["name"] for r in builds], ["release-inputs-linux.json"])
            self.assertEqual([r["name"] for r in validations], ["release-inputs-fuzz.json"])
            candidate = {"tag": "v1.2.3-rc.1", "build_receipts": builds}
            raw = (combined / builds[0]["name"]).read_bytes()
            from unittest.mock import Mock
            gh = Mock()
            gh.binary.return_value = raw
            with patch.object(lifecycle.rc, "release_snapshot", return_value=({}, {}, [{"name": builds[0]["name"], "id": 1}])):
                lifecycle.verify_final_toolchains(gh, candidate, builds)
                with self.assertRaisesRegex(lifecycle.rc.ReleaseError, "platform receipt inventory"):
                    lifecycle.verify_final_toolchains(gh, candidate, receipts)
            bad = copy.deepcopy(receipts)
            next(r for r in bad if r["name"] == "release-inputs-fuzz.json")["inputs"]["artifacts"].append({"name": "app.bin"})
            with self.assertRaises(ValueError):
                fuzz.split_receipts(bad)

    def test_adoption_requires_a_new_fuzzed_candidate(self):
        with patch.object(control, "checked_out_sha", return_value="a" * 40), patch.object(control, "source_policy_snapshot", return_value={"data": policy()}):
            with self.assertRaisesRegex(control.ReleaseError, "create a new RC"):
                control.require_current_fuzzing(None, {}, "a" * 40)
            control.require_current_fuzzing(None, policy(), "a" * 40)

    def test_only_public_default_branch_rc_dispatch_is_accepted(self):
        event = {"inputs": {"channel": "rc"}, "repository": {"private": False, "full_name": "owner/repo", "default_branch": "main"}}
        fuzz.event_contract(event, "workflow_dispatch", "refs/heads/main", "owner/repo", "a" * 40)
        for name in ("push", "schedule", "pull_request", "pull_request_target", "workflow_run"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fuzz.event_contract(event, name, "refs/heads/main", "owner/repo", "a" * 40)
        for channel in ("beta", "nightly", "stable", None):
            bad = copy.deepcopy(event)
            bad["inputs"]["channel"] = channel
            with self.subTest(channel=channel), self.assertRaises(ValueError):
                fuzz.event_contract(bad, "workflow_dispatch", "refs/heads/main", "owner/repo", "a" * 40)
        for private in (True, None, "false", 0):
            bad = copy.deepcopy(event)
            bad["repository"]["private"] = private
            with self.subTest(private=private), self.assertRaises(ValueError):
                fuzz.event_contract(bad, "workflow_dispatch", "refs/heads/main", "owner/repo", "a" * 40)
        with self.assertRaises(ValueError):
            fuzz.event_contract(event, "workflow_dispatch", "refs/heads/feature", "owner/repo", "a" * 40)

    def test_policy_rejects_private_unpinned_and_nonrelease(self):
        self.assertIsNone(fuzz.configuration({}))
        fuzz.configuration(policy())
        for key, value in (("visibility", "private"), ("mode", "validation-only"), ("ci_execution", "local"), ("versioning", None), ("fuzzing", {"toolkit_ref": "main"})):
            with self.subTest(key=key), self.assertRaises(ValueError):
                fuzz.configuration({**policy(), key: value})

    def test_manifest_rejects_escape_unbounded_commands_and_duplicate_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".github").mkdir()
            target = root / fuzz.MANIFEST
            target.write_text(json.dumps(config()))
            fuzz.manifest(root)
            invalid = []
            for key, value in (("seconds", 0), ("seconds", True), ("seconds", 121), ("cwd", "../outside"), ("command", "sh -c true"), ("command", ["go", "test", "-fuzztime=0"])):
                bad = config()
                bad["targets"][0][key] = value
                invalid.append(bad)
            bad = config()
            bad["targets"] *= 2
            invalid.append(bad)
            bad = config()
            bad["targets"] = [{**bad["targets"][0], "name": f"target-{i}", "seconds": 120} for i in range(6)]
            invalid.append(bad)
            for bad in invalid:
                target.write_text(json.dumps(bad))
                with self.subTest(config=bad), self.assertRaises(ValueError):
                    fuzz.manifest(root)
            (root / "escape").symlink_to(root.parent)
            bad = config()
            bad["targets"][0]["cwd"] = "escape"
            target.write_text(json.dumps(bad))
            with self.assertRaises(ValueError):
                fuzz.manifest(root)

    def test_zero_work_success_is_not_fuzzing_evidence(self):
        self.assertFalse(fuzz.exercised("go", "PASS: no fuzz tests to fuzz"))
        self.assertFalse(fuzz.exercised("go", "fuzz: elapsed: 0s, execs: 0"))
        self.assertTrue(fuzz.exercised("go", "fuzz: elapsed: 3s, execs: 1234 (411/sec), new interesting: 2"))
        self.assertTrue(fuzz.exercised("atheris", "Done 49210 runs in 61 second(s)"))
        self.assertFalse(fuzz.exercised("libfuzzer", "Done 0 runs in 0 second(s)"))

    def test_run_proof_rejects_missing_wrong_source_duplicate_or_skipped_campaign(self):
        job = {"name": fuzz.JOB, "head_sha": "a" * 40, "status": "completed", "conclusion": "success"}
        fuzz.check_run_jobs([job], "rc", "a" * 40)
        for jobs in ([], [job, job], [{**job, "head_sha": "b" * 40}], [{**job, "conclusion": "skipped"}], [{**job, "status": "in_progress"}]):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                fuzz.check_run_jobs(jobs, "rc", "a" * 40)
        for channel in ("beta", "stable", "nightly"):
            fuzz.check_run_jobs([], channel, "a" * 40)
            with self.assertRaises(ValueError):
                fuzz.check_run_jobs([job], channel, "a" * 40)

    def test_real_generated_gate_rejects_skipped_rc_but_accepts_skipped_beta(self):
        gate = {"needs": ["prepare", "checks", "build"], "steps": [{"env": {}, "run":
                "python3 - <<'PY'\nimport json, os\nresults=json.loads(os.environ['RESULTS'])\n"
                "expected=dict.fromkeys(('prepare','checks','build'),'success')\n"
                "failed = {name: results.get(name, {}).get('result') for name, result in expected.items() if results.get(name, {}).get('result') != result}\n"
                "if failed: raise SystemExit(str(failed))\nPY\n"}]}
        workflow = {"jobs": {"gate": gate}}
        installer.configure_fuzzing(workflow, policy())
        condition = workflow["jobs"]["fuzz"]["if"]
        for required in ("repository.private == false", "workflow_dispatch", "channel == 'rc'", "default_branch"):
            self.assertIn(required, condition)
        self.assertNotIn("secrets", workflow["jobs"]["fuzz"])
        for required, outcome, code in (("true", "success", 0), ("true", "skipped", 1), ("true", "failure", 1), ("false", "skipped", 0), ("false", "success", 1)):
            rows = {key: {"result": "success"} for key in ("prepare", "checks", "build")}
            rows["fuzz"] = {"result": outcome}
            result = subprocess.run(["bash", "-c", gate["steps"][0]["run"]], capture_output=True, env={**os.environ, "RESULTS": json.dumps(rows), "FUZZ_REQUIRED": required})
            self.assertEqual(result.returncode, code, result.stderr)

    def test_report_binds_every_target_manifest_source_and_attempt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".github").mkdir()
            (root / fuzz.MANIFEST).write_text(json.dumps(config()))
            report = {"schema": 1, "repository": "owner/repo", "source_sha": "a" * 40, "run_id": 42, "run_attempt": 2,
                      "channel": "rc", "private": False, "manifest_sha256": hashlib.sha256((root / fuzz.MANIFEST).read_bytes()).hexdigest(),
                      "status": "passed", "network": "isolated", "targets": [{"name": "parse", "seconds": 60, "exit_code": 0}]}
            path = root / fuzz.REPORT
            path.write_text(json.dumps(report))
            fuzz.verify_report(root, root, "owner/repo", "a" * 40, 42, 2)
            for key, value in (("source_sha", "b" * 40), ("run_attempt", 1), ("targets", []), ("targets", [{"name": "parse", "seconds": 60, "exit_code": False}]), ("status", "failed"), ("private", 0)):
                path.write_text(json.dumps({**report, key: value}))
                with self.subTest(key=key), self.assertRaises(ValueError):
                    fuzz.verify_report(root, root, "owner/repo", "a" * 40, 42, 2)


if __name__ == "__main__":
    unittest.main()
