#!/usr/bin/env python3
"""Move only GitHub Actions updates from Dependabot to the fleet Renovate preset.

No network calls or Git mutations. Other Dependabot ecosystems and their comments
are preserved apart from trailing blank lines. Review and test before submission.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import yaml

PRESET = "local>victron-venus/venus-os-ci-toolkit:renovate-ci"
DISABLED_ACTIONS = (
    "  # Renovate owns CI updates. The ignore rule also prevents separate\n"
    "  # Dependabot security-update PRs; vulnerability alerts remain enabled.\n"
    "  - package-ecosystem: github-actions\n"
    "    directory: /\n"
    "    schedule:\n"
    "      interval: weekly\n"
    "    open-pull-requests-limit: 0\n"
    "    ignore:\n"
    "      - dependency-name: '*'\n"
)


def action_nodes(document):
    """Select actual job/step uses scalars, excluding shell and env text."""

    def field(node, name):
        if isinstance(node, yaml.MappingNode):
            return next((value for key, value in node.value if key.value == name), None)
        return None

    jobs = field(document, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        return
    for _, job in jobs.value:
        value = field(job, "uses")
        if isinstance(value, yaml.ScalarNode):
            yield value
        steps = field(job, "steps")
        if isinstance(steps, yaml.SequenceNode):
            for step in steps.value:
                value = field(step, "uses")
                if isinstance(value, yaml.ScalarNode):
                    yield value


def action_version_map(directory: Path, extra: dict | None) -> dict:
    """Use declared SHA/ref pairs only; discovery remains a read-only review step."""
    versions = {}
    for manifest in [
        Path(__file__).resolve().parents[1] / ".github/action-pins.json",
        directory / ".github/action-pins.json",
    ]:
        if manifest.exists():
            for pin in json.loads(manifest.read_text()):
                versions[(pin["packageName"], pin["digest"])] = pin["version"]
    for action, entries in (extra or {}).items():
        for digest, version in entries.items():
            if not re.fullmatch(r"[a-f0-9]{40}", digest) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._/+-]*", version
            ):
                raise ValueError("Expected verified SHA-to-release-ref mappings")
            versions[(action, digest)] = version
    return versions


def annotated_workflows(directory: Path, extra: dict | None) -> dict[Path, str]:
    """Prepare update metadata before any files or update ownership are changed."""
    versions = action_version_map(directory, extra)
    rendered, unknown = {}, set()
    root = directory / ".github/workflows"
    for path in sorted([*root.glob("*.yml"), *root.glob("*.yaml")]):
        original = path.read_text()
        lines = original.splitlines(keepends=True)
        for node in action_nodes(yaml.compose(original, Loader=yaml.BaseLoader)):
            match = re.fullmatch(r"([^@\s]+/[^@\s]+)@([a-f0-9]{40})", node.value)
            if not match:
                continue
            action, digest = match.groups()
            index = node.end_mark.line
            if node.start_mark.line != index:
                raise ValueError(f"Multiline action reference requires review: {path}")
            suffix = lines[index][node.end_mark.column :].strip()
            if suffix and not suffix.startswith("#"):
                raise ValueError(f"Ambiguous action reference requires review: {path}")
            version = (
                "main"
                if action.startswith("victron-venus/venus-os-ci-toolkit/")
                else versions.get((action, digest))
            )
            # A prose note is not an update selector. Preserve it after the ref.
            recognized = r"(?:v?\d+(?:\.\d+){0,2}(?:[-+][A-Za-z0-9.-]+)?|main|master|stable|beta|nightly)"
            if re.match(r"#\s*" + recognized + r"(?:\s|[;,]|$)", suffix) or (
                version
                and re.match(r"#\s*" + re.escape(version) + r"(?:\s|[;,]|$)", suffix)
            ):
                continue
            if version is None:
                unknown.add(f"{action}@{digest}")
                continue
            ending = "\n" if lines[index].endswith("\n") else ""
            note = "; " + suffix.removeprefix("#").strip() if suffix else ""
            lines[index] = (
                lines[index][: node.end_mark.column].rstrip()
                + f" # {version}{note}"
                + ending
            )
        result = "".join(lines)
        if result != original:
            if yaml.safe_load(result) != yaml.safe_load(original):
                raise ValueError(
                    f"Action metadata would change workflow semantics: {path}"
                )
            rendered[path] = result
    if unknown:
        raise ValueError(
            "Missing verified action-version metadata; provide --action-versions: "
            + ", ".join(sorted(unknown))
        )
    return rendered


def migrate(directory: Path, action_versions: dict | None = None) -> None:
    """Remove GitHub Actions entries without reserializing unrelated policies."""
    renovate = directory / "renovate.json"
    expected = {
        "$schema": "https://docs.renovatebot.com/renovate-schema.json",
        "extends": [PRESET],
    }
    if renovate.exists() and json.loads(renovate.read_text()) != expected:
        raise ValueError("Existing Renovate configuration requires an explicit review")
    dependabot = directory / ".github/dependabot.yml"
    rendered = "version: 2\nupdates:\n"
    if dependabot.exists():
        original = dependabot.read_text()
        data = yaml.safe_load(original)
        remaining = [
            entry
            for entry in data["updates"]
            if entry["package-ecosystem"] != "github-actions"
        ]
        chunks = re.split(r"(?m)(?=^ {2}- package-ecosystem:)", original)
        if len(chunks) != len(data["updates"]) + 1:
            raise ValueError("Unrecognized Dependabot formatting; review manually")
        retained = [chunks[0]]
        for chunk, entry in zip(chunks[1:], data["updates"], strict=True):
            if entry["package-ecosystem"] != "github-actions":
                retained.append(chunk)
        if remaining:
            rendered = "".join(retained).rstrip() + "\n"
            if yaml.safe_load(rendered) != {**data, "updates": remaining}:
                raise ValueError("Migration would change a non-Actions update policy")
    # open-pull-requests-limit alone does not disable security update PRs.
    # Keep an explicit ignore rule instead of deleting the Actions policy.
    # Strip our trailing comment from a prior idempotent migration.
    rendered = rendered.split("  # Renovate owns CI updates.", 1)[0].rstrip() + "\n"
    workflows = annotated_workflows(directory, action_versions)
    dependabot.parent.mkdir(parents=True, exist_ok=True)
    for path, content in workflows.items():
        path.write_text(content)
    dependabot.write_text(rendered + DISABLED_ACTIONS)
    renovate.write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "--action-versions",
        type=Path,
        help="JSON mapping of action names to verified SHA/ref pairs for legacy pins",
    )
    args = parser.parse_args()
    migrate(
        args.directory.resolve(),
        json.loads(args.action_versions.read_text()) if args.action_versions else None,
    )
