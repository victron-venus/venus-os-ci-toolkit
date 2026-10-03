"""Regression coverage for atomic CI updates and the Dependabot migration."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

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
            self.dependabot.read_text(), "version: 2\nupdates:\n" + application
        )
        migration.migrate(self.root)
        self.assertEqual(
            self.dependabot.read_text(), "version: 2\nupdates:\n" + application
        )

    def test_removes_empty_dependabot_config_and_is_idempotent(self):
        self.dependabot.write_text(
            "version: 2\nupdates:\n"
            "  - package-ecosystem: github-actions\n"
            "    directory: /\n    schedule: {interval: weekly}\n"
        )
        migration.migrate(self.root)
        self.assertFalse(self.dependabot.exists())
        migration.migrate(self.root)
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


if __name__ == "__main__":
    unittest.main()
