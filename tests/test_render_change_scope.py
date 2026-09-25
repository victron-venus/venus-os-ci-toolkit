"""The scope bootstrap must follow the caller's runner without changing defaults."""

import importlib.util
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "render_change_scope", ROOT / "scripts/render_change_scope.py"
)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class ScopeRoutingTests(unittest.TestCase):
    def test_default_and_explicit_runner_are_supported_at_dispatch(self):
        workflow = yaml.load(
            renderer.render(renderer.SOURCE.read_text()), Loader=yaml.BaseLoader
        )
        runner = workflow["on"]["workflow_call"]["inputs"]["runner"]
        self.assertEqual(runner["type"], "string")
        self.assertEqual(runner["default"], "ubuntu-latest")
        self.assertEqual(workflow["jobs"]["scope"]["runs-on"], "${{ inputs.runner }}")
        self.assertEqual(list(workflow["jobs"]), ["scope"])

    def test_checked_in_workflow_matches_generator(self):
        self.assertEqual(
            renderer.TARGET.read_text(), renderer.render(renderer.SOURCE.read_text())
        )
