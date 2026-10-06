"""Operator metadata stays external while public defaults and API fences fail closed."""

import copy
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


mode = module("runner_mode")
fleet = module("fleet")


def fixture():
    return json.loads(
        (ROOT / "deploy/arc-ottplay/runner-mode.example.json").read_text()
    )


class OperatorConfigTests(unittest.TestCase):
    def test_reviewed_config_binds_exact_endpoints_and_groups(self):
        data = fixture()
        data["organization"] = "sample-team"
        raw = json.dumps(data).replace("example-org/", "sample-team/")
        mode.configure(json.loads(raw))
        mode.validate_api_request(
            "repos/sample-team/console/actions/variables", "GET", None
        )
        for route in (
            "repos/example-org/console",
            "repos/sample-team/unselected",
            "repos/sample-team/console/actions/secrets",
        ):
            with self.subTest(route=route), self.assertRaises(mode.PreflightError):
                mode.validate_api_request(route, "GET", None)
        self.assertEqual(
            mode.GROUP_POLICIES["ottplay-private-release"]["repositories"],
            ["sample-team/mobile"],
        )

    def test_invalid_or_broadened_configuration_cannot_reach_api(self):
        changes = [
            lambda d: d.update(schema=True),
            lambda d: d.update(organization="../another-org"),
            lambda d: d.update(repositories={}),
            lambda d: d["repositories"].update(
                {"--hostname=other": d["repositories"].pop("console")}
            ),
            lambda d: d["repositories"]["console"].update(
                repository="other-org/console"
            ),
            lambda d: d["repositories"]["console"].update(
                repository="example-org/mobile"
            ),
            lambda d: d["repositories"]["console"].update(pools={}),
            lambda d: d["repositories"]["mobile"]["pools"].update(
                {"smoke-release": ["runner", "missing"]}
            ),
            lambda d: d["groups"][0].update(allows_public_repositories=True),
            lambda d: d["groups"][0].update(visibility="all"),
            lambda d: d["groups"][0].update(repositories=["example-org/unselected"]),
            lambda d: d["groups"][1].update(
                restricted_to_workflows=False, selected_workflows=[]
            ),
            lambda d: d["groups"][1].update(
                selected_workflows=[
                    "example-org/mobile/.github/workflows/release.yml@refs/heads/topic"
                ]
            ),
            lambda d: d["groups"].append(copy.deepcopy(d["groups"][0])),
            lambda d: d.update(extra="not part of the operator contract"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "operator.json"
            for change in changes:
                data = fixture()
                change(data)
                path.write_text(json.dumps(data))
                with (
                    self.subTest(change=change),
                    patch.object(mode, "api") as api,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        mode.main(
                            [
                                "--config",
                                str(path),
                                "--repo",
                                "console",
                                "--mode",
                                "github",
                                "--apply",
                            ]
                        ),
                        2,
                    )
                    api.assert_not_called()

    def test_missing_symlink_or_unknown_selection_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "valid.json"
            valid.write_text(json.dumps(fixture()))
            linked = Path(tmp) / "link.json"
            linked.symlink_to(valid)
            for path, alias in [
                (Path(tmp) / "missing.json", "console"),
                (linked, "console"),
                (valid, "unknown"),
            ]:
                with (
                    self.subTest(path=path, alias=alias),
                    patch.object(mode, "api") as api,
                    redirect_stderr(io.StringIO()),
                ):
                    self.assertEqual(
                        mode.main(["--config", str(path), "--repo", alias]), 2
                    )
                    api.assert_not_called()

    def test_regular_external_cli_file_and_unsafe_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = root / "operator.json"
            valid.write_text(json.dumps(fixture()))
            valid.chmod(0o600)
            mode.load_config(valid)
            self.assertEqual(mode.ORG, "example-org")
            fifo = root / "pipe"
            os.mkfifo(fifo)
            for path in (fifo, root):
                with (
                    self.subTest(path=path),
                    self.assertRaises((OSError, mode.PreflightError)),
                ):
                    mode.load_config(path)
            valid.chmod(0o622)
            with self.assertRaises(mode.PreflightError):
                mode.load_config(valid)
            valid.chmod(0o600)
            with patch.object(mode.os, "getuid", return_value=os.getuid() + 1):
                with self.assertRaises(mode.PreflightError):
                    mode.load_config(valid)

    def test_duplicate_json_keys_cannot_replace_an_access_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "duplicate.json"
            raw = json.dumps(fixture()).replace(
                '"organization": "example-org"',
                '"organization": "discarded", "organization": "example-org"',
            )
            path.write_text(raw)
            with patch.object(mode, "api") as api, redirect_stderr(io.StringIO()):
                self.assertEqual(
                    mode.main(["--config", str(path), "--repo", "console"]), 2
                )
                api.assert_not_called()

    def test_provisioned_user_inventory_preserves_default_and_explicit_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            user = Path(tmp) / "fleet.json"
            with patch.object(fleet, "OPERATOR_INVENTORY", user):
                self.assertEqual(
                    fleet.parse_args(["status"]).inventory, ROOT / "fleet.json"
                )
                user.write_text('{"schema":1,"repositories":[]}')
                self.assertEqual(fleet.parse_args(["status"]).inventory, user)
                self.assertEqual(
                    fleet.parse_args(
                        ["status", "--inventory", str(ROOT / "fleet.json")]
                    ).inventory,
                    ROOT / "fleet.json",
                )

    def test_unknown_fleet_selection_does_not_silently_succeed(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "fleet.json"
            source.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "repositories": [
                            {
                                "repository": "example-org/console",
                                "directory": "console",
                            }
                        ],
                    }
                )
            )
            args = fleet.parse_args(
                ["status", "--inventory", str(source), "--repo", "example-org/unknown"]
            )
            with (
                patch.object(fleet, "parse_args", return_value=args),
                patch.object(fleet, "process_repository") as process,
                redirect_stdout(io.StringIO()),
            ):
                with self.assertRaisesRegex(ValueError, "Unknown repository"):
                    fleet.main()
                process.assert_not_called()

    def test_external_fleet_rejects_escaped_duplicate_or_excluded_targets(self):
        valid = {
            "schema": 1,
            "repositories": [
                {"repository": "example-org/console", "directory": "console"}
            ],
        }
        cases = []
        for directory in ("../outside", "/tmp/elsewhere", ".", ".."):
            changed = copy.deepcopy(valid)
            changed["repositories"][0]["directory"] = directory
            cases.append(changed)
        duplicate = copy.deepcopy(valid)
        duplicate["repositories"].append(copy.deepcopy(duplicate["repositories"][0]))
        cases.append(duplicate)
        excluded = copy.deepcopy(valid)
        excluded["repositories"][0]["excluded_reason"] = "historical"
        cases.append(excluded)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fleet.json"
            for manifest in cases:
                path.write_text(json.dumps(manifest))
                with self.subTest(manifest=manifest), self.assertRaises(ValueError):
                    fleet.load_inventory(path, ["example-org/console"])

    def test_checked_in_fleet_explicitly_contains_public_entries_only(self):
        inventory = json.loads((ROOT / "fleet.json").read_text())
        self.assertTrue(inventory["repositories"])
        self.assertTrue(
            all(row.get("visibility") == "public" for row in inventory["repositories"])
        )
