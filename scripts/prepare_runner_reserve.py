#!/usr/bin/env python3
"""Adopt opt-in shared runner routing without enabling it or upgrading releases."""
from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path

from runner_selection import ConsumerRunnerAdapter, adapt_jobs, yaml_editor

TOOLKIT = "victron-venus/venus-os-ci-toolkit/.github/workflows/"
AUTOMATION = {"auto-approve.yml", "auto-merge.yml", "coderabbit-review.yml", "coderabbit-autofix.yml"}


def select_profile(name, workflow, profiles):
    """Separate release and metadata routing from source validation."""
    if name in AUTOMATION:
        # PR-target metadata wrappers must not execute checked-out PR code.
        if any(job.get("steps") for job in workflow["jobs"].values()):
            raise ValueError("Review local automation steps: " + name)
        return "automation"
    if name.startswith(("release-", "nightly", "vendor-")):
        return "release"
    return profiles.get(name, "ci")


def update_pins(workflow, toolkit_ref):
    """Pin only reusable calls belonging to this toolkit."""
    changed = False
    for job in workflow["jobs"].values():
        reference = job.get("uses", "")
        if not reference.startswith(TOOLKIT):
            continue
        name, separator, old_ref = reference[len(TOOLKIT):].partition("@")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+\.ya?ml", name):
            raise ValueError("Invalid reusable workflow reference")
        if old_ref != toolkit_ref:
            job["uses"] = TOOLKIT + name + "@" + toolkit_ref
            changed = True
    return changed


def render_workflow(path, profiles, toolkit_ref):
    """Preserve syntax/comments and refuse file or parent-directory symlinks."""
    if path.resolve() != path:
        raise ValueError("Refusing workflow symlink: " + path.name)
    editor = yaml_editor()
    workflow = editor.load(path.read_text())
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        raise ValueError("Invalid workflow: " + path.name)
    changed = adapt_jobs(workflow, select_profile(path.name, workflow, profiles), path.name)
    pinned = update_pins(workflow, toolkit_ref)
    if not (changed or pinned):
        return None
    output = io.StringIO()
    editor.dump(workflow, output)
    return output.getvalue()


def render(directory, toolkit_ref):
    """Return a complete, validated edit set before writing any consumer files."""
    directory = Path(directory).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", toolkit_ref):
        raise ValueError("Use a reviewed immutable toolkit commit SHA")
    policy_path = directory / ".release-policy.json"
    if policy_path.is_symlink():
        raise ValueError("Refusing policy symlink")
    policy = json.loads(policy_path.read_text()) if policy_path.exists() else {}
    profiles = {}
    if policy.get("single_entry_ci"):
        adapter = ConsumerRunnerAdapter(directory, policy)
        for name, profile in adapter.roots.items():
            adapter.visit(name, profile)
        profiles.update(adapter.visited)
    files = {}
    for path in sorted((directory / ".github/workflows").glob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        text = render_workflow(path, profiles, toolkit_ref)
        if text is not None:
            files[str(path.relative_to(directory))] = text
    if policy.get("coverage") and policy["coverage"]["toolkit_ref"] != toolkit_ref:
        policy["coverage"]["toolkit_ref"] = toolkit_ref
        files[".release-policy.json"] = json.dumps(policy, indent=2) + "\n"
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--toolkit-ref", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    files = render(args.repository, args.toolkit_ref)
    if args.check and files:
        parser.exit(1, "Runner adoption drift: " + ", ".join(files) + "\n")
    if not args.check:
        for path, source in files.items():
            (args.repository / path).write_text(source)
    print(f"Runner adoption: {len(files)} files; no Actions settings changed")


if __name__ == "__main__":
    main()
