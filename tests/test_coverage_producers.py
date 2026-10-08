"""Keep coverage export separate from collection, authentication and old callers."""

import copy
import json
import unittest
from pathlib import Path, PurePosixPath

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {"python-ci.yml": "coverage.xml", "go-ci.yml": "coverage.out"}
SCOPE = (
    "${{ needs.change-scope.outputs.run != 'false' || "
    "needs.change-scope.outputs.reason != 'documentation-only' }}"
)
EXPORT = "${{ inputs.run-tests && inputs.coverage-artifact-name != '' }}"
LEGACY = "${{ inputs.run-tests && inputs.coverage-artifact-name == '' }}"


def workflow(name):
    """Preserve GitHub's on key and input defaults rather than YAML 1.1 booleans."""
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def step(job, name):
    """Require exactly one step with the given identity."""
    matches = [item for item in job["steps"] if item.get("name") == name]
    if len(matches) != 1:
        raise AssertionError(f"Expected one {name} step, found {len(matches)}")
    return matches[0]


class CoverageProducerTests(unittest.TestCase):
    """Exercise opt-in dispatch and reject artifacts detached from passing tests."""

    def assert_export_contract(self, data, report):
        """Reject widening permissions, optional export, stale attempts and paths."""
        inputs = data["on"]["workflow_call"]["inputs"]
        self.assertEqual(inputs["coverage-artifact-name"]["default"], "")
        self.assertEqual(inputs["coverage-artifact-name"]["type"], "string")
        self.assertEqual(inputs["run-tests"]["default"], "true")
        job = data["jobs"]["ci"]
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertEqual(job["if"], SCOPE)
        self.assertEqual(job["needs"], "change-scope")
        self.assertNotIn("continue-on-error", job)
        export = step(job, "Export coverage report")
        legacy = step(job, "Upload coverage")
        tests = step(job, "Run Tests")
        self.assertEqual(export["if"], EXPORT)
        self.assertEqual(legacy["if"], LEGACY)
        self.assertEqual(tests["if"], "inputs.run-tests")
        self.assertNotIn("continue-on-error", tests)
        self.assertNotIn("continue-on-error", export)
        pins = json.loads((ROOT / ".github/action-pins.json").read_text())
        pin = next(p for p in pins if p["packageName"] == "actions/upload-artifact")
        self.assertEqual(export["uses"], "actions/upload-artifact@" + pin["digest"])
        self.assertEqual(
            export["with"],
            {
                "name": "${{ inputs.coverage-artifact-name }}-${{ github.run_attempt }}",
                "path": "${{ inputs.working-directory }}/" + report,
                "if-no-files-found": "error",
                "retention-days": "7",
            },
        )
        self.assertLess(job["steps"].index(tests), job["steps"].index(export))
        return job, export, legacy

    def test_both_producers_export_existing_attempt_bound_reports(self):
        for name, report in WORKFLOWS.items():
            with self.subTest(workflow=name):
                self.assert_export_contract(workflow(name), report)

    def test_empty_input_retains_legacy_upload_behavior_without_oidc(self):
        for name, report in WORKFLOWS.items():
            with self.subTest(workflow=name):
                data = workflow(name)
                _, _, legacy = self.assert_export_contract(data, report)
                self.assertEqual(legacy["with"]["files"], "./" + report)
                if name == "python-ci.yml":
                    self.assertEqual(legacy["continue-on-error"], "true")
                    self.assertEqual(legacy["with"]["fail_ci_if_error"], "true")
                else:
                    self.assertNotIn("continue-on-error", legacy)
                    self.assertEqual(legacy["with"]["fail_ci_if_error"], "false")
                self.assertNotIn("id-token", json.dumps(data))
                self.assertNotIn("use_oidc", json.dumps(data))

    def test_no_duplicate_test_execution_and_original_thresholds_remain(self):
        python = workflow("python-ci.yml")
        job = python["jobs"]["ci"]
        self.assertEqual(
            step(job, "Run Tests")["run"].strip(),
            "python -m pytest ${{ inputs.test-args }} --cov=. --cov-report=xml "
            "--cov-report=term-missing --cov-fail-under=${{ inputs.coverage-threshold }}",
        )
        self.assertEqual(
            python["on"]["workflow_call"]["inputs"]["coverage-threshold"]["default"],
            "80",
        )
        go = workflow("go-ci.yml")
        job = go["jobs"]["ci"]
        self.assertEqual(
            step(job, "Run Tests")["run"], "go test ${{ inputs.test-args }} ./..."
        )
        self.assertEqual(
            go["on"]["workflow_call"]["inputs"]["test-args"]["default"],
            "-race -coverprofile=coverage.out -covermode=atomic",
        )
        self.assertEqual(
            go["on"]["workflow_call"]["inputs"]["coverage-threshold"]["default"], "70"
        )
        threshold = step(job, "Check coverage threshold")
        self.assertEqual(threshold["if"], "inputs.run-tests")
        self.assertIn("coverage < ${{ inputs.coverage-threshold }}", threshold["run"])
        self.assertIn("exit 1", threshold["run"])
        self.assertNotIn("continue-on-error", threshold)
        self.assertLess(
            job["steps"].index(threshold),
            job["steps"].index(step(job, "Export coverage report")),
        )

    def test_subdirectory_paths_do_not_rely_on_run_step_defaults(self):
        for name, report in WORKFLOWS.items():
            _, export, _ = self.assert_export_contract(workflow(name), report)
            for directory in (".", "backend", "components/mqtt-interceptor"):
                with self.subTest(workflow=name, directory=directory):
                    rendered = export["with"]["path"].replace(
                        "${{ inputs.working-directory }}", directory
                    )
                    self.assertEqual(
                        PurePosixPath(rendered), PurePosixPath(directory) / report
                    )

    def test_opt_in_paths_are_exclusive_and_disabled_tests_or_docs_export_nothing(self):
        for name, report in WORKFLOWS.items():
            job, export, legacy = self.assert_export_contract(workflow(name), report)
            for run_tests in (True, False):
                for artifact in ("", "coverage-python"):
                    for run, reason, expected_job in (
                        ("false", "documentation-only", False),
                        ("true", "full", True),
                        ("false", "git-error", True),
                        ("", "", True),
                    ):
                        values = {
                            "inputs.run-tests": run_tests,
                            "inputs.coverage-artifact-name": artifact,
                            "needs.change-scope.outputs.run": run,
                            "needs.change-scope.outputs.reason": reason,
                        }

                        def enabled(expression, bindings=values):
                            # Evaluate only the already-asserted repository-owned constants.
                            expression = expression.removeprefix("${{ ").removesuffix(
                                " }}"
                            )
                            for key, value in bindings.items():
                                expression = expression.replace(key, repr(value))
                            expression = expression.replace("&&", "and").replace(
                                "||", "or"
                            )
                            # pylint: disable-next=eval-used
                            return eval(expression, {"__builtins__": {}})

                        job_runs = enabled(job["if"])
                        self.assertEqual(job_runs, expected_job)
                        actual = [
                            job_runs and enabled(item["if"])
                            for item in (export, legacy)
                        ]
                        self.assertEqual(
                            actual,
                            [
                                expected_job and run_tests and bool(artifact),
                                expected_job and run_tests and not artifact,
                            ],
                        )
                        self.assertLessEqual(sum(actual), 1)

    def test_contract_rejects_optional_missing_stale_or_privileged_export(self):
        cases = {
            "optional": lambda j, e: e.update({"continue-on-error": "true"}),
            "missing-report": lambda j, e: e["with"].update(
                {"if-no-files-found": "warn"}
            ),
            "stale-attempt": lambda j, e: e["with"].update({"name": "coverage"}),
            "wrong-directory": lambda j, e: e["with"].update({"path": "coverage.xml"}),
            "oidc-producer": lambda j, e: j["permissions"].update(
                {"id-token": "write"}
            ),
            "upload-after-failure": lambda j, e: e.update({"if": "${{ always() }}"}),
        }
        for name, mutate in cases.items():
            with self.subTest(regression=name):
                data = copy.deepcopy(workflow("python-ci.yml"))
                job = data["jobs"]["ci"]
                mutate(job, step(job, "Export coverage report"))
                with self.assertRaises(AssertionError):
                    self.assert_export_contract(data, "coverage.xml")

    def test_plain_typescript_and_rust_tests_do_not_claim_report_generation(self):
        for name in ("typescript-ci.yml", "rust-ci.yml"):
            with self.subTest(workflow=name):
                data = workflow(name)
                self.assertNotIn(
                    "coverage-artifact-name", data["on"]["workflow_call"]["inputs"]
                )
                self.assertNotIn("Export coverage report", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
