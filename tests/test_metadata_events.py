"""Exercise metadata job eligibility and concurrency with real caller expressions."""

import ast
import json
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CALLERS = (
    ".github/workflows/auto-approve.yml",
    ".github/workflows/auto-merge-prs.yml",
    "docs/examples/auto-approve.yml",
    "docs/examples/auto-merge.yml",
)
REUSABLES = (
    ".github/workflows/auto-approve-reusable.yml",
    ".github/workflows/auto-merge.yml",
)


def definition(path):
    """Preserve GitHub's on key and expression scalars."""
    return yaml.load((ROOT / path).read_text(), Loader=yaml.BaseLoader)


def evaluate(expression, context):
    """Interpret the small expression subset used here; reject unknown syntax."""
    expression = expression.removeprefix("${{").removesuffix("}}")

    def value(match):
        values = [context]
        for field in match.group().split("."):
            values = (
                [item for value in values for item in value]
                if field == "*"
                else [v.get(field, "") if isinstance(v, dict) else "" for v in values]
            )
        return repr(values if ".*." in match.group() else values[0])

    expression = re.sub(r"\b(?:github|inputs)(?:\.[\w*-]+)+", value, expression)
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = re.sub(r"!(?!=)", " not ", expression)
    expression = re.sub(r"\bnull\b", "None", expression)

    def visit(node):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, (ast.Dict, ast.List)):
            return ast.literal_eval(node)
        if isinstance(node, ast.BoolOp):
            result = visit(node.values[0])
            for child in node.values[1:]:
                if isinstance(node.op, ast.And):
                    result = visit(child) if result else result
                else:
                    result = result if result else visit(child)
            return result
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not visit(node.operand)
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right = visit(node.left), visit(node.comparators[0])
            if isinstance(node.ops[0], ast.Eq):
                return left == right
            if isinstance(node.ops[0], ast.NotEq):
                return left != right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            args = [visit(arg) for arg in node.args]
            if node.func.id == "fromJSON" and len(args) == 1:
                return json.loads(args[0])
            if node.func.id == "contains" and len(args) == 2:
                return args[1] in args[0]
        raise AssertionError(f"Unsupported metadata expression: {expression}")

    return visit(ast.parse("(" + expression.strip() + ")", mode="eval").body)


def context(action="opened", changes=None, run="10", event="pull_request_target"):
    """Use the official edited schema: changes.base contains ref/sha from values."""
    return {
        "github": {
            "workflow": "metadata",
            "repository": "example/repo",
            "run_id": run,
            "event_name": event,
            "event": {
                "repository": {"default_branch": "main"},
                "action": action,
                "changes": changes or {},
                "pull_request": {
                    "number": 42,
                    "base": {"ref": "main"},
                    "user": {"login": "4alvit"},
                    "draft": False,
                    "head": {"repo": {"full_name": "contributor/fork"}},
                    "labels": [{"name": "automerge"}],
                },
            },
        },
        "inputs": {"authors": '["4alvit","dependabot[bot]"]'},
    }


def group(workflow, data):
    """Render each interpolation to compare actual workflow concurrency groups."""
    return re.sub(
        r"\$\{\{.*?\}\}",
        lambda match: str(evaluate(match.group(), data)),
        workflow["concurrency"]["group"],
    )


class MetadataEventsTests(unittest.TestCase):
    """Skip metadata churn without losing retargeting or cancellation guarantees."""

    def test_body_title_edits_never_allocate_metadata_jobs(self):
        for name in CALLERS:
            job = next(iter(definition(name)["jobs"].values()))
            for changes in (
                {},
                {"body": {"from": ""}},
                {"title": {"from": "old"}},
                {"body": {"from": "old"}, "title": {"from": "old"}},
            ):
                with self.subTest(workflow=name, changes=changes):
                    self.assertFalse(evaluate(job["if"], context("edited", changes)))

    def test_retarget_and_other_state_events_remain_eligible(self):
        base = {"base": {"ref": {"from": "old"}, "sha": {"from": "a" * 40}}}
        for name in CALLERS:
            job = next(iter(definition(name)["jobs"].values()))
            for action, changes in (
                ("edited", base),
                ("edited", base | {"body": {"from": "old"}}),
                ("opened", {}),
                ("synchronize", {}),
                ("reopened", {}),
                ("ready_for_review", {}),
                ("labeled", {}),
                ("unlabeled", {}),
            ):
                with self.subTest(workflow=name, action=action):
                    self.assertTrue(evaluate(job["if"], context(action, changes)))

    def test_ignored_edits_cannot_cancel_or_replace_meaningful_runs(self):
        for name in CALLERS:
            workflow = definition(name)
            with self.subTest(workflow=name):
                active = group(workflow, context("opened"))
                pending = group(workflow, context("synchronize", run="11"))
                ignored = group(workflow, context("edited", run="12"))
                next_ignored = group(workflow, context("edited", run="13"))
                retargeted = group(
                    workflow,
                    context("edited", {"base": {"ref": {"from": "other"}}}),
                )
                self.assertEqual(active, pending)
                self.assertEqual(active, retargeted)
                self.assertNotIn(ignored, (active, pending))
                self.assertNotEqual(ignored, next_ignored)
                self.assertEqual(workflow["concurrency"]["cancel-in-progress"], "true")
                for trigger in workflow["on"].values():
                    self.assertIn("edited", trigger["types"])
                    self.assertIn("converted_to_draft", trigger["types"])

    def test_trust_draft_and_dependabot_routing_are_preserved(self):
        workflow = definition(CALLERS[0])
        condition = workflow["jobs"]["auto-approve"]["if"]
        for author, draft, event, expected in (
            ("stranger", False, "pull_request_target", False),
            ("4alvit", True, "pull_request_target", False),
            ("4alvit", False, "pull_request_target", True),
            ("dependabot[bot]", False, "pull_request", False),
            ("dependabot[bot]", False, "pull_request_target", True),
        ):
            data = context(event=event)
            data["github"]["event"]["pull_request"]["user"]["login"] = author
            data["github"]["event"]["pull_request"]["draft"] = draft
            with self.subTest(author=author, draft=draft, event=event):
                self.assertEqual(bool(evaluate(condition, data)), expected)

    def test_dependabot_uses_the_default_branch_metadata_context(self):
        for name in (CALLERS[2], CALLERS[3]):
            condition = next(iter(definition(name)["jobs"].values()))["if"]
            for author in ("4alvit", "californiantiramisu", "dependabot[bot]", "renovate[bot]"):
                for same_repository in (True, False):
                    for event in ("pull_request", "pull_request_target"):
                        data = context(event=event)
                        pr = data["github"]["event"]["pull_request"]
                        pr["user"]["login"] = author
                        pr["head"]["repo"]["full_name"] = "example/repo" if same_repository else "contributor/fork"
                        target = author == "dependabot[bot]" or not same_repository
                        expected = target if event == "pull_request_target" else not target
                        with self.subTest(workflow=name, author=author, same=same_repository, event=event):
                            self.assertEqual(bool(evaluate(condition, data)), expected)
                            if event == "pull_request_target":
                                pr["base"]["ref"] = "untrusted-topic"
                                self.assertFalse(evaluate(condition, data))

    def test_pin_only_consumers_keep_the_replacement_job_after_metadata_edits(self):
        """An older caller can cancel the first run; its replacement must still run."""
        for name in REUSABLES:
            job = next(iter(definition(name)["jobs"].values()))
            for changes in ({"body": {"from": "old"}}, {"title": {"from": "old"}}):
                with self.subTest(workflow=name, changes=changes):
                    self.assertTrue(
                        evaluate(job.get("if", "True"), context("edited", changes))
                    )

    def test_explicit_approval_recovery_is_unaffected(self):
        job = definition(REUSABLES[0])["jobs"]["auto-approve"]
        data = context(event="workflow_dispatch")
        data["github"]["event"] = {}
        data["inputs"].update({"pull-request-number": "42", "expected-head": "a" * 40})
        self.assertTrue(evaluate(job["if"], data))


if __name__ == "__main__":
    unittest.main()
