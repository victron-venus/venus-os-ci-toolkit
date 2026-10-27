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
    spec = importlib.util.spec_from_file_location(
        "scripts." + name, ROOT / "scripts" / f"{name}.py"
    )
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
            patch.object(credentials, "validate_signing_key") as validate_key,
            redirect_stdout(output),
        ):
            response = urlopen.return_value.__enter__.return_value
            response.headers = (
                {} if scope_header is None else {"X-OAuth-Scopes": scope_header}
            )
            credentials.main()
            urlopen.assert_called_once()
            validate_key.assert_called_once_with("fixture-only")
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


class ActionMetadataMigrationTests(unittest.TestCase):
    setUp = DependencyMigrationTests.setUp

    def workflow(self, body):
        root = self.root / ".github/workflows"
        root.mkdir(exist_ok=True)
        path = root / "ci.yml"
        path.write_text(body)
        return path

    def test_actual_job_and_quoted_step_refs_get_metadata_and_are_idempotent(self):
        digest = "a" * 40
        toolkit = "victron-venus/venus-os-ci-toolkit/.github/workflows/ci.yml"
        original = (
            f"jobs:\n  shared:\n    uses: {toolkit}@{digest}\n"
            "  local:\n    runs-on: ubuntu-latest\n    steps:\n"
            f"      - uses: 'example/action@{digest}'\n"
            "      - run: |\n"
            f"          uses: unknown/action@{digest}\n"
            f"        env:\n          uses: unknown/env@{digest}\n"
        )
        path = self.workflow(original)
        versions = {"example/action": {digest: "v1.2.3"}}
        migration.migrate(self.root, versions)
        result = path.read_text()
        self.assertEqual(yaml.safe_load(result), yaml.safe_load(original))
        self.assertIn(f"uses: {toolkit}@{digest} # main", result)
        self.assertIn(f"uses: 'example/action@{digest}' # v1.2.3", result)
        self.assertIn(f"uses: unknown/action@{digest}\n", result)
        migration.migrate(self.root, versions)
        self.assertEqual(path.read_text(), result)

    def test_unknown_ref_fails_before_writing_any_migration_file(self):
        digest = "b" * 40
        original = (
            f"jobs:\n  build:\n    steps:\n      - uses: unknown/action@{digest}\n"
        )
        path = self.workflow(original)
        with self.assertRaisesRegex(
            ValueError, "Missing verified action-version metadata"
        ):
            migration.migrate(self.root)
        self.assertEqual(path.read_text(), original)
        self.assertFalse(self.dependabot.exists())
        self.assertFalse((self.root / "renovate.json").exists())

    def test_manifest_and_verified_branch_ref_are_used_without_changing_digest(self):
        digest = "c" * 40
        (self.root / ".github/action-pins.json").write_text(
            json.dumps(
                [
                    {
                        "packageName": "example/first",
                        "digest": digest,
                        "version": "v2.0.1",
                    }
                ]
            )
        )
        path = self.workflow(
            f"jobs:\n  build:\n    steps:\n      - uses: example/first@{digest}\n"
            f"      - uses: example/second@{digest}\n"
            f"      - uses: example/third@{digest} # v3.0.1; keep reason\n"
        )
        migration.migrate(self.root, {"example/second": {digest: "master"}})
        self.assertIn(f"example/first@{digest} # v2.0.1", path.read_text())
        self.assertIn(f"example/second@{digest} # master", path.read_text())
        self.assertIn(f"example/third@{digest} # v3.0.1; keep reason", path.read_text())

    def test_flow_style_requires_manual_review_without_partial_write(self):
        original = "jobs: {build: {steps: [{uses: example/action@" + "a" * 40 + "}]}}\n"
        path = self.workflow(original)
        with self.assertRaisesRegex(ValueError, "Ambiguous action reference"):
            migration.migrate(self.root)
        self.assertEqual(path.read_text(), original)
        self.assertFalse(self.dependabot.exists())

    def test_untrusted_mapping_cannot_insert_workflow_lines(self):
        with self.assertRaisesRegex(ValueError, "verified SHA-to-release-ref"):
            migration.migrate(self.root, {"example/action": {"a" * 40: "v1\nfoo: bar"}})
        self.assertFalse(self.dependabot.exists())

    def test_prose_comment_gets_verified_ref_without_losing_the_note(self):
        digest = "a" * 40
        original = f"jobs:\n  ci:\n    steps:\n      - uses: example/action@{digest} # kept for compatibility\n"
        path = self.workflow(original)
        versions = {"example/action": {digest: "v1.2.3"}}
        migration.migrate(self.root, versions)
        self.assertIn("# v1.2.3; kept for compatibility", path.read_text())
        self.assertEqual(yaml.safe_load(original), yaml.safe_load(path.read_text()))
        self.assertFalse(migration.annotated_workflows(self.root, versions))

    def test_prose_comment_without_verified_ref_does_not_disable_dependabot(self):
        original = (
            "jobs:\n  ci:\n    steps:\n      - uses: example/action@"
            + "a" * 40
            + " # kept for compatibility\n"
        )
        path = self.workflow(original)
        with self.assertRaisesRegex(ValueError, "Missing verified action-version"):
            migration.migrate(self.root)
        self.assertEqual(path.read_text(), original)
        self.assertFalse(self.dependabot.exists())


class CoupledWorkflowTests(unittest.TestCase):
    def test_repository_allowlist_keeps_shared_custom_managers_enabled(self):
        preset = json.loads((ROOT / "renovate-ci.json").read_text())
        repository = json.loads((ROOT / "renovate.json").read_text())
        # Renovate replaces enabledManagers rather than merging the arrays.
        enabled = repository.get("enabledManagers", preset["enabledManagers"])
        for manager in preset["customManagers"]:
            self.assertIn("custom." + manager["customType"], enabled)

    @staticmethod
    def codeql_source(action, comment, quote=""):
        reference = quote + f"github/codeql-action/{action}@" + "a" * 40 + quote
        return f"jobs:\n  scan:\n    steps:\n      - uses: {reference}{comment}\n"

    def test_codeql_update_metadata_covers_every_component_and_quoted_uses(self):
        for quote in ("", "'", '"'):
            sources = {
                f"{action}.yml": self.codeql_source(action, " # v4.38.3", quote)
                for action in ("init", "autobuild", "analyze", "upload-sarif")
            }
            with self.subTest(quote=quote):
                contracts.validate_codeql_update_metadata(sources)
                for filename in sources:
                    with (
                        self.subTest(filename=filename),
                        self.assertRaisesRegex(
                            ValueError, "inline full release comment"
                        ),
                    ):
                        contracts.validate_codeql_update_metadata(
                            {
                                **sources,
                                filename: sources[filename].replace(" # v4.38.3", ""),
                            }
                        )

    def test_codeql_partial_and_disagreeing_version_comments_are_rejected(self):
        for comment in (" # v4", " # maintained manually", "\n      # v4.38.3"):
            with (
                self.subTest(comment=comment),
                self.assertRaisesRegex(ValueError, "inline full release comment"),
            ):
                contracts.validate_codeql_update_metadata(
                    {"upload.yml": self.codeql_source("upload-sarif", comment)}
                )
        with self.assertRaisesRegex(ValueError, "same release"):
            contracts.validate_codeql_update_metadata(
                {
                    "ci.yml": self.codeql_source("init", " # v4.38.3"),
                    "upload.yml": self.codeql_source("upload-sarif", " # v4.38.2"),
                }
            )

    def test_codeql_metadata_check_ignores_shell_literals_and_accepts_notes(self):
        source = self.codeql_source("init", " # v4.38.3 keep upstream release")
        source += (
            "      - run: |\n          echo 'uses: github/codeql-action/init@"
            + "b" * 40
            + "'\n"
        )
        contracts.validate_codeql_update_metadata({"ci.yml": source})

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
            next(
                step["uses"]
                for step in parsed["jobs"]["build"]["steps"]
                if step["uses"].startswith("actions/checkout@")
            ),
            installer.CHECKOUT,
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
            parsed_workflows = {"quality-gate.yml": yaml.safe_load(workflow)}
            with self.assertRaisesRegex(ValueError, "differs from generator"):
                contracts.validate_generator_pins(directory, parsed_workflows)

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
                    parsed_workflows = {"quality-gate.yml": yaml.safe_load(workflow)}
                    with self.assertRaisesRegex(ValueError, "full commit SHAs"):
                        contracts.validate_generator_pins(directory, parsed_workflows)


if __name__ == "__main__":
    unittest.main()
