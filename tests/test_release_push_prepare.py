"""Prove admission cannot strand an unfinished predecessor's production delivery."""

import copy
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_push_scope as scope  # noqa: E402
import test_versioned_release as fixtures  # noqa: E402

lifecycle = fixtures.lifecycle
rc = fixtures.rc
BEFORE = fixtures.SHA
AFTER = "b" * 40
PATHS = ["tests/test_example.py"]


class PreparationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.policy = self.fixture.policy
        self.policy["change_scope"] = {"automatic_release_test_paths": PATHS}
        (self.fixture.root / ".release-policy.json").write_bytes(
            rc.json_bytes(self.policy)
        )
        self.gh = self.fixture.gh
        self.admission = self.fixture.root / ".release-push-admission.json"
        self.git = self.enterContext(
            patch.object(scope.change_scope, "git", side_effect=self.git_read)
        )

    @staticmethod
    def git_read(_repo, operation, revision=""):
        if operation == "shallow":
            return b"false"
        if operation == "commit-type":
            return b"commit"
        assert operation == "diff" and revision == f"{BEFORE}..{AFTER}"
        return f":100644 100644 {'c' * 40} {'d' * 40} M\0{PATHS[0]}\0".encode()

    def start_push(self, before=BEFORE, kind="push"):
        self.fixture.start_run("beta", 200)
        self.gh.runs[200].update(head_sha=AFTER, event=kind)
        self.gh.default_head = AFTER
        self.gh.jobs[0]["head_sha"] = AFTER
        self.gh.source_policies[AFTER] = self.policy
        self.enterContext(patch.object(rc, "checked_out_sha", return_value=AFTER))
        self.enterContext(
            patch.dict(os.environ, {"GITHUB_EVENT_NAME": kind, "GITHUB_SHA": AFTER})
        )
        Path(os.environ["GITHUB_EVENT_PATH"]).write_text(
            json.dumps(
                {
                    "ref": "refs/heads/main",
                    "repository": {"default_branch": "main"},
                    "before": before,
                    "after": AFTER,
                    "created": False,
                    "deleted": False,
                    "forced": False,
                    "inputs": {"channel": "rc", "expected_sha": AFTER},
                }
            )
        )
        lifecycle.PLAN.unlink(missing_ok=True)

    def prepare(self):
        return lifecycle.prepare(self.fixture.args)

    def assert_full(self):
        result = self.prepare()
        self.assertEqual(result["build"], "true")
        self.assertNotIn("status", result)
        self.assertTrue(lifecycle.PLAN.exists())
        self.assertIn("200", self.gh.ledger["plans"])
        self.assertFalse(self.admission.exists())

    def test_exact_delivered_predecessor_skips_without_allocation_or_publication(self):
        published, _ = self.fixture.qualify_schedule_fixture("beta")
        self.start_push()
        ledger, writes = copy.deepcopy(self.gh.ledger), copy.deepcopy(self.gh.writes)
        ledger_writes = copy.deepcopy(self.gh.ledger_writes)
        result = self.prepare()
        self.assertEqual(
            result,
            {
                "channel": "beta",
                "version": "1.2.3",
                "build": "false",
                "plan_artifact": "",
                "status": "test-only-already-published",
            },
        )
        self.assertEqual(self.gh.ledger, ledger)
        self.assertEqual(self.gh.ledger_writes, ledger_writes)
        self.assertEqual(self.gh.writes, writes)
        self.assertFalse(lifecycle.PLAN.exists())
        admission = json.loads(self.admission.read_bytes())
        self.assertEqual(admission["before"], BEFORE)
        self.assertEqual(admission["after"], AFTER)
        self.assertEqual(admission["published_tag"], published["tag"])
        self.assertEqual(
            set(admission),
            {
                "schema",
                "test_only",
                "reason",
                "before",
                "after",
                "diff_sha256",
                "test_paths",
                "status",
                "published_tag",
            },
        )
        self.assertLess(self.admission.stat().st_size, 1024)
        self.assertNotIn("https:", self.admission.read_text())

    def test_reserved_but_unpublished_predecessor_still_allocates_and_builds(self):
        self.fixture.start_run("beta", 100)
        self.prepare()
        self.start_push()
        self.assert_full()

    def test_missing_predecessor_and_second_test_only_commit_fall_back(self):
        self.fixture.qualify_schedule_fixture("beta")
        self.start_push(before="c" * 40)
        self.git.side_effect = lambda _repo, operation, revision="": (
            b"false"
            if operation == "shallow"
            else b"commit"
            if operation == "commit-type"
            else f":100644 100644 {'c' * 40} {'d' * 40} M\0{PATHS[0]}\0".encode()
        )
        self.assert_full()

    def test_failed_pending_expired_or_changed_proof_falls_back(self):
        published, _ = self.fixture.qualify_schedule_fixture("beta")
        self.start_push()
        baseline = copy.deepcopy(self.gh)
        rid = next(
            k
            for k, value in baseline.releases.items()
            if value["tag_name"] == published["tag"]
        )
        cases = {
            "pending": lambda gh: gh.runs[100].update(
                status="in_progress", conclusion=None
            ),
            "failed": lambda gh: gh.runs[100].update(conclusion="failure"),
            "draft": lambda gh: gh.releases[rid].update(draft=True),
            "missing-manifest": lambda gh: gh.assets[rid].pop(),
            "expired-evidence": lambda gh: gh.evidence_by_run[100][0].update(
                expired=True
            ),
            "bad-digest": lambda gh: gh.assets[rid][0].update(
                digest="sha256:" + "0" * 64
            ),
            "failed-ci-gate": lambda gh: gh.jobs_by_run[100][1].update(
                conclusion="failure"
            ),
            "failed-release-gate": lambda gh: gh.jobs_by_run[100][0].update(
                conclusion="failure"
            ),
            "different-policy": lambda gh: gh.ledger["plans"]["100"]["plan"].update(
                policy_sha256="0" * 64
            ),
        }
        for name, mutate in cases.items():
            self.gh = copy.deepcopy(baseline)
            mutate(self.gh)
            with (
                self.subTest(name=name),
                patch.object(lifecycle, "StateGitHub", return_value=self.gh),
            ):
                self.assert_full()
                lifecycle.PLAN.unlink()

    def test_main_movement_before_or_during_proof_preserves_full_route(self):
        self.fixture.qualify_schedule_fixture("beta")
        self.start_push()
        self.gh.default_head = "c" * 40
        with patch.object(lifecycle, "verify_scheduled_reuse") as verify:
            self.assert_full()
        verify.assert_not_called()
        self.gh.ledger["plans"].pop("200")
        self.gh.default_head = AFTER
        binary = self.gh.binary

        def moved(path):
            self.gh.default_head = "c" * 40
            return binary(path)

        with patch.object(self.gh, "binary", side_effect=moved):
            self.assert_full()

    def test_proof_exceptions_preserve_original_allocation(self):
        self.fixture.qualify_schedule_fixture("beta")
        self.start_push()
        baseline = copy.deepcopy(self.gh)
        for error in (
            rc.ReleaseError("unavailable"),
            ValueError("invalid"),
            KeyError("missing"),
            TypeError("bad metadata"),
            OSError("transport unavailable"),
        ):
            self.gh = copy.deepcopy(baseline)
            with (
                self.subTest(error=type(error).__name__),
                patch.object(lifecycle, "StateGitHub", return_value=self.gh),
                patch.object(lifecycle, "scheduled_reuse", side_effect=error),
            ):
                self.assert_full()
                lifecycle.PLAN.unlink()

    def test_non_test_push_never_probes_prior_publication(self):
        self.start_push()
        self.git.side_effect = lambda _repo, operation, revision="": (
            b"false"
            if operation == "shallow"
            else b"commit"
            if operation == "commit-type"
            else f":100644 100644 {'c' * 40} {'d' * 40} M\0src/app.py\0".encode()
        )
        with patch.object(lifecycle, "scheduled_reuse") as verify:
            self.assert_full()
        verify.assert_not_called()

    def test_manual_rc_and_scheduled_runs_do_not_classify_or_probe(self):
        for kind in ("workflow_dispatch", "schedule"):
            self.start_push(kind=kind)
            with (
                self.subTest(kind=kind),
                patch.object(lifecycle, "scheduled_reuse") as verify,
            ):
                self.assert_full()
            verify.assert_not_called()
            self.git.assert_not_called()
            self.gh.ledger["plans"].pop("200")
