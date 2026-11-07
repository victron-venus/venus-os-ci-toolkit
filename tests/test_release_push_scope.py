"""Prove the narrow main-push exception without weakening release validation."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.dict(sys.modules, {"change_scope": load("change_scope")}):
    SCOPE = load("release_push_scope")

BASE = "a" * 40
HEAD = "b" * 40
PATHS = [
    "tests/helpers/aspect-readiness-diagnostics.cjs",
    "tests/test_hls_aspect_fixture.cjs",
    "tests/test_native_local_media_siblings.py",
]


def event(before=BASE, after=HEAD):
    return {
        "ref": "refs/heads/main",
        "repository": {"default_branch": "main"},
        "before": before,
        "after": after,
        "created": False,
        "deleted": False,
        "forced": False,
    }


def record(
    path, status="M", old_mode="100644", new_mode="100644", old_hash=BASE, new_hash=HEAD
):
    return f":{old_mode} {new_mode} {old_hash} {new_hash} {status}\0{path}\0".encode()


class ClassifierTests(unittest.TestCase):
    def test_helper_supports_existing_package_import_callers(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "from scripts import release_push_scope; assert release_push_scope.decision('not-main-push')['test_only'] is False",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def classify_raw(self, raw, payload=None, kind="push", runner=HEAD):
        def git(_repo, operation, revision=""):
            if operation == "shallow":
                return b"false\n"
            if operation == "commit-type":
                return b"commit\n"
            self.assertEqual(operation, "diff")
            self.assertEqual(revision, f"{BASE}..{HEAD}")
            return raw

        with patch.object(SCOPE.change_scope, "git", side_effect=git):
            return SCOPE.classify("unused", kind, payload or event(), runner, PATHS)

    def test_exact_three_file_profile_and_each_regular_addition_or_modification(self):
        result = self.classify_raw(b"".join(record(path) for path in PATHS))
        self.assertTrue(result["test_only"])
        self.assertEqual(result["test_paths"], PATHS)
        self.assertEqual(result["before"], BASE)
        self.assertEqual(result["after"], HEAD)
        for path in PATHS:
            for raw in (record(path), record(path, "A", "000000", old_hash="0" * 40)):
                with self.subTest(path=path, raw=raw):
                    self.assertTrue(self.classify_raw(raw)["test_only"])

    def test_mixed_unknown_production_config_dependency_and_documentation_stay_full(
        self,
    ):
        for path in (
            "src/channels.ts",
            "package-lock.json",
            "Cargo.lock",
            "README.md",
            "docs/example.md",
            "CHANGELOG.md",
            ".github/workflows/ci.yml",
            "scripts/release_push_scope.py",
            "tests/new-test.py",
            "tests/fixtures/media.mp4",
            "tests/helpers/other.cjs",
            "../tests/test_hls_aspect_fixture.cjs",
        ):
            for raw in (record(path), record(PATHS[0]) + record(path)):
                with self.subTest(path=path):
                    self.assertFalse(self.classify_raw(raw)["test_only"])

    def test_nonregular_deletion_rename_mode_change_and_invalid_headers_stay_full(self):
        cases = [
            record(PATHS[0], "D", new_mode="000000", new_hash="0" * 40),
            record(PATHS[0], "R100"),
            record(PATHS[0], "T"),
            record(PATHS[0], old_mode="100755"),
            record(PATHS[0], new_mode="100755"),
            record(PATHS[0], new_mode="120000"),
            record(PATHS[0], new_mode="160000"),
            record(PATHS[0], old_hash="0" * 40),
            record(PATHS[0], new_hash="0" * 40),
            record(PATHS[0], new_hash=BASE),
            record(PATHS[0], old_hash="-option"),
            record(PATHS[0], "A", "000000"),
            record(PATHS[0]) + record(PATHS[0]),
            record(PATHS[0]).replace(b":100644", b"100644"),
            b"invalid\0path\0",
            b"header\0",
            b"",
            b"\xff\0\xff\0",
            record(PATHS[0])[:-1],
            record(PATHS[0]) + b"unterminated",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertFalse(self.classify_raw(raw)["test_only"])
        with patch.object(SCOPE.change_scope, "MAX_DIFF_BYTES", 10):
            self.assertFalse(self.classify_raw(record(PATHS[0]))["test_only"])

    def test_nonpush_events_never_read_git_or_skip(self):
        with patch.object(SCOPE.change_scope, "git") as git:
            for kind in (
                "workflow_dispatch",
                "schedule",
                "pull_request",
                "merge_group",
                "",
                "other",
            ):
                with self.subTest(kind=kind):
                    self.assertFalse(
                        SCOPE.classify("unused", kind, event(), HEAD, PATHS)[
                            "test_only"
                        ]
                    )
            git.assert_not_called()

    def test_nonmain_created_deleted_forced_or_incomplete_events_stay_full(self):
        cases = [
            None,
            [],
            {},
            {**event(), "ref": "refs/heads/topic"},
            {**event(), "repository": {"default_branch": "other"}},
            {**event(), "repository": None},
        ]
        for flag in ("created", "deleted", "forced"):
            for value in (True, None, "false", 0):
                cases.append({**event(), flag: value})
            missing = event()
            del missing[flag]
            cases.append(missing)
        with patch.object(SCOPE.change_scope, "git") as git:
            for payload in cases:
                self.assertFalse(
                    SCOPE.classify("unused", "push", payload, HEAD, PATHS)["test_only"]
                )
            git.assert_not_called()

    def test_invalid_or_stale_revisions_stay_full(self):
        with patch.object(SCOPE.change_scope, "git") as git:
            for field in ("before", "after"):
                for value in ("0" * 40, "abc", "A" * 40, "--help", None, 42):
                    self.assertFalse(
                        SCOPE.classify(
                            "unused", "push", {**event(), field: value}, HEAD, PATHS
                        )["test_only"]
                    )
            for runner in ("", BASE, "--help"):
                self.assertFalse(
                    SCOPE.classify("unused", "push", event(), runner, PATHS)[
                        "test_only"
                    ]
                )
            git.assert_not_called()

    def test_missing_objects_shallow_history_and_git_errors_stay_full(self):
        results = (
            [b"true\n"],
            [b"false\n", b"blob\n"],
            [b"false\n", b"commit\n", subprocess.CalledProcessError(128, "git")],
            [OSError("missing Git")],
            [subprocess.TimeoutExpired("git", 30)],
        )
        for calls in results:
            with (
                self.subTest(calls=calls),
                patch.object(SCOPE.change_scope, "git", side_effect=calls),
            ):
                self.assertFalse(
                    SCOPE.classify("unused", "push", event(), HEAD, PATHS)["test_only"]
                )

    def test_cli_retains_one_bounded_receipt_and_exact_output_without_event_secrets(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = event()
            payload["commits"] = [{"message": "secret https://private.invalid/token"}]
            (root / "event.json").write_text(json.dumps(payload))
            env = {
                "GITHUB_EVENT_PATH": str(root / "event.json"),
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_SHA": HEAD,
                "GITHUB_OUTPUT": str(root / "outputs"),
            }
            raw = b"".join(record(path) for path in PATHS)
            with (
                patch.dict(os.environ, env, clear=True),
                patch.object(
                    SCOPE.change_scope,
                    "git",
                    side_effect=[b"false", b"commit", b"commit", raw],
                ),
                contextlib.redirect_stdout(io.StringIO()) as stdout,
            ):
                self.assertEqual(
                    SCOPE.main(
                        [
                            "--output",
                            str(root / "receipt.json"),
                            "--test-paths-json",
                            json.dumps(PATHS),
                        ]
                    ),
                    0,
                )
            receipt = (root / "receipt.json").read_text()
            self.assertEqual(stdout.getvalue(), receipt)
            self.assertEqual(json.loads(receipt)["test_paths"], PATHS)
            self.assertEqual(
                (root / "outputs").read_text(),
                "test_only=true\nreason=test-infrastructure-only\n",
            )
            self.assertNotIn("private.invalid", receipt)
            self.assertNotIn("secret", receipt)


class CompleteGitRangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        for path in [*PATHS, "src/channels.ts"]:
            self.write(path, "baseline\n")
        self.base = self.commit("baseline")

    def git(self, *arguments):
        env = {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=self.root,
            env=env,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        ).strip()

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value)

    def commit(self, message):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def test_main_merge_compares_before_to_merge_tip_for_all_three_test_files(self):
        self.git("checkout", "-qb", "test-topic")
        for path in PATHS:
            self.write(path, "extended fixture\n")
            self.commit("extend fixture")
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-qm", "merge tests", "test-topic")
        tip = self.git("rev-parse", "HEAD")
        result = SCOPE.classify(self.root, "push", event(self.base, tip), tip, PATHS)
        self.assertTrue(result["test_only"])
        self.assertEqual(result["test_paths"], PATHS)

    def test_earlier_production_commit_in_same_push_cannot_hide_behind_last_test_commit(
        self,
    ):
        self.write("src/channels.ts", "production change\n")
        earlier = self.commit("change production")
        self.write(PATHS[0], "test-only tail\n")
        tip = self.commit("change test")
        self.assertTrue(
            SCOPE.classify(self.root, "push", event(earlier, tip), tip, PATHS)[
                "test_only"
            ]
        )
        self.assertFalse(
            SCOPE.classify(self.root, "push", event(self.base, tip), tip, PATHS)[
                "test_only"
            ]
        )


if __name__ == "__main__":
    unittest.main()
