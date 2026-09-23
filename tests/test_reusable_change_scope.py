"""Verify reusable scope delivery and execute its generated shell in a consumer."""

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scope_renderer", ROOT / "scripts/render_change_scope.py"
)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)
WORKFLOWS = (
    "python-ci.yml",
    "go-ci.yml",
    "rust-ci.yml",
    "typescript-ci.yml",
    "terraform-ci.yml",
    "security-scan.yml",
)
GUARD = (
    "needs.change-scope.outputs.run != 'false' || "
    "needs.change-scope.outputs.reason != 'documentation-only'"
)


def workflow(name):
    """BaseLoader preserves on/booleans as workflow syntax strings."""
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


class ReusableScopeContracts(unittest.TestCase):
    """Guard generated source, caller-safe defaults and optional job conditions."""

    def test_workflow_contains_exact_canonical_source(self):
        """Reject drift and ensure execution never downloads mutable toolkit code."""
        source = renderer.SOURCE.read_text()
        self.assertEqual(renderer.TARGET.read_text(), renderer.render(source))
        command = workflow("change-scope.yml")["jobs"]["scope"]["steps"][-1]["run"]
        embedded = command.split("<<'" + renderer.DELIMITER + "'\n", 1)[1]
        self.assertEqual(embedded, source + renderer.DELIMITER + "\n")
        self.assertNotIn("github.workflow_sha", command)
        self.assertNotIn("curl ", command)

    def test_scope_fetches_consumer_history_and_exposes_both_outputs(self):
        """Make complete history and both scope outputs part of the public contract."""
        data = workflow("change-scope.yml")
        call = data["on"]["workflow_call"]
        self.assertEqual(call["inputs"]["force-full"]["default"], "false")
        self.assertEqual(set(call["outputs"]), {"run", "reason"})
        job = data["jobs"]["scope"]
        self.assertEqual(job["timeout-minutes"], "5")
        self.assertEqual(job["outputs"]["run"], "${{ steps.classify.outputs.run }}")
        self.assertEqual(
            job["outputs"]["reason"], "${{ steps.classify.outputs.reason }}"
        )
        checkout = job["steps"][0]
        self.assertEqual(checkout["if"], "${{ !inputs.force-full }}")
        self.assertEqual(
            checkout["with"], {"fetch-depth": "0", "persist-credentials": "false"}
        )
        self.assertNotIn("repository", checkout["with"])

    def test_language_and_security_defaults_preserve_full_release_validation(self):
        """Keep old callers full while allowing ordinary CI to opt into scope."""
        for name in WORKFLOWS:
            with self.subTest(workflow=name):
                data = workflow(name)
                inputs = data["on"]["workflow_call"]["inputs"]
                self.assertEqual(inputs["force-full"]["default"], "true")
                self.assertEqual(inputs["documentation-paths"]["default"], "[]")
                self.assertEqual(inputs["required-paths"]["default"], "[]")
                scope = data["jobs"]["change-scope"]
                self.assertEqual(scope["uses"], "./.github/workflows/change-scope.yml")
                for field in ("force-full", "documentation-paths", "required-paths"):
                    self.assertEqual(
                        scope["with"][field], "${{ inputs." + field + " }}"
                    )
                for key, job in data["jobs"].items():
                    if key == "change-scope":
                        continue
                    needs = job["needs"]
                    self.assertIn(
                        "change-scope", [needs] if isinstance(needs, str) else needs
                    )
                    self.assertIn(GUARD, job["if"])

    def test_existing_optional_and_security_conditions_remain_required(self):
        """Retain frontend dependencies, security visibility and retired failures."""
        frontend = workflow("rust-ci.yml")["jobs"]["frontend"]
        self.assertEqual(frontend["needs"], ["change-scope", "rust-ci"])
        self.assertEqual(
            frontend["if"], "${{ inputs.enable-frontend && (" + GUARD + ") }}"
        )
        original = {
            "codeql": "inputs.run-codeql && github.event.repository.private == false",
            "trivy": "inputs.run-trivy || github.event.repository.private == true",
            "dependency-review": (
                "inputs.run-dependency-review && github.event.repository.private == false "
                "&& github.event_name == 'pull_request'"
            ),
        }
        jobs = workflow("security-scan.yml")["jobs"]
        for name, condition in original.items():
            self.assertEqual(
                jobs[name]["if"], "${{ (" + condition + ") && (" + GUARD + ") }}"
            )
        # Retired publishing entrypoints must still fail even for a docs-only PR.
        retired = workflow("docker-build.yml")["jobs"]
        self.assertEqual(set(retired), {"migration-required"})
        self.assertIn("exit 1", retired["migration-required"]["steps"][0]["run"])

    def test_missing_or_inconsistent_scope_outputs_cannot_skip(self):
        """Only a consistent documentation-only decision may suppress work."""
        expression = workflow("python-ci.yml")["jobs"]["ci"]["if"]
        self.assertEqual(expression, "${{ " + GUARD + " }}")
        for run, reason, expected in (
            ("false", "documentation-only", False),
            ("true", "documentation-only", True),
            ("false", "git-error", True),
            ("", "documentation-only", True),
            ("false", "", True),
            ("", "", True),
        ):
            with self.subTest(run=run, reason=reason):
                condition = GUARD.replace("needs.change-scope.outputs.run", repr(run))
                condition = condition.replace(
                    "needs.change-scope.outputs.reason", repr(reason)
                )
                # This expression is a repository-owned workflow constant.
                # pylint: disable-next=eval-used
                actual = eval(condition.replace("||", "or"), {"__builtins__": {}})
                self.assertEqual(actual, expected)


class ReusableScopeExecution(unittest.TestCase):
    """Run the actual generated shell without toolkit files in the consumer."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()  # pylint: disable=consider-using-with
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.repo = self.directory / "consumer"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "scope@example.invalid")
        self.git("config", "user.name", "Scope fixture")
        (self.repo / "README.md").write_text("before\n")
        (self.repo / "scripts").mkdir()
        self.marker = self.directory / "consumer-script-executed"
        (self.repo / "scripts/change_scope.py").write_text(
            f"from pathlib import Path\nPath({str(self.marker)!r}).touch()\nraise SystemExit(42)\n"
        )
        self.base = self.commit()
        (self.repo / "README.md").write_text("after\n")
        self.head = self.commit()
        self.event = self.directory / "event.json"
        self.event.write_text(json.dumps({"before": self.base, "after": self.head}))
        self.command = workflow("change-scope.yml")["jobs"]["scope"]["steps"][-1]["run"]

    def git(self, *args):
        """Run Git inside the temporary consumer without changing toolkit source."""
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self):
        """Commit fixture data without user hooks or signing configuration."""
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

    def classify(self, **overrides):
        """Execute the generated shell and require matching JSON/job outputs."""
        output = self.directory / "github-output"
        output.unlink(missing_ok=True)
        env = dict(
            os.environ,
            GITHUB_WORKSPACE=str(self.repo),
            GITHUB_EVENT_PATH=str(self.event),
            GITHUB_EVENT_NAME="push",
            GITHUB_OUTPUT=str(output),
            FORCE_FULL="false",
            DOCUMENTATION_PATHS="[]",
            REQUIRED_PATHS="[]",
        )
        env.update(overrides)
        result = subprocess.run(
            ["bash", "-e", "-c", self.command],
            cwd=self.repo,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        value = json.loads(result.stdout)
        self.assertEqual(
            output.read_text(),
            f"run={str(value['run']).lower()}\nreason={value['reason']}\n",
        )
        self.assertFalse(self.marker.exists())
        return value

    def test_docs_only_uses_pinned_inline_source_not_consumer_script(self):
        """Ignore even an executable-looking classifier supplied by the consumer."""
        self.assertEqual(
            self.classify(), {"run": False, "reason": "documentation-only"}
        )

    def test_code_and_required_documentation_request_full_validation(self):
        """Code and explicitly required documents override the docs optimization."""
        self.assertTrue(self.classify(REQUIRED_PATHS='["README.md"]')["run"])
        (self.repo / "application.py").write_text("print('changed')\n")
        self.event.write_text(json.dumps({"before": self.base, "after": self.commit()}))
        self.assertTrue(self.classify()["run"])

    def test_manual_and_forced_calls_do_not_read_diff(self):
        """Manual or release qualification remains full even without Git inputs."""
        self.assertTrue(self.classify(GITHUB_EVENT_NAME="workflow_dispatch")["run"])
        self.assertEqual(
            self.classify(
                FORCE_FULL="true",
                GITHUB_EVENT_PATH="missing",
                GITHUB_WORKSPACE="missing",
            ),
            {"run": True, "reason": "forced"},
        )

    def test_invalid_policy_stays_literal_and_requests_full_validation(self):
        """Treat untrusted JSON input as data, never as shell source."""
        payload = '["README.md"]; touch "' + str(self.marker) + '"'
        self.assertTrue(self.classify(DOCUMENTATION_PATHS=payload)["run"])
        self.assertTrue(self.classify(REQUIRED_PATHS="{}")["run"])


if __name__ == "__main__":
    unittest.main()
