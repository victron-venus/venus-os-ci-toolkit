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
            dependabot.write_text(rendered)
        else:
            dependabot.unlink()
    renovate.write_text(json.dumps(expected, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    migrate(parser.parse_args().directory.resolve())
