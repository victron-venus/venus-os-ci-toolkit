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


def migrate(directory: Path) -> None:
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
        chunks = re.split(r"(?m)(?=^  - package-ecosystem:)", original)
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
    dependabot.write_text(rendered + DISABLED_ACTIONS)
    renovate.write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    migrate(parser.parse_args().directory.resolve())
