"""Closed requests and public-only resolution fail before releasing credentials."""

import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_closed as closed  # noqa: E402
import test_versioned_release as fixtures  # noqa: E402


def policy():
    return {**fixtures.policy("promote-bytes"), "visibility": "public", "single_entry_ci": True,
            "closed_build": {"workflow": "closed-build.yml", "environment": "closed-build", "enabled": True}}


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.event = {"repository": {"full_name": fixtures.REPO, "private": False, "default_branch": "main"},
                      "inputs": {"channel": "rc", "expected_sha": fixtures.SHA}}
        self.env = {"GITHUB_REPOSITORY": fixtures.REPO, "GITHUB_SHA": fixtures.SHA,
                    "GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch"}

    def test_default_release_and_explicit_rebuild(self):
        self.assertEqual(closed.request(policy(), self.event, self.env), {
            "operation": "release", "closed_required": "true", "closed_request": "", "release_tag": ""})
        for event_name in ("push", "schedule"):
            result = closed.request(policy(), self.event, {**self.env, "GITHUB_EVENT_NAME": event_name})
            self.assertEqual(result["closed_request"], "")
            self.assertEqual(result["closed_required"], "true")
        self.event["inputs"].update(operation="closed-rebuild", existing_release="v1.2.3-rc.2", closed_request="request_012")
        result = closed.request(policy(), self.event, self.env)
        self.assertEqual(result["operation"], "closed-rebuild")
        self.assertEqual(result["closed_request"], "request_012")
        self.assertEqual(result["release_tag"], "v1.2.3-rc.2")

    def test_disabled_policy_requires_exact_skipped_path(self):
        absent, disabled = policy(), policy()
        del absent["closed_build"]
        disabled["closed_build"]["enabled"] = False
        for config in (absent, disabled):
            self.assertEqual(closed.request(config, self.event, self.env)["closed_required"], "false")
            for inputs in ({"closed_request": "explicit"}, {"operation": "closed-rebuild", "existing_release": "v1.2.3"}):
                event = {**self.event, "inputs": {**self.event["inputs"], **inputs}}
                with self.assertRaises(ValueError):
                    closed.request(config, event, self.env)

    def test_enabled_is_an_explicit_policy_boolean(self):
        for value in (None, "true", "false", 1, 0, {}):
            config = policy()
            config["closed_build"]["enabled"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                closed.configuration(config)
        config = policy()
        del config["closed_build"]["enabled"]
        with self.assertRaises(ValueError):
            closed.configuration(config)

    def test_untrusted_events_refs_repositories_and_sha_are_rejected(self):
        config = policy()
        for field, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "workflow_run"),
                             ("GITHUB_REF", "refs/heads/topic"), ("GITHUB_REPOSITORY", "fork/project"),
                             ("GITHUB_SHA", "b" * 40)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                closed.request(config, self.event, {**self.env, field: value})
        self.event["repository"]["private"] = True
        with self.assertRaises(ValueError):
            closed.request(config, self.event, self.env)

    def test_dispatch_is_a_bounded_identifier_interface(self):
        config = policy()
        for key, value in (("closed_request", "https://host/secret"), ("closed_request", "x\ny"),
                           ("closed_request", "x" * 81), ("closed_request", "-request"),
                           ("closed_request", "_request"), ("closed_request", {}),
                           ("operation", "publish"), ("existing_release", "v1.2.3")):
            event = copy.deepcopy(self.event)
            event["inputs"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises((ValueError, TypeError)):
                closed.request(config, event, self.env)
        for value in ("a", "a" * 80, "Request_012-xyz"):
            event = copy.deepcopy(self.event)
            event["inputs"]["closed_request"] = value
            self.assertEqual(closed.request(policy(), event, self.env)["closed_request"], value)
        for tag in ("main", "v1.2.3-nightly.1", "https://host", "v1.2.3; id", "v1.2.3\n"):
            event = copy.deepcopy(self.event)
            event["inputs"].update(operation="closed-rebuild", existing_release=tag)
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                closed.request(config, event, self.env)

    def test_cli_does_not_echo_invalid_dispatch_values(self):
        import io
        from contextlib import redirect_stdout
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.event["inputs"]["closed_request"] = "PRIVATE_CANARY\ninvalid"
        (directory / ".release-policy.json").write_text(json.dumps(policy()))
        (directory / "event.json").write_text(json.dumps(self.event))
        output = io.StringIO()
        old = Path.cwd()
        try:
            os.chdir(directory)
            with patch.dict(os.environ, {**self.env, "GITHUB_EVENT_PATH": str(directory / "event.json")}), \
                    patch.object(sys, "argv", ["release_closed.py", "request"]), redirect_stdout(output):
                self.assertEqual(closed.main(), 1)
        finally:
            os.chdir(old)
        self.assertEqual(output.getvalue(), "CLOSED_REQUEST_REJECTED\n")


class PublicResolutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LifecycleTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.policy["versioning"]["promotion"] = "promote-bytes"
        (self.fixture.root / ".release-policy.json").write_text(json.dumps(self.fixture.policy))
        self.result, self.plan = self.fixture.qualify_schedule_fixture()
        self.gh = self.fixture.gh
        self.gh.info["private"] = False
        self.output = self.fixture.root / "resolved"
        self.enterContext(patch.object(closed, "rc", fixtures.rc))
        self.gh.writes.clear()
        self.gh.ledger_writes.clear()

    def resolve(self):
        return closed.resolve(self.gh, self.result["tag"], self.output, fixtures.SHA, 200)

    def test_retained_release_context_contains_exact_public_bytes_without_mutation(self):
        context = self.resolve()
        self.assertEqual(context["run_id"], "100")
        self.assertEqual(context["run_attempt"], "1")
        self.assertEqual(context["source_sha"], fixtures.SHA)
        self.assertEqual(context["mode"], "published")
        self.assertEqual(json.loads((self.output / "version-plan.json").read_text()), self.plan)
        self.assertEqual(set(context["payloads"]), {p.name for p in (self.output / "payloads").iterdir()})
        for name, identity in context["payloads"].items():
            raw = (self.output / "payloads" / name).read_bytes()
            self.assertEqual(identity, {"size": len(raw), "sha256": fixtures.rc.digest(raw)})
        self.assertEqual(self.gh.writes, [])
        self.assertEqual(self.gh.ledger_writes, [])

    def test_fuzz_validation_payload_and_receipt_remain_in_complete_inventory(self):
        original = self.fixture.build_current

        def build_with_validation():
            plan = original()
            folder = self.fixture.root / "fuzz"
            folder.mkdir()
            (folder / "fuzz-report.json").write_text('{"synthetic":true}')
            fixtures.receipt.create_receipt(
                fixtures.lifecycle.PLAN, self.fixture.root / ".release-inputs.json",
                folder, folder / "release-inputs-fuzz.json")
            for path in folder.iterdir():
                (Path(self.fixture.args.assets) / path.name).write_bytes(path.read_bytes())
            return plan

        with patch.object(self.fixture, "build_current", side_effect=build_with_validation):
            self.result, self.plan, _ = self.fixture.release_run("rc", 101)
        self.gh.jobs_by_run[101] = copy.deepcopy(self.gh.jobs_by_run[100])
        for assets in self.gh.assets.values():
            for asset in assets:
                asset["digest"] = "sha256:" + fixtures.rc.digest(self.gh.files[asset["id"]])
        context = self.resolve()
        self.assertEqual(context["run_id"], "101")
        self.assertIn("fuzz-report.json", context["payloads"])
        self.assertIn("release-inputs-fuzz.json", context["payloads"])

    def test_failed_rerun_expired_wrong_workflow_and_failed_gate_never_stage_context(self):
        cases = {
            "rerun": lambda gh: gh.runs[100].update(run_attempt=2),
            "failed": lambda gh: gh.runs[100].update(conclusion="failure"),
            "wrong_workflow": lambda gh: gh.runs[100].update(path=".github/workflows/other.yml"),
            "expired": lambda gh: gh.evidence_by_run[100][0].update(expired=True),
            "wrong_evidence": lambda gh: gh.evidence_by_run[100][0].update(digest="sha256:" + "0" * 64),
            "failed_gate": lambda gh: gh.jobs_by_run[100][1].update(conclusion="failure"),
        }
        original = copy.deepcopy(self.gh)
        for name, mutate in cases.items():
            self.gh = copy.deepcopy(original)
            mutate(self.gh)
            with self.subTest(name=name), self.assertRaises((ValueError, fixtures.rc.ReleaseError)):
                self.resolve()
            self.assertFalse(self.output.exists())
            self.assertEqual(self.gh.writes, [])

    def test_transfer_corruption_and_late_rerun_clean_partial_context(self):
        original = self.gh.download_asset
        for late in (False, True):
            def transfer(path, stream):
                original(path, stream)
                if late:
                    self.gh.runs[100]["run_attempt"] = 2
                else:
                    stream.write(b"changed")
            with self.subTest(late=late), patch.object(self.gh, "download_asset", side_effect=transfer), \
                    self.assertRaises((ValueError, fixtures.rc.ReleaseError)):
                self.resolve()
            self.assertFalse(self.output.exists())
            self.assertEqual(list(self.output.parent.glob("closed-public-*")), [])

    def test_existing_destination_is_preserved(self):
        self.output.mkdir()
        (self.output / "keep").write_text("original")
        with self.assertRaises(ValueError):
            self.resolve()
        self.assertEqual((self.output / "keep").read_text(), "original")


if __name__ == "__main__":
    unittest.main()
