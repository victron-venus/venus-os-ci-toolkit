"""Resolve source-bound producer attempts without network or consumer execution."""

import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import coverage_reports as reports  # noqa: E402

NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def context(*, fork=False):
    env = {
        "COVERAGE_ARTIFACT": "coverage-python",
        "COVERAGE_FILE": "coverage.xml",
        "COVERAGE_FORMAT": "cobertura",
        "COVERAGE_FLAGS": "python",
        "COVERAGE_MODE": "tokenless" if fork else "oidc",
        "GITHUB_RUN_ATTEMPT": "2",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_REPOSITORY": "example/project",
        "GITHUB_EVENT_NAME": "pull_request",
        "GITHUB_REF": "refs/pull/42/merge",
        "GITHUB_SHA": "a" * 40,
        "GITHUB_TOKEN": "synthetic-read-token",
    }
    event = {
        "repository": {"id": 100, "full_name": "example/project", "private": False},
        "pull_request": {
            "number": 42,
            "base": {"sha": "c" * 40, "repo": {"full_name": "example/project"}},
            "head": {
                "sha": "b" * 40,
                "repo": {
                    "id": 200 if fork else 100,
                    "full_name": "other/project" if fork else "example/project",
                },
            },
        },
    }
    return env, event


def artifact(attempt=1, *, identifier=None, fork=False):
    return {
        "id": identifier or (1000 + attempt),
        "name": f"coverage-python-{attempt}",
        "expired": False,
        "expires_at": "2099-01-01T00:00:00Z",
        "digest": "sha256:" + "d" * 64,
        "workflow_run": {
            "id": 12345,
            "repository_id": 100,
            "head_repository_id": 200 if fork else 100,
            "head_sha": "b" * 40,
        },
    }


class ProducerAttemptTests(unittest.TestCase):
    def select(self, records, *, fork=False):
        return reports.select_artifact(*context(fork=fork), records, now=NOW)

    def test_producer_one_is_available_to_uploader_two(self):
        result = self.select([artifact(1)])
        self.assertEqual(result["producer_attempt"], 1)
        self.assertEqual(result["uploader_attempt"], 2)
        self.assertEqual(result["artifact_id"], 1001)
        self.assertEqual(result["head_sha"], "b" * 40)
        self.assertEqual(result["tested_sha"], "a" * 40)
        self.assertEqual(result["event_base_sha"], "c" * 40)

    def test_prefix_sharing_producers_select_only_their_full_base(self):
        records = [
            artifact(1),
            artifact(2, identifier=2002) | {"name": "coverage-python-unit-2"},
            artifact(2, identifier=3002) | {"name": "coverage-python-2-2"},
        ]
        for inventory in (records, list(reversed(records))):
            with self.subTest(inventory=inventory):
                self.assertEqual(self.select(inventory)["artifact_id"], 1001)
                env, event = context()
                for base, expected in (
                    ("coverage-python-unit", 2002),
                    ("coverage-python-2", 3002),
                ):
                    env["COVERAGE_ARTIFACT"] = base
                    selected = reports.select_artifact(env, event, inventory, now=NOW)
                    self.assertEqual(selected["artifact_id"], expected)
                    self.assertEqual(selected["producer_attempt"], 2)
        with self.assertRaisesRegex(ValueError, "Malformed producer attempt"):
            self.select(records + [artifact(2) | {"name": "coverage-python-02"}])
        with self.assertRaisesRegex(ValueError, "duplicate coverage artifact ID"):
            self.select(records + [artifact(1) | {"name": "coverage-python-unit-1"}])

    def test_newest_available_attempt_wins_without_retesting_claim(self):
        result = self.select([artifact(1), artifact(2)])
        self.assertEqual(result["artifact_id"], 1002)
        self.assertNotIn("producer_passed", result)

    def test_newer_failed_producer_without_artifact_does_not_invent_receipt(self):
        env, event = context()
        env["GITHUB_RUN_ATTEMPT"] = "3"
        result = reports.select_artifact(env, event, [artifact(1)], now=NOW)
        self.assertEqual(
            (result["producer_attempt"], result["uploader_attempt"]), (1, 3)
        )
        self.assertNotIn("qualification", result)

    def test_future_attempt_is_never_selected(self):
        self.assertEqual(self.select([artifact(1), artifact(3)])["producer_attempt"], 1)
        with self.assertRaises(ValueError):
            self.select([artifact(3)])

    def test_expired_wronghead_malformed_newest_never_falls_back(self):
        mutations = [
            {"expired": True},
            {"expired": 0},
            {"expires_at": "2020-01-01T00:00:00Z"},
            {"expires_at": "not-a-time"},
            {"expires_at": "2099-01-01T00:00:00"},
            {"digest": "bad"},
            {"digest": None},
            {"workflow_run": {}},
        ]
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.select([artifact(1), artifact(2) | changes])
        for key, value in [
            ("head_sha", "e" * 40),
            ("id", 54321),
            ("repository_id", 999),
            ("head_repository_id", 200),
            ("id", True),
        ]:
            newest = artifact(2)
            newest["workflow_run"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.select([artifact(1), newest])

    def test_duplicate_ids_and_newest_names_fail_closed(self):
        for records in (
            [artifact(1), artifact(2, identifier=1001)],
            [artifact(2), artifact(2, identifier=2002)],
            [artifact(2), artifact(1, identifier=1002) | {"name": "unrelated"}],
        ):
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.select(records)

    def test_malformed_matching_names_fail_instead_of_using_older(self):
        for name in [
            "coverage-python-0",
            "coverage-python-02",
            "coverage-python-2.0",
            "coverage-python-2\n",
            "coverage-python-../../2",
        ]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.select([artifact(1), artifact(2) | {"name": name}])

    def test_missing_inventory_or_invalid_identifiers_fail(self):
        for records in [
            [],
            [None],
            [artifact(2) | {"id": True}],
            [artifact(2) | {"id": -1}],
            [artifact(2) | {"name": None}],
        ]:
            with self.subTest(records=records), self.assertRaises(ValueError):
                self.select(records)

    def test_fork_binding_uses_head_repository_without_oidc(self):
        result = self.select([artifact(1, fork=True)], fork=True)
        self.assertEqual(result["head_sha"], "b" * 40)
        with self.assertRaises(ValueError):
            self.select([artifact(1)], fork=True)

    def test_original_event_ids_and_base_are_required(self):
        for target, key, value in [
            ("repository", "id", True),
            ("repository", "id", None),
            ("base", "sha", "bad"),
            ("head_repo", "id", 0),
        ]:
            env, event = context()
            value_target = (
                event["repository"]
                if target == "repository"
                else event["pull_request"]["head"]["repo"]
                if target == "head_repo"
                else event["pull_request"]["base"]
            )
            value_target[key] = value
            with self.subTest(target=target), self.assertRaises(ValueError):
                reports.select_artifact(env, event, [artifact(1)], now=NOW)

    def test_newest_report_schema_failure_never_selects_older_report(self):
        selected = self.select([artifact(1), artifact(2)])
        self.assertEqual(selected["artifact_id"], 1002)
        with tempfile.TemporaryDirectory() as directory:
            for raw in ["", "<coverage/>"]:
                (Path(directory) / "coverage.xml").write_text(raw)
                with self.assertRaises(ValueError):
                    reports.validate_report(directory, "coverage.xml", "cobertura")
            with self.assertRaises(ValueError):
                reports.safe_path("../coverage.xml")

    def test_selection_cli_records_older_producer_without_claiming_new_ci(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, event = context()
            env.update(
                RUNNER_TEMP=str(root),
                GITHUB_EVENT_PATH=str(root / "event.json"),
                GITHUB_OUTPUT=str(root / "output"),
                GITHUB_STEP_SUMMARY=str(root / "summary"),
            )
            (root / "event.json").write_text(json.dumps(event))
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(sys, "argv", ["coverage", "artifact"]),
                patch.object(reports, "artifact_inventory", return_value=[artifact(1)]),
            ):
                reports.main()
            self.assertIn("producer_attempt=1\n", (root / "output").read_text())
            self.assertIn("does not retest", (root / "summary").read_text())
            self.assertEqual(
                json.loads((root / "coverage-selection.json").read_text())[
                    "tested_sha"
                ],
                "a" * 40,
            )

    def test_api_error_publishes_no_selection_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env, event = context()
            env.update(
                RUNNER_TEMP=str(root),
                GITHUB_EVENT_PATH=str(root / "event.json"),
                GITHUB_OUTPUT=str(root / "output"),
                GITHUB_STEP_SUMMARY=str(root / "summary"),
            )
            (root / "event.json").write_text(json.dumps(event))
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(sys, "argv", ["coverage", "artifact"]),
                patch.object(
                    reports,
                    "github_artifact_json",
                    side_effect=ValueError("API failed"),
                ),
                self.assertRaises(ValueError),
            ):
                reports.main()
            self.assertFalse((root / "output").exists())
            self.assertFalse((root / "coverage-selection.json").exists())


class ArtifactTransportTests(unittest.TestCase):
    def test_dot_github_repository_remains_supported_without_traversal(self):
        env, _ = context()
        seen = []

        def fetch(path):
            seen.append(path)
            return {"total_count": 0, "artifacts": []}

        reports.artifact_inventory(
            env | {"GITHUB_REPOSITORY": "example/.github"}, fetch
        )
        self.assertIn("repos/example/.github/", seen[0])
        for repository in [
            "example/..",
            "example/.",
            "example/../other",
            "bad host/repo",
        ]:
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                reports.artifact_inventory(
                    env | {"GITHUB_REPOSITORY": repository}, fetch
                )

    def test_bounded_complete_pagination(self):
        env, _ = context()
        calls = []
        pages = [
            {
                "total_count": 101,
                "artifacts": [artifact(1, identifier=i + 1) for i in range(100)],
            },
            {"total_count": 101, "artifacts": [artifact(2)]},
        ]

        def fetch(path):
            calls.append(path)
            return pages.pop(0)

        records = reports.artifact_inventory(env, fetch)
        self.assertEqual(len(records), 101)
        self.assertEqual(len(calls), 2)
        self.assertTrue(
            all("runs/12345/artifacts?per_page=100&page=" in path for path in calls)
        )

    def test_incomplete_oversized_or_changing_pages_fail_closed(self):
        env, _ = context()
        for payload in [
            {"total_count": 1001, "artifacts": []},
            {"total_count": 2, "artifacts": [artifact(1)]},
            {"total_count": True, "artifacts": []},
            {"total_count": 0, "artifacts": [artifact(1)]},
        ]:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                reports.artifact_inventory(env, lambda path: payload)
        pages = [
            {"total_count": 101, "artifacts": [artifact(1)] * 100},
            {"total_count": 102, "artifacts": [artifact(2)]},
        ]
        with self.assertRaises(ValueError):
            reports.artifact_inventory(env, lambda path: pages.pop(0))

    def test_https_request_has_no_redirect_or_token_disclosure(self):
        env, _ = context()
        observed = []

        class Response(io.BytesIO):
            status = 200

        class Opener:
            def open(self, request, timeout):
                observed.append(
                    (request.full_url, request.get_header("Authorization"), timeout)
                )
                return Response(b'{"total_count":0,"artifacts":[]}')

        def build(*handlers):
            self.assertEqual(
                len(handlers), 1
            )  # Do not override standard proxy support.
            redirect = next(
                h for h in handlers if isinstance(h, reports.HTTPRedirectHandler)
            )
            with self.assertRaises(ValueError):
                redirect.redirect_request(
                    None, None, 302, "redirect", {}, "https://other.invalid"
                )
            return Opener()

        with patch.object(reports, "build_opener", side_effect=build):
            self.assertEqual(
                reports.github_artifact_json(
                    env, "repos/example/project/actions/runs/12345/artifacts"
                )["total_count"],
                0,
            )
        self.assertEqual(
            observed,
            [
                (
                    "https://api.github.com/repos/example/project/actions/runs/12345/artifacts",
                    "Bearer synthetic-read-token",
                    30,
                )
            ],
        )

        class Failing:
            def open(self, *args, **kwargs):
                raise URLError("synthetic-read-token must not escape")

        with (
            patch.object(reports, "build_opener", return_value=Failing()),
            self.assertRaisesRegex(ValueError, "inventory request failed") as raised,
        ):
            reports.github_artifact_json(env, "fixed")
        self.assertNotIn(env["GITHUB_TOKEN"], str(raised.exception))

    def test_metadata_body_bound_and_invalid_origin_or_token(self):
        env, _ = context()
        for changes in [
            {"GITHUB_API_URL": "http://api.github.com"},
            {"GITHUB_API_URL": "https://user@api.github.com"},
            {"GITHUB_API_URL": "https://api.github.com?token=secret"},
            {"GITHUB_TOKEN": ""},
            {"GITHUB_TOKEN": "secret\x00token"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                reports.github_artifact_json(env | changes, "fixed")

        class Response(io.BytesIO):
            status = 200

        class Opener:
            def open(self, *args, **kwargs):
                return Response(b"x" * 5)

        with (
            patch.object(reports, "build_opener", return_value=Opener()),
            patch.object(reports, "MAX_METADATA_BYTES", 4),
            self.assertRaises(ValueError),
        ):
            reports.github_artifact_json(env, "fixed")
