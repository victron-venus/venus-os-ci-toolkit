"""Configure the same qualified GitHub API client at every publication boundary."""
from __future__ import annotations


def installer() -> dict:
    return {
        "name": "Install the qualified GitHub CLI",
        "env": {"GODEBUG": "fips140=on"},
        "run": (
            'python3 scripts/install_release_gh.py --output "$RUNNER_TEMP/release-gh/gh"\n'
            'echo "$RUNNER_TEMP/release-gh" >> "$GITHUB_PATH"\n'
            'echo "GODEBUG=fips140=on" >> "$GITHUB_ENV"\n'
            'echo "GH_HOST=github.com" >> "$GITHUB_ENV"\n'
            'mkdir -p "$RUNNER_TEMP/release-gh-config"\n'
            'echo "GH_CONFIG_DIR=$RUNNER_TEMP/release-gh-config" >> "$GITHUB_ENV"\n'
        ),
    }


def apply(workflow: dict, pins: dict) -> None:
    for name in ("prepare", "candidate", "stable", "final"):
        if name not in workflow["jobs"]:
            continue
        steps = workflow["jobs"][name]["steps"]
        if not steps[0].get("uses", "").startswith("actions/checkout@"):
            raise ValueError(f"Review checkout order before installing release CLI in {name}")
        steps[1:1] = [{"uses": pins["actions/setup-go"], "with": {"go-version": "1.27.2", "cache": False}}, installer()]
