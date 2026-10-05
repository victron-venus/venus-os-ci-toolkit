#!/usr/bin/env python3
"""Render opt-in runner routing without a hosted selector/bootstrap job."""

from __future__ import annotations

import argparse
import io
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ".github/workflows"
PROFILES = {
    "ci": "CI_RUNNER_LABELS",
    "automation": "CI_RUNNER_AUTOMATION_LABELS",
    "release": "CI_RUNNER_RELEASE_LABELS",
}
# Forks, merge queues (which can contain forks), PR-target events and unknown
# events stay hosted. Only same-repository PRs and default-branch events qualify.
DEFAULT_BRANCH_EVENT = (
    "(contains(fromJSON('[\"push\",\"schedule\",\"workflow_dispatch\"]'), github.event_name) && "
    "github.ref == format('refs/heads/{0}', github.event.repository.default_branch))"
)
TRUSTED_EVENT = (
    "((github.event_name == 'pull_request' && "
    "github.event.pull_request.head.repo.full_name == github.repository) || "
    + DEFAULT_BRANCH_EVENT + ")"
)
METADATA_EVENT = TRUSTED_EVENT.replace(
    "github.event_name == 'pull_request'",
    "contains(fromJSON('[\"pull_request\",\"pull_request_target\",\"pull_request_review\"]'), github.event_name)",
)
ACTIVE_WORKFLOWS = {
    "python-ci.yml": "ci", "go-ci.yml": "ci", "typescript-ci.yml": "ci",
    "rust-ci.yml": "ci", "terraform-ci.yml": "ci", "security-scan.yml": "ci",
    "ci.yml": "ci", "contract-tests.yml": "ci",
    "auto-approve-reusable.yml": "automation", "auto-merge.yml": "automation",
    "coderabbit-review-reusable.yml": "automation", "renovate.yml": "automation",
    "vendor-update.yml": "release",
}


def runner_labels(profile="ci", *, explicit_input=None, scalar=False):
    """Keep hosted defaults and legacy explicit inputs, with no implicit pool reuse."""
    variable = PROFILES[profile]
    trusted = {"ci": TRUSTED_EVENT, "automation": METADATA_EVENT, "release": DEFAULT_BRANCH_EVENT}[profile]
    managed = (
        "(vars.CI_RUNNER_MODE == 'self-hosted' || vars.CI_RUNNER_MODE == 'k3s') && "
        f"{trusted} && (vars.{variable} || '[]') || '[\"ubuntu-latest\"]'"
    )
    if explicit_input is not None:
        if explicit_input not in {"runner", "runner-labels"}:
            raise ValueError("Unsupported explicit runner input")
        default = "ubuntu-latest" if scalar else '["ubuntu-latest"]'
        value = f"toJSON(inputs.{explicit_input})" if scalar else f"inputs.{explicit_input}"
        # Existing private callers retain their reviewed explicit pool selection.
        # Public callers cannot use an input to bypass the fork/event boundary.
        managed = (
            f"(github.event.repository.private || {trusted}) && "
            f"inputs.{explicit_input} != '{default}' && {value} || ({managed})"
        )
    return "${{ fromJSON(" + managed + ") }}"


def yaml_editor():
    """Use the same round-trip dependency as the coverage adapter."""
    # Generator-only dependency; reusable workflows contain the resulting expression.
    from ruamel.yaml import YAML  # pylint: disable=import-outside-toplevel

    editor = YAML(typ="rt", pure=True)
    editor.preserve_quotes = True
    editor.allow_duplicate_keys = False
    editor.indent(mapping=2, sequence=4, offset=2)
    editor.width = 4096
    return editor


class ConsumerRunnerAdapter:
    """Traverse one consumer's workflow graph without writing any files."""

    def __init__(self, directory, policy):
        self.directory = directory
        self.editor = yaml_editor()
        self.roots = dict.fromkeys(policy["validation_workflows"], "ci")
        self.roots["quality-gate.yml"] = "ci"
        if policy.get("mode", "release") == "release":
            self.roots.update({"release-pipeline.yml": "release", "release-build.yml": "release"})
        self.files = {}
        self.visited = {}

    def visit(self, name, profile, chain=()):
        """Resolve local calls and reject paths or roles that cannot be adapted safely."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+\.ya?ml", name) or name in chain:
            raise ValueError("Invalid or recursive runner adapter: " + name)
        profile = self.roots.get(name, profile)
        if name in self.visited:
            if self.visited[name] != profile:
                raise ValueError("Workflow has conflicting CI/release runner profiles: " + name)
            return
        self.visited[name] = profile
        path = self.directory / WORKFLOWS / name
        if path.resolve() != self.directory.resolve() / WORKFLOWS / name:
            raise ValueError("Runner workflow must not use a symlink: " + name)
        source = path.read_text()
        workflow = self.editor.load(source)
        changed = adapt_jobs(workflow, profile, name)
        for job in workflow["jobs"].values():
            reference = job.get("uses", "")
            if reference.startswith("./.github/workflows/"):
                self.visit(reference.removeprefix("./.github/workflows/"), profile, (*chain, name))
        output = io.StringIO()
        if changed:
            self.editor.dump(workflow, output)
        self.files[WORKFLOWS + "/" + name] = output.getvalue() if changed else source


def render_consumer(directory, policy):
    """Adapt existing Linux/x64 jobs reachable from the declared CI/release graph."""
    adapter = ConsumerRunnerAdapter(directory, policy)
    for name, profile in adapter.roots.items():
        adapter.visit(name, profile)
    return adapter.files


def adapt_jobs(workflow, profile, name):
    """Edit owned Linux selections while preserving matrices and other platforms."""
    changed = False
    for job in workflow["jobs"].values():
        runner = job.get("runs-on")
        managed = isinstance(runner, str) and runner.startswith(
            "${{ fromJSON((vars.CI_RUNNER_MODE == 'self-hosted' || vars.CI_RUNNER_MODE == 'k3s') && "
        )
        if runner != "ubuntu-latest" and not managed:
            continue
        if any(getattr(getattr(item, "anchor", None), "value", None) or getattr(item, "merge", None)
               for item in (workflow, workflow["jobs"], job)):
            raise ValueError("Review aliased/merged runner adapters explicitly: " + name)
        selected = runner_labels(profile)
        if runner != selected:
            job["runs-on"] = selected
            changed = True
    return changed


def render_shared_workflow(filename, profile):
    """Normalize one owned workflow while keeping its explicit input contract."""
    editor = yaml_editor()
    path = ROOT / WORKFLOWS / filename
    source = path.read_text()
    workflow = editor.load(source)
    changed = False
    explicit = "runner-labels" if filename in {"auto-merge.yml", "auto-approve-reusable.yml"} else None
    selected = runner_labels(profile, explicit_input=explicit)
    for job in workflow["jobs"].values():
        if "runs-on" in job and job["runs-on"] != selected:
            job["runs-on"] = selected
            changed = True
    output = io.StringIO()
    if changed:
        editor.dump(workflow, output)
    return output.getvalue() if changed else source


def render_workflows():
    """Update job routing only, preserving repository commands, comments and pins."""
    return {ROOT / WORKFLOWS / name: render_shared_workflow(name, profile)
            for name, profile in ACTIVE_WORKFLOWS.items()}


def main():
    """Generate the shared workflow expressions or detect drift without writing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = render_workflows()
    drift = [path.name for path, text in files.items() if path.read_text() != text]
    if args.check and drift:
        parser.exit(1, "Runner selection drift: " + ", ".join(drift) + "\n")
    if not args.check:
        for path, text in files.items():
            path.write_text(text)
    print(f"Verified shared runner routing in {len(files)} workflows")


if __name__ == "__main__":
    main()
