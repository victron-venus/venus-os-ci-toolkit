"""Regression coverage for atomic CI updates and the Dependabot migration."""

import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = load("migrate_ci_updates")
contracts = load("workflow_contracts")
installer = load("install_release")
credentials = load("check_renovate_credentials")


class RenovateCredentialTests(unittest.TestCase):
    def test_classic_token_without_workflow_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing the workflow scope"):
            credentials.validate_scopes("repo, read:org")

    def test_workflow_scope_and_fine_grained_tokens_are_supported(self):
        self.assertTrue(credentials.validate_scopes("repo, workflow, read:org"))
        self.assertFalse(credentials.validate_scopes(None))

    def preflight_output(self, scope_header):
        output = io.StringIO()
        with (
            patch.dict(
                credentials.os.environ,
                {
                    "RENOVATE_TOKEN": "fixture-only",
                    "RENOVATE_GIT_PRIVATE_KEY": "fixture-only",
                },
                clear=True,
            ),
            patch.object(credentials.urllib.request, "urlopen") as urlopen,
            redirect_stdout(output),
        ):
            response = urlopen.return_value.__enter__.return_value
            response.headers = (
                {} if scope_header is None else {"X-OAuth-Scopes": scope_header}
            )
            credentials.main()
            urlopen.assert_called_once()
        return output.getvalue()

    def test_missing_scope_header_warns_that_workflow_permissions_are_unverified(self):
        output = self.preflight_output(None)
        self.assertIn("::warning::", output)
        self.assertIn("workflow write permissions remain unverified", output)
        self.assertNotIn("classic workflow scope is present", output)

    def test_classic_success_reports_only_the_checked_workflow_scope(self):
        output = self.preflight_output("repo, workflow")
        self.assertIn("classic workflow scope is present", output)
        self.assertNotIn("::warning::", output)


class DependencyMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".github").mkdir()
        self.dependabot = self.root / ".github/dependabot.yml"

    def test_preserves_application_constraints_and_comments(self):
        application = (
            "  - package-ecosystem: npm\n"
            "    # ES5 compilation requires TypeScript <6.\n"
            "    directory: /\n"
            "    schedule: {interval: daily}\n"
            "    ignore:\n"
            "      - dependency-name: typescript\n"
            "        versions: ['>=6']\n"
        )
        self.dependabot.write_text(
            "version: 2\nupdates:\n"
            "  - package-ecosystem: github-actions\n"
            "    directory: /\n"
            "    schedule: {interval: daily}\n" + application
        )
        migration.migrate(self.root)
        self.assertEqual(
            self.dependabot.read_text(),
            "version: 2\nupdates:\n" + application + migration.DISABLED_ACTIONS,
        )
        migration.migrate(self.root)
        self.assertEqual(
            self.dependabot.read_text(),
            "version: 2\nupdates:\n" + application + migration.DISABLED_ACTIONS,
        )

    def test_blocks_both_dependabot_version_and_security_prs_and_is_idempotent(self):
        self.dependabot.write_text(
            "version: 2\nupdates:\n"
            "  - package-ecosystem: github-actions\n"
            "    directory: /\n    schedule: {interval: weekly}\n"
        )
        migration.migrate(self.root)
        disabled = yaml.safe_load(self.dependabot.read_text())["updates"]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0]["open-pull-requests-limit"], 0)
        self.assertEqual(disabled[0]["ignore"], [{"dependency-name": "*"}])
        migration.migrate(self.root)
        self.assertEqual(
            yaml.safe_load(self.dependabot.read_text())["updates"], disabled
        )
        config = json.loads((self.root / "renovate.json").read_text())
        self.assertEqual(config["extends"], [migration.PRESET])

    def test_existing_renovate_policy_is_not_overwritten(self):
        (self.root / "renovate.json").write_text('{"enabled":false}')
        with self.assertRaisesRegex(ValueError, "explicit review"):
            migration.migrate(self.root)

    def test_unrecognized_yaml_is_not_partially_migrated(self):
        original = "version: 2\nupdates: [{package-ecosystem: github-actions}]\n"
        self.dependabot.write_text(original)
        with self.assertRaisesRegex(ValueError, "formatting"):
            migration.migrate(self.root)
        self.assertEqual(self.dependabot.read_text(), original)
        self.assertFalse((self.root / "renovate.json").exists())


class CoupledWorkflowTests(unittest.TestCase):
    def test_upload_and_init_in_different_workflows_cannot_diverge(self):
        workflows = {
            name: {
                "jobs": {
                    "scan": {
                        "steps": [{"uses": f"github/codeql-action/{action}@{pin * 40}"}]
                    }
                }
            }
            for name, action, pin in [
                ("codeql.yml", "init", "a"),
                ("trivy.yml", "upload-sarif", "b"),
            ]
        }
        with self.assertRaisesRegex(ValueError, "across workflows"):
            contracts.validate_codeql(workflows)

    def test_all_codeql_components_can_upgrade_together(self):
        contracts.validate_codeql(
            {
                "ci.yml": {
                    "jobs": {
                        "scan": {
                            "steps": [
                                {
                                    "uses": "github/codeql-action/"
                                    + action
                                    + "@"
                                    + "c" * 40
                                }
                                for action in [
                                    "init",
                                    "autobuild",
                                    "analyze",
                                    "upload-sarif",
                                ]
                            ]
                        }
                    }
                }
            }
        )

    def test_generated_workflows_keep_renovate_version_comments(self):
        workflow = installer.dump(
            {
                "jobs": {
                    "build": {
                        "steps": [
                            {"uses": installer.CHECKOUT},
                            {"uses": installer.UPLOAD},
                        ]
                    }
                }
            }
        )
        pins = {item["packageName"]: item for item in installer.ACTION_PINS}
        for name in ["actions/checkout", "actions/upload-artifact"]:
            pin = pins[name]
            self.assertIn(name + "@" + pin["digest"] + " # " + pin["version"], workflow)
        parsed = yaml.safe_load(workflow)
        self.assertEqual(
            parsed["jobs"]["build"]["steps"][0]["uses"], installer.CHECKOUT
        )

    def test_workflow_update_without_generator_update_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / ".github/workflows").mkdir(parents=True)
            (directory / ".github/action-pins.json").write_text(
                json.dumps(installer.ACTION_PINS)
            )
            workflow = installer.dump(
                {
                    "jobs": {
                        "test": {"steps": [{"uses": "actions/checkout@" + "a" * 40}]}
                    }
                }
            )
            (directory / ".github/workflows/quality-gate.yml").write_text(workflow)
            with self.assertRaisesRegex(ValueError, "differs from generator"):
                contracts.validate_generator_pins(
                    directory, {"quality-gate.yml": yaml.safe_load(workflow)}
                )

    def test_matching_mutable_generator_and_workflow_refs_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / ".github/workflows").mkdir(parents=True)
            for digest in ("main", "v7", "a" * 7, "g" * 40):
                with self.subTest(digest=digest):
                    (directory / ".github/action-pins.json").write_text(
                        json.dumps(
                            [{"packageName": "actions/checkout", "digest": digest}]
                        )
                    )
                    workflow = installer.dump(
                        {
                            "jobs": {
                                "test": {
                                    "steps": [{"uses": "actions/checkout@" + digest}]
                                }
                            }
                        }
                    )
                    (directory / ".github/workflows/quality-gate.yml").write_text(
                        workflow
                    )
                    with self.assertRaisesRegex(ValueError, "full commit SHAs"):
                        contracts.validate_generator_pins(
                            directory, {"quality-gate.yml": yaml.safe_load(workflow)}
                        )


if __name__ == "__main__":
    unittest.main()
