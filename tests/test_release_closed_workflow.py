"""Opt-in closed branches preserve public qualification and default consumers."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_release as installer  # noqa: E402
import release_closed as closed  # noqa: E402
import release_closed_workflow as generator  # noqa: E402
from test_release_closed import policy  # noqa: E402
import test_channel_validation as channel_fixtures  # noqa: E402


def adapter():
    return {"on": {"workflow_call": {"inputs": {
        name: {"type": "string", "required": name == "action"} for name in closed.INPUTS
    }}}, "permissions": {"contents": "read", "actions": "read"}, "jobs": {"worker": {
        "runs-on": "ubuntu-24.04", "environment": "closed-build", "timeout-minutes": 30,
        "steps": [{"run": "python3 scripts/closed_build.py", "env": {"CLOSED_INPUT_READ_URL": "${{ secrets.CLOSED_INPUT_READ_URL }}"}}],
    }}}


class ClosedWorkflowTests(unittest.TestCase):
    def test_public_gate_inventory_and_existing_conditions_are_preserved(self):
        config = policy()
        config["fuzzing"] = {"toolkit_ref": "a" * 40}
        baseline_policy = copy.deepcopy(config)
        del baseline_policy["closed_build"]
        baseline = installer.release(baseline_policy)
        result = installer.release(config)
        jobs = result["jobs"]
        self.assertEqual(jobs["gate"], baseline["jobs"]["gate"])
        self.assertEqual(jobs["gate"]["needs"], ["prepare", "checks", "build", "fuzz"])
        for name in ("checks", "build", "fuzz"):
            self.assertEqual(jobs[name], baseline["jobs"][name])
        for name in ("candidate", "final"):
            self.assertIn(baseline["jobs"][name]["if"][3:-3].strip(), jobs[name]["if"])
            self.assertEqual(jobs[name]["steps"], baseline["jobs"][name]["steps"])
            self.assertIn("closed_build", jobs[name]["needs"])
        self.assertEqual(result["concurrency"]["queue"], "max")
        self.assertIs(result["concurrency"]["cancel-in-progress"], False)
        self.assertNotIn("closed_request", baseline["jobs"])
        self.assertNotIn("operation", baseline["on"]["workflow_dispatch"]["inputs"])

    def test_secretless_resolvers_and_workers_are_separate(self):
        jobs = installer.release(policy())["jobs"]
        for name in ("closed_request", "closed_existing_resolve", "closed_stable_resolve"):
            job = jobs[name]
            self.assertEqual(job["runs-on"], "ubuntu-24.04")
            self.assertNotIn("environment", job)
            self.assertNotIn("secrets.", json.dumps(job))
            self.assertNotIn("write", job["permissions"].values())
        for name, action in (("closed_build", "build"), ("closed_stable_validate", "validate"), ("closed_rebuild", "rebuild")):
            job = jobs[name]
            self.assertEqual(job["uses"], "./.github/workflows/closed-build.yml")
            self.assertEqual(job["with"]["action"], action)
            self.assertNotIn("secrets", job)
            self.assertNotIn("outputs", job)
        self.assertEqual(jobs["closed_rebuild"]["needs"], ["closed_request", "closed_existing_resolve"])
        self.assertNotIn("prepare", jobs["closed_existing_resolve"]["needs"])
        self.assertIn("operation == 'release'", jobs["scope"]["if"])
        self.assertIn("operation == 'release'", jobs["prepare"]["if"])
        self.assertIn("closed_stable_validate", jobs["stable"]["needs"])
        self.assertIn("!cancelled()", jobs["stable"]["if"])
        promotion = jobs["stable"]["steps"][-2:]
        self.assertIn("record-promotion", promotion[0]["run"])
        self.assertEqual(promotion[1]["with"]["name"], "release-promotion-evidence")
        self.assertNotIn("if", promotion[0])
        self.assertNotIn("continue-on-error", promotion[0])

    def test_disabled_rollout_keeps_the_same_graph_for_source_controlled_selection(self):
        enabled, disabled = policy(), policy()
        disabled["closed_build"]["enabled"] = False
        self.assertEqual(installer.release(enabled), installer.release(disabled))

    def test_adapter_rejects_dynamic_runners_unprotected_jobs_and_public_outputs(self):
        config = policy()
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        directory = root / ".github/workflows"
        directory.mkdir(parents=True)
        path = directory / "closed-build.yml"
        good = adapter()
        path.write_text(installer.dump(good))
        generator.validate_adapter(root, policy())
        for field, value in (("runs-on", "ubuntu-latest"), ("runs-on", "${{ inputs.runner }}"),
                             ("environment", "release"), ("outputs", {"private": "value"}),
                             ("permissions", {"contents": "write"}), ("continue-on-error", True)):
            bad = copy.deepcopy(good)
            bad["jobs"]["worker"][field] = value
            path.write_text(installer.dump(bad))
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                generator.validate_adapter(root, config)
        for action in ("actions/upload-artifact@" + "a" * 40, "actions/cache@" + "a" * 40):
            bad = copy.deepcopy(good)
            bad["jobs"]["worker"]["steps"].append({"uses": action})
            path.write_text(installer.dump(bad))
            with self.assertRaises(ValueError):
                generator.validate_adapter(root, config)
        path.write_text(installer.dump(good) + "jobs: {}\n")
        with self.assertRaises(ValueError):
            generator.validate_adapter(root, config)

    def test_rendered_opt_in_is_idempotent_and_contains_runtime_closure(self):
        fixture = channel_fixtures.ChannelValidationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.policy["closed_build"] = policy()["closed_build"]
        fixture.write("closed-build.yml", adapter())
        first = fixture.render()
        self.assertEqual(first, installer.render(fixture.root))
        self.assertIn("scripts/release_closed.py", first)
        self.assertIn("scripts/release_promotion.py", first)
        self.assertIn(".github/release-tests/test_release_closed.py", first)
        self.assertIn("docs/closed-builds.md", first)
        self.assertNotIn(".github/workflows/closed-build.yml", first)

    def test_policy_rejects_arbitrary_adapter_inputs(self):
        for value in (None, {}, {"workflow": "../secret.yml", "environment": "closed-build"},
                      {"workflow": "closed-build.yml", "environment": "${{ inputs.env }}"},
                      {**policy()["closed_build"], "secrets": "inherit"}):
            config = policy()
            config["closed_build"] = value
            with self.subTest(value=value), self.assertRaises(ValueError):
                closed.configuration(config)
