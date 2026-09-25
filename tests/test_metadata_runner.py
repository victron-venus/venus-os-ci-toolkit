"""Metadata automation can use an existing trusted runner without checking out code."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class MetadataRunnerTests(unittest.TestCase):
    """Keep hosted defaults and metadata-only execution intact for both consumers."""

    def test_runner_selection_is_typed_and_defaults_to_hosted(self):
        """Dedicated labels are explicit inputs rather than PR-controlled metadata."""
        for filename in ("auto-merge.yml", "auto-approve-reusable.yml"):
            with self.subTest(filename=filename):
                workflow = yaml.load(
                    (ROOT / ".github/workflows" / filename).read_text(),
                    Loader=yaml.BaseLoader,
                )
                runner = workflow["on"]["workflow_call"]["inputs"]["runner-labels"]
                self.assertEqual(runner["type"], "string")
                self.assertEqual(runner["default"], '["ubuntu-latest"]')
                for job in workflow["jobs"].values():
                    self.assertEqual(
                        job["runs-on"], "${{ fromJSON(inputs.runner-labels) }}"
                    )
                    for step in job["steps"]:
                        self.assertNotIn("uses", step)
                        self.assertNotIn("checkout", step.get("run", ""))

    def test_approval_requires_python3_on_selected_runner(self):
        """Private Linux runners need not have a legacy python alias."""
        workflow = yaml.load(
            (ROOT / ".github/workflows/auto-approve-reusable.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        self.assertEqual(
            workflow["jobs"]["auto-approve"]["steps"][0]["shell"], "python3 {0}"
        )


if __name__ == "__main__":
    unittest.main()
