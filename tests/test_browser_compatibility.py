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
            "BROWSER_COMMAND": "node scripts/application-acceptance.cjs",
        }

    def execute(self, job, name, **env):
        return subprocess.run(
            ["node", "-e", script(job, name)], cwd=self.root,
            env=self.env | env, capture_output=True, text=True, check=False,
        )

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
        data = json.loads((self.root / "output").read_text().split("=", 1)[1])
        self.assertEqual([row["id"] for row in data["include"]], [TARGET["id"]])
        (self.root / "output").unlink()
        result = self.plan([TARGET, old], BROWSER_EXTENDED="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads((self.root / "output").read_text().split("=", 1)[1])
        self.assertEqual(len(data["include"]), 2)

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
