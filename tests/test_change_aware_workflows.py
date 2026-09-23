"""Execute rendered scope/gate contracts without GitHub or project toolchains."""

# Keep each contract suite independent; their small isolated Git fixtures match.
# pylint: disable=duplicate-code

import ast
import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "change_aware_installer", ROOT / "scripts/install_release.py"
)
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


def policy(**overrides):
    """Model SWOP's required CodeQL plus normal release validation."""
    return {
        "repository": "example/change-aware",
        "mode": "release",
        "default_branch": "main",
        "visibility": "public",
        "single_entry_ci": True,
        "validation_workflows": ["ci.yml", "codeql.yml", "dependency-review.yml"],
        "change_scope": {"always_validate_workflows": ["codeql.yml"]},
    } | overrides


def rendered(data):
    """Exercise the serialized workflow, including literal generated scripts."""
    return yaml.safe_load(installer.dump(data))


def job_runs(job, needs, **context):
    """Evaluate only rendered job conditions; fail on unsupported expression syntax.

    This deliberately small harness models GitHub's implicit success() guard and
    missing output strings, not its whole expression engine or scope algorithm.
    """
    names = job.get("needs", [])
    names = [names] if isinstance(names, str) else names
    success = all(needs[name]["result"] == "success" for name in names)
    expression = (
        job.get("if", "${{ success() }}").removeprefix("${{").removesuffix("}}")
    )
    if (
        not re.search(r"\b(?:always|success|failure|cancelled)\(", expression)
        and not success
    ):
        return False

    def value(match):
        fields = match.group().split(".")
        current = {"needs": needs, **context}
        for field in fields:
            current = current.get(field, "") if isinstance(current, dict) else ""
        return repr(current)

    expression = re.sub(
        r"\b(?:needs|github|vars|inputs)(?:\.[\w-]+)+", value, expression
    )
    expression = expression.replace("always()", "True").replace(
        "success()", repr(success)
    )
    expression = expression.replace("&&", " and ").replace("||", " or ").strip()

    def evaluate(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BoolOp):
            values = [bool(evaluate(child)) for child in node.values]
            if isinstance(node.op, ast.And):
                return all(values)
            if isinstance(node.op, ast.Or):
                return any(values)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = evaluate(node.left), evaluate(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        raise AssertionError(f"Unsupported generated condition: {expression}")

    return bool(evaluate(ast.parse(expression, mode="eval").body))


class GeneratedGateExecution(unittest.TestCase):
    """A green required gate must prove the exact expected execution inventory."""

    def setUp(self):
        self.jobs = rendered(installer.quality(policy()))["jobs"]
        self.command = self.jobs["gate"]["steps"][0]["run"]

    def results(self, full=False):
        """Build the complete expected job inventory for full or docs-only CI."""
        always = {"scope", "check-1", "workflow-contracts"}
        values = {
            name: {
                "result": "success" if full or name in always else "skipped",
                "outputs": {},
            }
            for name in self.jobs
            if name != "gate"
        }
        values["scope"]["outputs"] = {
            "run": "true" if full else "false",
            "reason": "non-documentation-change" if full else "documentation-only",
        }
        return values

    def run_gate(self, values):
        """Execute the rendered gate with controlled GitHub needs metadata."""
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-c", self.command],
            env=dict(os.environ, RESULTS=json.dumps(values)),
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def assert_rejected(self, values):
        """Require the real gate script to fail for an invalid job result."""
        result = self.run_gate(values)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_docs_only_gate_executes_with_only_authorized_skips(self):
        """Accept only the exact skips justified by a successful docs-only scope."""
        result = self.run_gate(self.results())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Documentation-only", result.stdout)
        self.assertEqual(self.jobs["gate"]["name"], "CI gate")
        self.assertEqual(set(self.jobs["gate"]["needs"]), set(self.jobs) - {"gate"})

    def test_full_gate_executes_only_after_every_job_succeeded(self):
        """Reject every unsuccessful validator when full validation is required."""
        result = self.run_gate(self.results(full=True))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in self.results(full=True):
            for conclusion in ("skipped", "failure", "cancelled"):
                with self.subTest(job=name, conclusion=conclusion):
                    values = self.results(full=True)
                    values[name]["result"] = conclusion
                    self.assert_rejected(values)

    def test_missing_or_extra_job_cannot_turn_into_a_green_gate(self):
        """Reject an incomplete or unexpected validation job inventory."""
        for name in self.results():
            with self.subTest(missing=name):
                values = self.results()
                del values[name]
                self.assert_rejected(values)
        values = self.results()
        values["unexpected-validator"] = {"result": "success"}
        self.assert_rejected(values)
        self.assert_rejected({})

    def test_unsuccessful_scope_cannot_authorize_skips(self):
        """Require the scope job itself to succeed before trusting its outputs."""
        for conclusion in ("failure", "cancelled", "skipped", "neutral", ""):
            with self.subTest(conclusion=conclusion):
                values = self.results()
                values["scope"]["result"] = conclusion
                self.assert_rejected(values)

    def test_missing_and_malformed_outputs_do_not_authorize_docs_only(self):
        """Reject missing scope output fields and incorrectly typed boolean values."""
        for outputs in (
            {},
            {"run": "false"},
            {"reason": "documentation-only"},
            {"run": False, "reason": "documentation-only"},
            {"run": "FALSE", "reason": "documentation-only"},
            {"run": "", "reason": "documentation-only"},
        ):
            with self.subTest(outputs=outputs):
                values = self.results()
                values["scope"]["outputs"] = outputs
                self.assert_rejected(values)
        values = self.results()
        del values["scope"]["outputs"]
        self.assert_rejected(values)

    def test_false_with_error_or_forged_reason_is_rejected(self):
        """Only the exact documentation-only reason can authorize a skip."""
        for reason in (
            "git-error",
            "forced",
            "invalid-revisions",
            "",
            None,
            "documentation-only\nrun=true",
            "documentation-only ",
        ):
            with self.subTest(reason=reason):
                values = self.results()
                values["scope"]["outputs"]["reason"] = reason
                self.assert_rejected(values)

    def test_required_codeql_and_configuration_contracts_really_run(self):
        """Keep independently required validators active on documentation changes."""
        for name in ("check-1", "workflow-contracts"):
            for conclusion in ("skipped", "failure", "cancelled"):
                with self.subTest(job=name, conclusion=conclusion):
                    values = self.results()
                    values[name]["result"] = conclusion
                    self.assert_rejected(values)
        results = self.results()
        self.assertTrue(job_runs(self.jobs["check-1"], results))
        self.assertTrue(job_runs(self.jobs["workflow-contracts"], results))
        self.assertFalse(job_runs(self.jobs["check-0"], results))
        self.assertEqual(self.jobs["check-1"]["uses"], "./.github/workflows/codeql.yml")
        self.assertEqual(
            self.jobs["check-1"]["permissions"]["security-events"], "write"
        )

    def test_docs_exemption_does_not_hide_failed_optional_checks(self):
        """Require optional jobs to match the planned skip instead of hiding failures."""
        for name in ("check-0", "check-2", "release-contracts"):
            for conclusion in ("failure", "cancelled", "success"):
                with self.subTest(job=name, conclusion=conclusion):
                    values = self.results()
                    values[name]["result"] = conclusion
                    self.assert_rejected(values)

    def test_gate_still_runs_after_failed_or_cancelled_dependencies(self):
        """Evaluate the gate even when an upstream validator did not succeed."""
        for conclusion in ("failure", "cancelled", "skipped"):
            values = self.results(full=True)
            values["check-0"]["result"] = conclusion
            self.assertTrue(job_runs(self.jobs["gate"], values))

    def test_private_nightly_opt_in_and_manual_validation_are_preserved(self):
        """Preserve private nightly opt-in and full manual validation."""
        jobs = rendered(
            installer.quality(policy(mode="validation-only", visibility="private"))
        )["jobs"]
        values = {
            name: {"result": "success", "outputs": {}}
            for name in jobs
            if name != "gate"
        }
        values["scope"]["outputs"] = {"run": "true", "reason": "unsupported-event"}
        for event, enabled, expected in (
            ("schedule", "", False),
            ("schedule", "false", False),
            ("schedule", "true", True),
            ("workflow_dispatch", "", True),
        ):
            for name, job in jobs.items():
                with self.subTest(event=event, enabled=enabled, job=name):
                    self.assertEqual(
                        job_runs(
                            job,
                            values,
                            github={"event_name": event},
                            vars={"NIGHTLY_CHECKS_ENABLED": enabled},
                        ),
                        expected,
                    )
                    self.assertNotIn("security-events", job.get("permissions", {}))


class GeneratedReleaseScopeExecution(unittest.TestCase):
    """The real classifier decides whether allocation may start in either renderer."""

    def setUp(self):
        # enterContext registers cleanup even if a later setUp step fails.
        # pylint: disable-next=consider-using-with
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = self.root / "consumer"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy2(
            ROOT / "scripts/change_scope.py", self.repo / "scripts/change_scope.py"
        )
        self.git("init", "-q")
        self.git("config", "user.name", "Workflow fixture")
        self.git("config", "user.email", "workflow@example.invalid")
        (self.repo / "README.md").write_text("original documentation\n")
        self.base = self.commit()
        (self.repo / "README.md").write_text("corrected documentation\n")
        self.head = self.commit()
        self.event = self.root / "event.json"
        self.event.write_text(json.dumps({"before": self.base, "after": self.head}))

    def git(self, *args):
        """Read or update only the isolated fixture repository through Git."""
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self):
        """Commit fixture changes with signing and workstation hooks disabled."""
        self.git("add", ".")
        self.git(
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "fixture",
        )
        return self.git("rev-parse", "HEAD")

    @staticmethod
    def release(versioned):
        """Render either the legacy or versioned release orchestration."""
        config = policy()
        if versioned:
            config["versioning"] = {
                "schema": 1,
                "promotion": "promote-bytes",
                "files": [{"path": "VERSION", "format": "text", "value": "package"}],
                "artifacts": [],
            }
        return rendered(installer.release(config))

    def scope(self, job, event="push", force=False):
        """Execute the generated scope command with controlled event inputs."""
        step = next(step for step in job["steps"] if step.get("id") == "scope")
        output = self.root / "github-output"
        output.unlink(missing_ok=True)
        environment = dict(
            os.environ,
            GITHUB_EVENT_NAME=event,
            GITHUB_EVENT_PATH=str(self.event),
            GITHUB_OUTPUT=str(output),
            DOCUMENTATION_PATHS=step["env"]["DOCUMENTATION_PATHS"],
            REQUIRED_PATHS=step["env"]["REQUIRED_PATHS"],
            FORCE_FULL="true" if force else "false",
        )
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-e", "-c", step["run"]],
            cwd=self.repo,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return dict(line.split("=", 1) for line in output.read_text().splitlines())

    def test_docs_push_stops_before_legacy_resolution_or_versioned_allocation(self):
        """Prove that a docs push skips every job capable of allocating a version."""
        for versioned in (False, True):
            with self.subTest(versioned=versioned):
                jobs = self.release(versioned)["jobs"]
                self.assertEqual(next(iter(jobs)), "scope")
                self.assertEqual(jobs["prepare"]["needs"], "scope")
                values = {
                    "scope": {"result": "success", "outputs": self.scope(jobs["scope"])}
                }
                self.assertEqual(
                    values["scope"]["outputs"],
                    {"run": "false", "reason": "documentation-only"},
                )
                for name, job in jobs.items():
                    if name == "scope":
                        continue
                    self.assertFalse(
                        job_runs(
                            job, values, vars={"RELEASE_CHANNELS_ENABLED": "true"}
                        ),
                        name,
                    )
                    values[name] = {"result": "skipped", "outputs": {}}
                metadata = next(
                    step
                    for step in jobs["prepare"]["steps"]
                    if step.get("id") == "metadata"
                )
                self.assertIn(
                    "release_versioned.py prepare" if versioned else "'resolve'",
                    metadata["run"],
                )
                self.assertFalse((self.repo / ".release-plan.json").exists())

    def test_release_qualification_forces_real_checks_even_for_documentation_commit(
        self,
    ):
        """Require full validation when release qualification calls Quality gate."""
        for versioned in (False, True):
            with self.subTest(versioned=versioned):
                checks = self.release(versioned)["jobs"]["checks"]
                self.assertEqual(checks["uses"], "./.github/workflows/quality-gate.yml")
                self.assertIs(checks["with"]["force-full"], True)
        quality = rendered(installer.quality(policy()))
        call = quality["on"]["workflow_call"]["inputs"]["force-full"]
        self.assertEqual(call["type"], "boolean")
        self.assertEqual(
            self.scope(quality["jobs"]["scope"], force=True),
            {"run": "true", "reason": "forced"},
        )

    def test_manual_and_schedule_release_runs_still_reach_prepare(self):
        """Preserve explicit and scheduled release preparation in both renderers."""
        for versioned in (False, True):
            workflow = self.release(versioned)
            self.assertEqual(workflow["on"]["push"], {"branches": ["main"]})
            self.assertEqual(
                workflow["on"]["schedule"], [{"cron": installer.schedule(policy())}]
            )
            inputs = workflow["on"]["workflow_dispatch"]["inputs"]
            self.assertEqual(
                inputs["channel"]["options"], ["nightly", "beta", "rc", "stable"]
            )
            self.assertIn("expected_sha", inputs)
            self.assertIn("rc_tag", inputs)
            for event in ("schedule", "workflow_dispatch"):
                with self.subTest(versioned=versioned, event=event):
                    jobs = workflow["jobs"]
                    values = {
                        "scope": {
                            "result": "success",
                            "outputs": self.scope(jobs["scope"], event),
                        }
                    }
                    self.assertEqual(values["scope"]["outputs"]["run"], "true")
                    self.assertTrue(job_runs(jobs["prepare"], values))

    def test_scope_failure_never_reaches_version_preparation(self):
        """Block version preparation after an unsuccessful scope job."""
        for versioned in (False, True):
            jobs = self.release(versioned)["jobs"]
            for conclusion in ("failure", "cancelled", "skipped"):
                with self.subTest(versioned=versioned, conclusion=conclusion):
                    values = {
                        "scope": {"result": conclusion, "outputs": {"run": "true"}}
                    }
                    self.assertFalse(job_runs(jobs["prepare"], values))

    def test_required_documentation_override_reaches_prepare(self):
        """Treat documentation declared as a build input as a real release change."""
        config = policy(change_scope={"required_paths": ["README.md"]})
        jobs = rendered(installer.release(config))["jobs"]
        outputs = self.scope(jobs["scope"])
        self.assertEqual(outputs, {"run": "true", "reason": "required-path-change"})
        self.assertTrue(
            job_runs(
                jobs["prepare"], {"scope": {"result": "success", "outputs": outputs}}
            )
        )


if __name__ == "__main__":
    unittest.main()
