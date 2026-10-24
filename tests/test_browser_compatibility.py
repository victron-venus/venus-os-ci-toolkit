"""Execute the shipped browser admission/evidence scripts without network access."""

import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/typescript-ci.yml").read_text(), Loader=yaml.BaseLoader
)


def script(job, name):
    """Run the actual reusable-workflow body, not a second implementation."""
    step = next(step for step in WORKFLOW["jobs"][job]["steps"] if step.get("name") == name)
    return step["run"].split("node <<'NODE'\n", 1)[1].removesuffix("NODE\n")


TARGET = {
    "id": "chromium-current-linux", "os": "ubuntu-latest", "engine": "chromium",
    "playwright": "1.64.0", "browser_version": "156.0.8078.4",
    "toolchain": "tools/browser-current", "tier": "required",
}


class BrowserCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {
            **os.environ, "GITHUB_OUTPUT": str(self.root / "output"),
            "GITHUB_ENV": str(self.root / "env"), "RUNNER_TEMP": str(self.root),
            "BROWSER_MATRIX_FILE": "", "BROWSER_EXTENDED": "false",
            "BROWSER_PLATFORM_SCOPE": "all", "CI_HOSTED_RUNNERS_ENABLED": "",
            "BROWSER_RELEASE_CHANNEL": "",
            "BROWSER_EVIDENCE_PREFIX": "browser-compatibility",
            "BROWSER_COMMAND": "node scripts/application-acceptance.cjs",
        }

    def execute(self, job, name, **env):
        return subprocess.run(
            ["node", "-e", script(job, name)], cwd=self.root,
            env=self.env | env, capture_output=True, text=True, check=False,
        )

    def output(self, name="matrix"):
        return json.loads(dict(line.split("=", 1) for line in (self.root / "output").read_text().splitlines())[name])

    def plan(self, targets, **env):
        return self.execute(
            "browser-plan", "Validate and select the explicit compatibility inventory",
            BROWSER_MATRIX=json.dumps(targets), **env,
        )

    def test_required_subset_and_extended_versions_are_distinct(self):
        old = TARGET | {"id": "chromium-previous-windows", "os": "windows-2022",
                        "playwright": "1.63.0", "tier": "extended"}
        result = self.plan([TARGET, old])
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.output()
        self.assertEqual([row["id"] for row in data["include"]], [TARGET["id"]])
        (self.root / "output").unlink()
        result = self.plan([TARGET, old], BROWSER_EXTENDED="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = self.output()
        self.assertEqual(len(data["include"]), 2)

    def reserve_env(self):
        return {"CI_HOSTED_RUNNERS_ENABLED": "false", "CI_RUNNER_MODE": "self-hosted",
                "CI_RUNNER_LABELS": '["reviewed-ci"]', "RUNNER_ENVIRONMENT": "self-hosted"}

    def test_disabled_hosted_selects_real_linux_and_reports_omitted_targets(self):
        rows = self.release_targets()
        for extended, count, omitted in ((False, 3, 2), (True, 13, 22)):
            (self.root / "output").unlink(missing_ok=True)
            result = self.plan(rows, BROWSER_EXTENDED=str(extended).lower(), **self.reserve_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            selected, coverage = self.output()["include"], self.output("coverage")
            self.assertEqual(len(selected), count)
            self.assertTrue(all(row["os"] == "ubuntu-latest" for row in selected))
            self.assertEqual(len(coverage["skippedTargets"]), omitted)
            self.assertTrue(all(row["reason"] == "hosted-runners-disabled" for row in coverage["skippedTargets"]))
            self.assertEqual(coverage["selectedTargetIds"], [row["id"] for row in selected])
            self.assertFalse(coverage["hostedRunnersEnabled"])

    def test_disabled_hosted_requires_configured_real_reserve(self):
        for override in ({"CI_RUNNER_MODE": "github"}, {"CI_RUNNER_LABELS": "[]"},
                         {"CI_RUNNER_LABELS": "invalid"}, {"CI_RUNNER_LABELS": '[""]'},
                         {"RUNNER_ENVIRONMENT": "github-hosted"}, {"CI_HOSTED_RUNNERS_ENABLED": "False"}):
            with self.subTest(override=override):
                result = self.plan([TARGET], **(self.reserve_env() | override))
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "output").exists())

    def test_disabled_hosted_release_tests_preserve_inventory_but_report_reduced_scope(self):
        for channel in ("nightly", "beta", "rc", "stable"):
            (self.root / "output").unlink(missing_ok=True)
            result = self.plan(self.release_targets(), BROWSER_RELEASE_CHANNEL=channel,
                               BROWSER_EXTENDED=str(channel in ("rc", "stable")).lower(),
                               **self.reserve_env())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.output("coverage")["releaseChannel"], channel)

    def test_selected_scope_gate_rejects_failed_skipped_and_missing_results(self):
        result = self.plan([TARGET], **self.reserve_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        coverage = self.output("coverage")
        env = self.reserve_env() | {"CHANGE_RESULT": "success", "CHANGE_RUN": "true",
            "CHANGE_REASON": "forced", "PLAN_RESULT": "success", "BROWSER_RESULT": "success",
            "BROWSER_COVERAGE": json.dumps(coverage), "GITHUB_STEP_SUMMARY": str(self.root / "summary")}
        for state in ("failure", "skipped", "cancelled", ""):
            for name in ("CHANGE_RESULT", "PLAN_RESULT", "BROWSER_RESULT"):
                with self.subTest(name=name, state=state):
                    result = self.execute("browser-gate", "Require selected browser checks and report omitted platforms", **(env | {name: state}))
                    self.assertNotEqual(result.returncode, 0)
        result = self.execute("browser-gate", "Require selected browser checks and report omitted platforms", **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output("coverage")["status"], "selected-targets-passed")

    def test_malformed_targets_fail_before_any_runner_is_selected(self):
        for changed in (
            {"os": "private-production"}, {"playwright": "latest"},
            {"playwright": "1.64.0;echo injected"}, {"toolchain": "../outside"},
            {"browser_version": "latest"}, {"engine": "safari"},
            {"tier": "optional"}, {"channel": "firefox"},
            {"channel": "chrome", "engine": "firefox"}, {"command": "echo ignored"},
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(self.plan([TARGET | changed]).returncode, 0)
        self.assertNotEqual(self.plan([TARGET, TARGET]).returncode, 0)
        self.assertNotEqual(self.plan([TARGET], BROWSER_COMMAND=" ").returncode, 0)
        self.assertNotEqual(self.plan([TARGET | {"tier": "extended"}]).returncode, 0)
        self.assertFalse((self.root / "output").exists())

    def release_targets(self):
        spec = importlib.util.spec_from_file_location("release_browser_generator", ROOT / "scripts/prepare_browser_compatibility.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        return json.loads(generator.render()[".github/browser-compatibility.json"])

    def test_release_channels_cannot_downgrade_rc_or_expand_beta(self):
        rows = self.release_targets()
        for channel in ("nightly", "beta", "rc", "stable"):
            extended = channel in ("rc", "stable")
            with self.subTest(channel=channel):
                result = self.plan(rows, BROWSER_RELEASE_CHANNEL=channel,
                                   BROWSER_EXTENDED=str(extended).lower())
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(self.plan(rows, BROWSER_RELEASE_CHANNEL=channel,
                                              BROWSER_EXTENDED=str(not extended).lower()).returncode, 0)
        self.assertNotEqual(self.plan(rows, BROWSER_RELEASE_CHANNEL="release-candidate").returncode, 0)
        self.assertNotEqual(self.plan(rows[:-1], BROWSER_RELEASE_CHANNEL="beta").returncode, 0)
        duplicate_identity = copy.deepcopy(rows)
        duplicate_identity[-1] = duplicate_identity[-2] | {"id": rows[-1]["id"]}
        self.assertNotEqual(self.plan(duplicate_identity, BROWSER_RELEASE_CHANNEL="rc",
                                     BROWSER_EXTENDED="true").returncode, 0)

    def regression_targets(self):
        rows = self.release_targets()
        regression = next(row for row in rows if row["os"] == "windows-2022"
                          and row["engine"] == "firefox" and row["playwright"] == "1.62.1")
        regression["tier"] = "required"
        return rows

    def test_required_profile_is_declared_and_bound_to_both_actual_scripts(self):
        declaration = WORKFLOW["on"]["workflow_call"]["inputs"]["browser-required-profile"]
        self.assertEqual(declaration["default"], "baseline")
        for job, name in (("browser-plan", "Validate and select the explicit compatibility inventory"),
                          ("browser-verdict", "Verify the exact release target set and evidence identity")):
            step = next(row for row in WORKFLOW["jobs"][job]["steps"] if row.get("name") == name)
            self.assertEqual(step["env"]["BROWSER_REQUIRED_PROFILE"], "${{ inputs.browser-required-profile }}")

    def test_historical_profile_preserves_all_baseline_targets_and_full_release_inventory(self):
        rows = self.regression_targets()
        for channel in ("", "nightly", "beta", "rc", "stable"):
            with self.subTest(channel=channel):
                (self.root / "output").unlink(missing_ok=True)
                full = channel in ("rc", "stable")
                result = self.plan(rows, BROWSER_REQUIRED_PROFILE="windows-firefox-1.62.1",
                                   BROWSER_RELEASE_CHANNEL=channel, BROWSER_EXTENDED=str(full).lower())
                self.assertEqual(result.returncode, 0, result.stderr)
                selected = self.output()["include"]
                self.assertEqual(len(selected), 35 if full else 6)
                baseline = {row["id"] for row in self.release_targets() if row["tier"] == "required"}
                self.assertTrue(baseline.issubset({row["id"] for row in selected if row["tier"] == "required"}))
                self.assertEqual(selected, [row | {"channel": row.get("channel", "")} for row in rows
                                            if full or row["tier"] == "required"])

    def test_required_profile_rejects_unknown_missing_swapped_or_undeclared_regression(self):
        for profile, rows in (("unknown", self.regression_targets()), ("", self.regression_targets()),
                              ("windows-firefox-1.62.1", self.release_targets()),
                              ("baseline", self.regression_targets())):
            result = self.plan(rows, BROWSER_REQUIRED_PROFILE=profile, BROWSER_RELEASE_CHANNEL="rc",
                               BROWSER_EXTENDED="true")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((self.root / "output").exists())
        for changed in ({"playwright": "1.63.0"}, {"engine": "webkit"}, {"os": "ubuntu-latest"},
                        {"tier": "extended"}):
            rows = self.regression_targets()
            target = next(row for row in rows if row["os"] == "windows-2022" and row["engine"] == "firefox"
                          and row["playwright"] == "1.62.1")
            target.update(changed)
            self.assertNotEqual(self.plan(rows, BROWSER_REQUIRED_PROFILE="windows-firefox-1.62.1",
                                          BROWSER_RELEASE_CHANNEL="rc", BROWSER_EXTENDED="true").returncode, 0)
        rows = self.regression_targets()
        # Keep the count at six while removing a mandatory current target.
        baseline = next(row for row in rows if row["tier"] == "required" and row["os"] == "ubuntu-latest")
        other = next(row for row in rows if row["tier"] == "extended")
        baseline["tier"], other["tier"] = "extended", "required"
        self.assertNotEqual(self.plan(rows, BROWSER_REQUIRED_PROFILE="windows-firefox-1.62.1",
                                      BROWSER_RELEASE_CHANNEL="rc", BROWSER_EXTENDED="true").returncode, 0)

    def test_historical_profile_verdict_requires_exact_sixth_result_and_attempt(self):
        for channel in ("beta", "rc"):
            with self.subTest(channel=channel):
                import shutil
                shutil.rmtree(self.root / "evidence", ignore_errors=True)
                rows, directory = self.release_evidence(channel, self.regression_targets())
                env = {"BROWSER_REQUIRED_PROFILE": "windows-firefox-1.62.1"}
                result = self.verdict(rows, directory, channel=channel, **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                receipt = json.loads((self.root / "verdict.json").read_text())
                self.assertEqual(receipt["requiredProfile"], "windows-firefox-1.62.1")
                self.assertEqual(len(receipt["targets"]), 6 if channel == "beta" else 35)
                self.assertNotEqual(self.verdict(rows, directory, channel=channel).returncode, 0)
                self.assertNotEqual(self.verdict(rows, directory, channel=channel,
                                                BROWSER_REQUIRED_PROFILE="unknown").returncode, 0)
                target = next(row for row in rows if row["os"] == "windows-2022" and row["engine"] == "firefox"
                              and row["playwright"] == "1.62.1")
                file = directory / f"browser-release-{target['id']}-2" / "result.json"
                data = json.loads(file.read_text())
                file.write_text(json.dumps(data | {"runAttempt": "1"}))
                self.assertNotEqual(self.verdict(rows, directory, channel=channel, **env).returncode, 0)
                file.write_text(json.dumps(data))
                swapped = copy.deepcopy(rows)
                next(row for row in swapped if row["id"] == target["id"])["engine"] = "webkit"
                self.assertNotEqual(self.verdict(swapped, directory, channel=channel, **env).returncode, 0)

    def test_historical_profile_reduced_scope_never_claims_windows_qualification(self):
        rows = self.regression_targets()
        env = {"BROWSER_REQUIRED_PROFILE": "windows-firefox-1.62.1"}
        result = self.plan(rows, BROWSER_RELEASE_CHANNEL="beta", **(self.reserve_env() | env))
        self.assertEqual(result.returncode, 0, result.stderr)
        coverage = self.output("coverage")
        self.assertEqual(len(coverage["skippedTargets"]), 3)
        rows, directory = self.release_evidence("beta", self.output()["include"])
        result = self.verdict(rows, directory, channel="beta", **(env | {
            "CI_HOSTED_RUNNERS_ENABLED": "false", "BROWSER_COVERAGE": json.dumps(coverage)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.root / "verdict.json").read_text())
        self.assertFalse(receipt["fullPlatformQualified"])
        self.assertEqual(len(receipt["targets"]), 3)
        self.assertEqual(len(receipt["skippedTargets"]), 3)

    def test_linux_scope_preserves_every_selected_linux_target(self):
        rows = self.release_targets()
        for extended in (False, True):
            for scope in ("all", "linux"):
                with self.subTest(scope=scope, extended=extended):
                    (self.root / "output").unlink(missing_ok=True)
                    result = self.plan(rows, BROWSER_PLATFORM_SCOPE=scope,
                                       BROWSER_EXTENDED=str(extended).lower())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    selected = self.output()["include"]
                    expected = [row | {"channel": row.get("channel", "")} for row in rows
                                if (extended or row["tier"] == "required")
                                and (scope == "all" or row["os"] == "ubuntu-latest")]
                    self.assertEqual(selected, expected)
                    self.assertEqual(len(selected), (35 if extended else 5) if scope == "all"
                                     else (13 if extended else 3))
                    self.assertEqual(json.loads(result.stdout), {
                        "platformScope": scope, "selectedTargetIds": [row["id"] for row in expected],
                    })

    def test_linux_scope_requires_required_linux_and_validates_excluded_targets(self):
        windows = TARGET | {"id": "windows", "os": "windows-2022"}
        for rows, scope in (
            ([TARGET], ""), ([TARGET], "windows"), ([TARGET], "ALL"),
            ([windows], "linux"),
            ([windows, TARGET | {"tier": "extended"}], "linux"),
            ([TARGET, windows | {"playwright": "latest"}], "linux"),
        ):
            with self.subTest(rows=rows, scope=scope):
                self.assertNotEqual(self.plan(rows, BROWSER_PLATFORM_SCOPE=scope,
                                             BROWSER_EXTENDED="true").returncode, 0)
                self.assertFalse((self.root / "output").exists())

    def test_linux_scope_cannot_claim_any_release_qualification(self):
        for channel in ("nightly", "beta", "rc", "stable"):
            with self.subTest(channel=channel):
                result = self.plan(self.release_targets(), BROWSER_PLATFORM_SCOPE="linux",
                                   BROWSER_RELEASE_CHANNEL=channel,
                                   BROWSER_EXTENDED=str(channel in ("rc", "stable")).lower())
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Release inventory selection requires all platforms", result.stderr)
                self.assertFalse((self.root / "output").exists())

    def test_linux_scope_excludes_fixed_hosted_ubuntu_without_relabelling(self):
        fixed = [TARGET | {"id": os_name.replace(".", "-"), "os": os_name}
                 for os_name in ("ubuntu-24.04", "ubuntu-26.04")]
        for extended in ("false", "true"):
            for scope in ("all", "linux"):
                with self.subTest(scope=scope, extended=extended):
                    (self.root / "output").unlink(missing_ok=True)
                    result = self.plan([TARGET, *fixed], BROWSER_PLATFORM_SCOPE=scope,
                                       BROWSER_EXTENDED=extended)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    selected = self.output()["include"]
                    expected = [TARGET, *fixed] if scope == "all" else [TARGET]
                    self.assertEqual(selected, [row | {"channel": ""} for row in expected])
        (self.root / "output").unlink(missing_ok=True)
        self.assertNotEqual(self.plan(fixed, BROWSER_PLATFORM_SCOPE="linux",
                                     BROWSER_EXTENDED="true").returncode, 0)
        self.assertFalse((self.root / "output").exists())

    def release_evidence(self, channel="rc", rows=None):
        rows = self.release_targets() if rows is None else rows
        if channel in ("nightly", "beta"):
            rows = [row for row in rows if row["tier"] == "required"]
        directory = self.root / "evidence"
        directory.mkdir(exist_ok=True)
        binding = {"schema": 1, "sourceSha": "a" * 40, "runId": "123",
                   "runAttempt": "2", "releaseChannel": channel, "candidateArtifactId": "456"}
        for row in rows:
            folder = directory / f"browser-release-{row['id']}-2"
            folder.mkdir()
            platform = "linux" if row["os"].startswith("ubuntu-") else "win32" if row["os"].startswith("windows-") else "darwin"
            runtime = binding | {"target": row["id"], "engine": row["engine"],
                                 "channel": row.get("channel", ""), "playwrightVersion": row["playwright"],
                                 "platform": platform, "browserVersion": "156.0.1.2" if row.get("channel") else row["browser_version"]}
            if row["os"] == "ubuntu-26.04":
                runtime.update(runnerArchitecture="x64", runnerOsId="ubuntu", runnerOsVersion="26.04")
            row.setdefault("channel", "")
            for name, data in (("target", row), ("runtime", runtime),
                               ("result", runtime | {"testsPassed": 2, "testsFailed": 0, "scenarios": ["sign in", "refresh state"]})):
                (folder / f"{name}.json").write_text(json.dumps(data))
        return rows, directory

    def test_release_inventory_preserves_full_topology_not_only_target_counts(self):
        original = self.release_targets()
        changes = [
            (0, {"os": "ubuntu-24.04"}),
            (0, {"playwright": "1.65.0"}),
            (0, {"browser_version": "155.0.0.1"}),
            (next(index for index, row in enumerate(original) if row.get("channel") == "chrome"),
             {"playwright": "1.63.0"}),
        ]
        for index, changed in changes:
            rows = copy.deepcopy(original)
            rows[index].update(changed)
            with self.subTest(changed=changed):
                self.assertNotEqual(self.plan(rows, BROWSER_RELEASE_CHANNEL="rc",
                                             BROWSER_EXTENDED="true").returncode, 0)
        rows = copy.deepcopy(original)
        required = next(row for row in rows if row["tier"] == "required")
        extended = next(row for row in rows if row["tier"] == "extended")
        required["tier"], extended["tier"] = "extended", "required"
        self.assertNotEqual(self.plan(rows, BROWSER_RELEASE_CHANNEL="rc",
                                     BROWSER_EXTENDED="true").returncode, 0)
        rows = copy.deepcopy(original)
        for row in rows:
            if row["engine"] == "firefox":
                row["engine"], row["channel"], row["browser_version"] = "chromium", "chrome-beta", "installed"
        self.assertNotEqual(self.plan(rows, BROWSER_RELEASE_CHANNEL="rc",
                                     BROWSER_EXTENDED="true").returncode, 0)

    def test_release_inventory_accepts_reviewed_intel_macos_targets_without_rewriting_runners(self):
        for all_macos in (False, True):
            rows = self.release_targets()
            for row in rows:
                if row["os"] == "macos-15" and (all_macos or (
                    row["engine"] == "webkit" and row["playwright"] != "1.64.0"
                )):
                    row["os"] = "macos-15-intel"
                    row["id"] = row["id"].replace("macos-15-", "macos-15-intel-")
            for channel in ("nightly", "beta", "rc", "stable"):
                with self.subTest(all_macos=all_macos, channel=channel):
                    full = channel in ("rc", "stable")
                    output = self.root / "output"
                    output.unlink(missing_ok=True)
                    result = self.plan(rows, BROWSER_RELEASE_CHANNEL=channel,
                                       BROWSER_EXTENDED=str(full).lower())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    selected = self.output()["include"]
                    expected = [row | {"channel": row.get("channel", "")}
                                for row in rows if full or row["tier"] == "required"]
                    self.assertEqual(selected, expected)
                    self.assertEqual(len(selected), 35 if full else 5)

    def test_release_inventory_rejects_intel_alias_duplicates_and_missing_os_coverage(self):
        rows = self.release_targets()
        mac = next(row for row in rows if row["os"] == "macos-15" and row["engine"] == "webkit")
        windows_index = next(index for index, row in enumerate(rows)
                             if row["os"] == "windows-2022" and row["engine"] == mac["engine"]
                             and row["playwright"] == mac["playwright"])
        rows[windows_index] = mac | {"id": "intel-alias", "os": "macos-15-intel",
                                     "tier": rows[windows_index]["tier"]}
        result = self.plan(rows, BROWSER_RELEASE_CHANNEL="rc", BROWSER_EXTENDED="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not repeat", result.stderr)
        self.assertFalse((self.root / "output").exists())

        rows = self.release_targets()
        mac = next(row for row in rows if row["os"] == "macos-15"
                   and row["engine"] == "chromium" and row.get("channel") == "chrome")
        windows_index = next(index for index, row in enumerate(rows)
                             if row["os"] == "windows-2022" and row["engine"] == "chromium"
                             and not row.get("channel") and row["playwright"] != mac["playwright"])
        rows[windows_index] = mac | {
            "id": "intel-distinct", "os": "macos-15-intel",
            "playwright": rows[windows_index]["playwright"], "tier": rows[windows_index]["tier"],
        }
        result = self.plan(rows, BROWSER_RELEASE_CHANNEL="rc", BROWSER_EXTENDED="true")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete three-OS", result.stderr)
        self.assertFalse((self.root / "output").exists())

    def linux26_targets(self):
        rows = self.release_targets()
        for row in rows:
            if row["os"] == "ubuntu-latest" and row["engine"] == "webkit":
                row["os"] = "ubuntu-26.04"
                row["id"] = row["id"].replace("ubuntu-latest", "ubuntu-26-04")
        return rows

    def test_explicit_linux26_preserves_runner_and_release_breadth(self):
        rows = self.linux26_targets()
        for channel in ("nightly", "beta", "rc", "stable"):
            full = channel in ("rc", "stable")
            with self.subTest(channel=channel):
                (self.root / "output").unlink(missing_ok=True)
                result = self.plan(rows, BROWSER_RELEASE_CHANNEL=channel,
                                   BROWSER_EXTENDED=str(full).lower())
                self.assertEqual(result.returncode, 0, result.stderr)
                selected = self.output()["include"]
                self.assertEqual(selected, [row | {"channel": row.get("channel", "")}
                                            for row in rows if full or row["tier"] == "required"])
                self.assertEqual(len(selected), 35 if full else 5)
                self.assertEqual(sum(row["os"] == "ubuntu-26.04" for row in selected), 3 if full else 1)

    def test_linux26_alias_cannot_duplicate_linux_or_replace_other_os(self):
        for duplicate in (True, False):
            rows = self.release_targets()
            linux = next(row for row in rows if row["os"] == "ubuntu-latest"
                         and (row["engine"] == "webkit" if duplicate else row.get("channel") == "chrome"))
            index = next(index for index, row in enumerate(rows)
                         if row["os"] == "windows-2022" and row["engine"] == linux["engine"]
                         and not row.get("channel") and (row["playwright"] == linux["playwright"] if duplicate
                                                          else row["playwright"] != linux["playwright"]))
            rows[index] = linux | {"id": "linux26-alias", "os": "ubuntu-26.04",
                                   "playwright": rows[index]["playwright"], "tier": rows[index]["tier"]}
            result = self.plan(rows, BROWSER_RELEASE_CHANNEL="rc", BROWSER_EXTENDED="true")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must not repeat" if duplicate else "complete three-OS", result.stderr)
            self.assertFalse((self.root / "output").exists())

    def test_linux26_verdict_requires_distribution_and_architecture_identity(self):
        rows, directory = self.release_evidence(rows=self.linux26_targets())
        result = self.verdict(rows, directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        row = next(row for row in rows if row["os"] == "ubuntu-26.04")
        folder = directory / f"browser-release-{row['id']}-2"
        for name in ("runtime", "result"):
            path = folder / f"{name}.json"
            original = json.loads(path.read_text())
            for key, value in (("runnerOsVersion", "24.04"), ("runnerOsId", "debian"),
                               ("runnerArchitecture", "arm64"), ("runnerOsVersion", None)):
                path.write_text(json.dumps(original | {key: value}))
                self.assertNotEqual(self.verdict(rows, directory).returncode, 0)
            path.write_text(json.dumps(original))


    def verdict(self, rows, directory, channel="rc", **env):
        return self.execute("browser-verdict", "Verify the exact release target set and evidence identity",
                            BROWSER_MATRIX=json.dumps({"include": rows}),
                            BROWSER_RELEASE_CHANNEL=channel, BROWSER_ARTIFACT_ID="456",
                            BROWSER_EVIDENCE_PREFIX="browser-release", BROWSER_EVIDENCE_DIRECTORY=str(directory),
                            BROWSER_VERDICT_FILE=str(self.root / "verdict.json"), GITHUB_SHA="a" * 40,
                            GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", **env)

    def test_release_verdict_binds_every_real_result_to_the_exact_plan(self):
        rows, directory = self.release_evidence()
        result = self.verdict(rows, directory)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.root / "verdict.json").read_text())
        self.assertEqual(len(receipt["targets"]), 35)
        self.assertEqual(receipt["candidateArtifactId"], "456")
        self.assertTrue(all(len(target["evidenceSha256"]) == 64 for target in receipt["targets"]))
        self.assertNotEqual(self.verdict(rows[:-1], directory).returncode, 0)
        self.assertNotEqual(self.verdict(rows[:-1] + [rows[0]], directory).returncode, 0)
        self.assertNotEqual(self.verdict(rows, directory, channel="beta").returncode, 0)
        self.assertNotEqual(self.verdict(rows, directory, channel="invalid").returncode, 0)

    def test_beta_verdict_accepts_only_the_five_required_targets(self):
        rows, directory = self.release_evidence("beta")
        result = self.verdict(rows, directory, channel="beta")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.verdict(rows, directory, channel="rc").returncode, 0)
        rows[0]["tier"] = "extended"
        self.assertNotEqual(self.verdict(rows, directory, channel="beta").returncode, 0)

    def test_reduced_release_verdict_is_explicit_and_still_requires_every_selected_result(self):
        result = self.plan(self.release_targets(), BROWSER_RELEASE_CHANNEL="rc",
                           BROWSER_EXTENDED="true", **self.reserve_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        coverage = self.output("coverage")
        rows, directory = self.release_evidence(rows=self.output()["include"])
        env = {"CI_HOSTED_RUNNERS_ENABLED": "false", "BROWSER_COVERAGE": json.dumps(coverage)}
        result = self.verdict(rows, directory, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads((self.root / "verdict.json").read_text())
        self.assertFalse(receipt["fullPlatformQualified"])
        self.assertEqual(receipt["platformCoverage"], "reduced-linux")
        self.assertEqual(len(receipt["targets"]), 13)
        self.assertEqual(len(receipt["skippedTargets"]), 22)
        self.assertNotEqual(self.verdict(rows, directory).returncode, 0)
        self.assertNotEqual(self.verdict(rows[:-1], directory, **env).returncode, 0)
        self.assertNotEqual(self.verdict(rows, directory, **(env | {"BROWSER_COVERAGE": "{}"})).returncode, 0)

    def test_release_verdict_rejects_missing_or_unexpected_artifacts(self):
        rows, directory = self.release_evidence("beta")
        folder = next(directory.iterdir())
        missing = directory / "unrelated-extra"
        folder.rename(missing)
        self.assertNotEqual(self.verdict(rows, directory, channel="beta").returncode, 0)
        missing.rename(folder)
        (directory / "duplicate-evidence").mkdir()
        self.assertNotEqual(self.verdict(rows, directory, channel="beta").returncode, 0)
        (directory / "duplicate-evidence").rmdir()
        (folder / "runtime.json").unlink()
        self.assertNotEqual(self.verdict(rows, directory, channel="beta").returncode, 0)

    def test_release_verdict_rejects_stale_mismatched_and_failed_receipts(self):
        rows, directory = self.release_evidence("beta")
        folder = next(directory.iterdir())
        for name in ("runtime", "result"):
            file = folder / f"{name}.json"
            original = json.loads(file.read_text())
            for changed in ({"runAttempt": "1"}, {"runId": "122"}, {"sourceSha": "b" * 40},
                            {"releaseChannel": "rc"}, {"candidateArtifactId": "455"},
                            {"target": "other"}, {"engine": "other"}, {"platform": "other"},
                            {"playwrightVersion": "1.0.0"}, {"browserVersion": "155.0.0.1"}):
                with self.subTest(name=name, changed=changed):
                    file.write_text(json.dumps(original | changed))
                    self.assertNotEqual(self.verdict(rows, directory, channel="beta").returncode, 0)
            file.write_text(json.dumps(original))
        file = folder / "result.json"
        original = json.loads(file.read_text())
        for changed in ({"testsPassed": 0}, {"testsFailed": 1}, {"scenarios": ["same", "same"]}):
            file.write_text(json.dumps(original | changed))
            self.assertNotEqual(self.verdict(rows, directory, channel="beta").returncode, 0)

    def test_verdict_is_required_for_release_and_does_not_accept_skipped_jobs(self):
        job = WORKFLOW["jobs"]["browser-verdict"]
        self.assertEqual(job["needs"], ["browser-plan", "browser"])
        self.assertEqual(job["if"], "${{ !cancelled() && inputs.browser-release-channel != '' }}")
        require = next(step for step in job["steps"] if step.get("name") == "Require all browser jobs to succeed")
        for result in ("success", "skipped", "failure", "cancelled", ""):
            status = subprocess.run(["bash", "-c", require["run"]], env=os.environ | {
                "PLAN_RESULT": "success", "BROWSER_RESULT": result}, check=False)
            self.assertEqual(status.returncode == 0, result == "success")
        download = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/download-artifact@"))
        self.assertNotIn("run-id", download["with"])
        self.assertEqual(download["with"]["merge-multiple"], "false")

    def test_branded_channel_records_installed_version_without_claiming_a_pin(self):
        target = TARGET | {"channel": "msedge", "browser_version": "installed"}
        result = self.plan([target])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_candidate_artifact_is_one_exact_id_and_a_relative_destination(self):
        result = self.plan([TARGET], BROWSER_ARTIFACT_ID="12345", BROWSER_ARTIFACT_PATH="shared-core/dist")
        self.assertEqual(result.returncode, 0, result.stderr)
        for artifact_id, destination in (
            ("latest", "shared-core/dist"), ("123,456", "shared-core/dist"),
            ("0", "shared-core/dist"), ("123", "../outside"),
            ("123", "/absolute"), ("123", ""), ("", "shared-core/dist"),
        ):
            with self.subTest(artifact_id=artifact_id, destination=destination):
                self.assertNotEqual(self.plan([TARGET], BROWSER_ARTIFACT_ID=artifact_id,
                                             BROWSER_ARTIFACT_PATH=destination).returncode, 0)
        step = next(step for step in WORKFLOW["jobs"]["browser"]["steps"]
                    if step.get("name") == "Download the exact candidate artifact from this workflow run")
        self.assertEqual(step["with"]["artifact-ids"], "${{ inputs.browser-artifact-id }}")
        self.assertNotIn("run-id", step["with"])
        self.assertNotIn("name", step["with"])

    def test_artifact_destination_rejects_existing_symlinks_and_files_on_actual_runner(self):
        def verify(destination):
            return self.execute("browser", "Validate candidate artifact destination",
                                GITHUB_WORKSPACE=str(self.root), BROWSER_ARTIFACT_PATH=destination)

        self.assertEqual(verify("build/dist").returncode, 0)
        (self.root / "build").mkdir()
        self.assertEqual(verify("build/dist").returncode, 0)
        (self.root / "outside").symlink_to(ROOT, target_is_directory=True)
        self.assertNotEqual(verify("outside/dist").returncode, 0)
        (self.root / "build/link").symlink_to(self.root / "missing", target_is_directory=True)
        self.assertNotEqual(verify("build/link/dist").returncode, 0)
        (self.root / "plain").write_text("not a directory")
        self.assertNotEqual(verify("plain/dist").returncode, 0)

    def test_prepared_reserve_does_not_install_system_packages(self):
        steps = WORKFLOW["jobs"]["browser"]["steps"]
        installers = [step for step in steps if "cli.js\" install" in step.get("run", "")]
        self.assertEqual(len(installers), 3)
        hosted = next(step for step in installers if "--with-deps" in step["run"])
        reserve = next(step for step in installers if "self-hosted" in step["if"])
        self.assertEqual(hosted["if"], "runner.environment == 'github-hosted'")
        self.assertEqual(reserve["if"], "${{ runner.environment == 'self-hosted' && matrix.channel == '' }}")
        self.assertNotIn("sudo", reserve["run"])

    def test_windows_webkit_installs_only_locked_media_dependencies_and_propagates_failure(self):
        steps = WORKFLOW["jobs"]["browser"]["steps"]
        step = next(step for step in steps if step.get("name") == "Install Windows WebKit media dependencies")
        self.assertEqual(step["if"], "${{ runner.environment == 'github-hosted' && runner.os == 'Windows' && matrix.engine == 'webkit' }}")
        names = [step.get("name") for step in steps]
        self.assertLess(names.index("Install exact locked browser toolchain"), names.index(step["name"]))
        self.assertLess(names.index(step["name"]), names.index("Install selected browser and hosted system dependencies"))
        self.assertNotIn("continue-on-error", step)
        module = self.root / "locked toolchain/node_modules/playwright"
        module.mkdir(parents=True)
        (module / "cli.js").write_text(
            "require('node:fs').writeFileSync(process.env.ARGUMENTS_FILE, JSON.stringify(process.argv.slice(2)));"
            "process.exit(Number(process.env.INSTALL_EXIT));"
        )
        arguments = self.root / "arguments.json"
        for code in (0, 23):
            result = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.root,
                env=self.env | {"CI_PLAYWRIGHT_MODULE": str(module), "ARGUMENTS_FILE": str(arguments),
                                "INSTALL_EXIT": str(code)}, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertEqual(json.loads(arguments.read_text()), ["install-deps", "chromium"])

    def test_distinct_calls_can_namespace_artifacts_without_changing_inventory(self):
        self.assertEqual(self.plan([TARGET], BROWSER_EVIDENCE_PREFIX="browser-release").returncode, 0)
        for prefix in ("", "../escape", "two words", "x" * 65):
            self.assertNotEqual(self.plan([TARGET], BROWSER_EVIDENCE_PREFIX=prefix).returncode, 0)
        upload = next(step for step in WORKFLOW["jobs"]["browser"]["steps"]
                      if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["name"],
                         "${{ inputs.browser-evidence-prefix }}-${{ matrix.id }}-${{ github.run_attempt }}")

    def test_matrix_file_cannot_escape_checkout(self):
        matrix = self.root / "targets.json"
        matrix.write_text(json.dumps([TARGET]))
        result = self.plan([], BROWSER_MATRIX_FILE="targets.json")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.plan([TARGET], BROWSER_MATRIX_FILE="targets.json")
        self.assertNotEqual(result.returncode, 0)
        (self.root / "outside").symlink_to(ROOT / "README.md")
        self.assertNotEqual(self.plan([], BROWSER_MATRIX_FILE="outside").returncode, 0)

    def make_lock(self):
        directory = self.root / TARGET["toolchain"]
        directory.mkdir(parents=True)
        package = {"private": True, "dependencies": {"playwright": TARGET["playwright"]}}
        packages = {"": {"dependencies": package["dependencies"]}}
        for name in ("playwright", "playwright-core"):
            packages["node_modules/" + name] = {
                "version": TARGET["playwright"], "integrity": "sha512-test==",
                "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{TARGET['playwright']}.tgz",
            }
        lock = {"lockfileVersion": 3, "packages": packages}
        (directory / "package.json").write_text(json.dumps(package))
        (directory / "package-lock.json").write_text(json.dumps(lock))
        return directory, lock

    def lock(self, **target):
        os_name = "windows-2022" if sys.platform == "win32" else "macos-15" if sys.platform == "darwin" else "ubuntu-latest"
        return self.execute(
            "browser", "Validate the isolated lock and actual runner platform",
            CI_BROWSER_TARGET=json.dumps(TARGET | {"os": os_name} | target),
        )

    def test_lock_integrity_and_runner_platform_are_mandatory(self):
        directory, original = self.make_lock()
        result = self.lock()
        self.assertEqual(result.returncode, 0, result.stderr)
        for changed in ("integrity", "version", "resolved"):
            lock = copy.deepcopy(original)
            lock["packages"]["node_modules/playwright"][changed] = "wrong"
            (directory / "package-lock.json").write_text(json.dumps(lock))
            self.assertNotEqual(self.lock().returncode, 0)
        (directory / "package-lock.json").write_text(json.dumps(original))
        self.assertNotEqual(self.lock(os="windows-2022" if sys.platform != "win32" else "macos-15").returncode, 0)

    def test_intel_macos_lock_admission_requires_actual_x64_platform(self):
        self.make_lock()
        body = script("browser", "Validate the isolated lock and actual runner platform")
        for platform, architecture, accepted in (
            ("darwin", "x64", True), ("darwin", "arm64", False),
            ("darwin", "ia32", False), ("linux", "x64", False),
        ):
            with self.subTest(platform=platform, architecture=architecture):
                prelude = (
                    "Object.defineProperty(process, 'platform', {value: " + json.dumps(platform) + "});"
                    "Object.defineProperty(process, 'arch', {value: " + json.dumps(architecture) + "});"
                )
                result = subprocess.run(
                    ["node", "-e", prelude + body], cwd=self.root,
                    env=self.env | {"CI_BROWSER_TARGET": json.dumps(TARGET | {"os": "macos-15-intel"})},
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)

    def test_linux26_lock_admission_checks_actual_distribution_and_architecture(self):
        self.make_lock()
        body = script("browser", "Validate the isolated lock and actual runner platform")
        for platform, architecture, release, accepted in (
            ("linux", "x64", 'ID=ubuntu\nVERSION_ID="26.04"\n', True),
            ("linux", "x64", 'ID="ubuntu"\nVERSION_ID=26.04\n', True),
            ("linux", "arm64", 'ID=ubuntu\nVERSION_ID="26.04"\n', False),
            ("linux", "x64", 'ID=ubuntu\nVERSION_ID="24.04"\n', False),
            ("linux", "x64", 'ID=debian\nVERSION_ID="26.04"\n', False),
            ("darwin", "x64", 'ID=ubuntu\nVERSION_ID="26.04"\n', False),
        ):
            with self.subTest(platform=platform, architecture=architecture, release=release):
                prelude = (
                    "Object.defineProperty(process, 'platform', {value: " + json.dumps(platform) + "});"
                    "Object.defineProperty(process, 'arch', {value: " + json.dumps(architecture) + "});"
                    "const testFs = require('node:fs'); const originalRead = testFs.readFileSync;"
                    "testFs.readFileSync = (file, ...args) => file === '/etc/os-release' ? "
                    + json.dumps(release) + " : originalRead(file, ...args);"
                )
                result = subprocess.run(
                    ["node", "-e", prelude + body], cwd=self.root,
                    env=self.env | {"CI_BROWSER_TARGET": json.dumps(TARGET | {"os": "ubuntu-26.04"})},
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if accepted:
                    receipt = json.loads((self.root / ".compatibility" / TARGET["id"] / "runner-platform.json").read_text())
                    self.assertEqual(receipt, {"runnerArchitecture": "x64", "runnerOsId": "ubuntu", "runnerOsVersion": "26.04"})

    def test_generator_inventory_keeps_three_real_generations_and_five_required_rows(self):
        spec = importlib.util.spec_from_file_location("browser_generator", ROOT / "scripts/prepare_browser_compatibility.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        files = generator.render()
        rows = json.loads(files[".github/browser-compatibility.json"])
        self.assertEqual(len(rows), 35)
        self.assertEqual(len([row for row in rows if row["tier"] == "required"]), 5)
        self.assertEqual(len({row["playwright"] for row in rows}), 3)
        self.assertEqual({row["os"] for row in rows}, {"ubuntu-latest", "windows-2022", "macos-15"})
        for engine in ("chromium", "firefox", "webkit"):
            self.assertEqual(len({row["browser_version"] for row in rows if row["engine"] == engine and not row.get("channel")}), 3)
        result = self.plan(rows, BROWSER_EXTENDED="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative, content in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        for row in rows:
            self.assertEqual(self.lock(**{key: value for key, value in row.items() if key != "os"}).returncode, 0)
        command = [sys.executable, str(ROOT / "scripts/prepare_browser_compatibility.py"), str(self.root), "--check"]
        self.assertEqual(subprocess.run(command, capture_output=True, check=False).returncode, 0)
        (self.root / ".github/browser-compatibility.json").write_text("[]")
        self.assertNotEqual(subprocess.run(command, capture_output=True, check=False).returncode, 0)
        self.assertEqual((self.root / ".github/browser-compatibility.json").read_text(), "[]")

    def test_runtime_probe_rejects_wrong_binary_and_closes_the_browser(self):
        module = self.root / "node_modules/playwright"
        core = self.root / "node_modules/playwright-core"
        module.mkdir(parents=True)
        core.mkdir(parents=True)
        (module / "package.json").write_text(json.dumps({"version": "1.64.0", "main": "index.js"}))
        (core / "package.json").write_text(json.dumps({"version": "1.64.0"}))
        for actual in ("156.0.8078.4", "155.0.1.2"):
            (module / "index.js").write_text(
                "exports.chromium = {launch: async () => ({version: () => " + json.dumps(actual) +
                ", close: async () => require('node:fs').writeFileSync('closed', 'yes')})};"
            )
            result = self.execute(
                "browser", "Check installed runtime identity", CI_PLAYWRIGHT_MODULE=str(module),
                CI_PLAYWRIGHT_VERSION="1.64.0", CI_BROWSER_ENGINE="chromium", CI_BROWSER_CHANNEL="",
                CI_BROWSER_EXPECTED_VERSION="156.0.8078.4", CI_BROWSER_TARGET_ID="fixture",
                CI_BROWSER_DIAGNOSTICS=str(self.root), GITHUB_SHA="a" * 40,
                GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
            )
            self.assertEqual(result.returncode == 0, actual == "156.0.8078.4", result.stderr)
            self.assertEqual((self.root / "closed").read_text(), "yes")
            (self.root / "closed").unlink()

    def test_linux26_runtime_receipt_carries_only_qualified_platform_identity(self):
        module = self.root / "node_modules/playwright"
        core = self.root / "node_modules/playwright-core"
        module.mkdir(parents=True)
        core.mkdir(parents=True)
        (module / "package.json").write_text(json.dumps({"version": "1.64.0", "main": "index.js"}))
        (core / "package.json").write_text(json.dumps({"version": "1.64.0"}))
        (module / "index.js").write_text(
            "exports.chromium = {launch: async () => ({version: () => '156.0.8078.4',"
            "close: async () => require('node:fs').writeFileSync('closed', 'yes')})};"
        )
        for version in ("26.04", "24.04"):
            platform = {"runnerArchitecture": "x64", "runnerOsId": "ubuntu", "runnerOsVersion": version}
            (self.root / "runner-platform.json").write_text(json.dumps(platform))
            (self.root / "runtime.json").unlink(missing_ok=True)
            result = self.execute(
                "browser", "Check installed runtime identity", CI_PLAYWRIGHT_MODULE=str(module),
                CI_PLAYWRIGHT_VERSION="1.64.0", CI_BROWSER_ENGINE="chromium", CI_BROWSER_CHANNEL="",
                CI_BROWSER_EXPECTED_VERSION="156.0.8078.4", CI_BROWSER_TARGET_ID="fixture",
                CI_BROWSER_TARGET=json.dumps(TARGET | {"os": "ubuntu-26.04"}),
                CI_BROWSER_DIAGNOSTICS=str(self.root), GITHUB_SHA="a" * 40,
                GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
            )
            self.assertEqual(result.returncode == 0, version == "26.04", result.stderr)
            self.assertEqual((self.root / "closed").read_text(), "yes")
            if version == "26.04":
                runtime = json.loads((self.root / "runtime.json").read_text())
                self.assertTrue(all(runtime[key] == value for key, value in platform.items()))
            else:
                self.assertFalse((self.root / "runtime.json").exists())

    def test_evidence_binds_actual_browser_source_attempt_and_scenarios(self):
        runtime = {"schema": 1, "target": TARGET["id"], "engine": "chromium", "channel": "",
                   "playwrightVersion": "1.64.0", "browserVersion": "156.0.8078.4",
                   "platform": "linux", "sourceSha": "a" * 40, "runId": "123", "runAttempt": "2"}
        (self.root / "runtime.json").write_text(json.dumps(runtime))
        evidence = self.root / "result.json"
        valid = runtime | {"testsPassed": 2, "testsFailed": 0, "scenarios": ["login refresh", "render device state"]}

        def verify(data):
            evidence.write_text(json.dumps(data))
            return self.execute(
                "browser", "Require actual application test evidence",
                CI_BROWSER_DIAGNOSTICS=str(self.root), CI_BROWSER_EVIDENCE=str(evidence),
            )

        result = verify(valid)
        self.assertEqual(result.returncode, 0, result.stderr)
        for changed in (
            {"browserVersion": "old"}, {"sourceSha": "b" * 40}, {"runAttempt": "1"},
            {"engine": "webkit"}, {"testsPassed": 0}, {"testsPassed": 1.5},
            {"testsFailed": 1}, {"scenarios": ["same", "same"]}, {"scenarios": []},
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(verify(valid | changed).returncode, 0)


if __name__ == "__main__":
    unittest.main()
