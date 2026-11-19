"""Configure the same qualified GitHub API client at every publication boundary."""
from __future__ import annotations

RELEASE_PYTHON_VERSION = "3.13"


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
    for name in ("prepare", "candidate", "stable", "final", "closed_stable_resolve", "closed_existing_resolve"):
        if name not in workflow["jobs"]:
            continue
        steps = workflow["jobs"][name]["steps"]
        if not steps[0].get("uses", "").startswith("actions/checkout@"):
            raise ValueError(f"Review checkout order before installing release CLI in {name}")
        python_steps = [step for step in steps if step.get("uses", "").startswith("actions/setup-python@")]
        if len(python_steps) > 1:
            raise ValueError(f"Multiple Python runtimes configured before release CLI installation in {name}")
        python_setup = python_steps[0] if python_steps else {
            "uses": pins["actions/setup-python"],
            "with": {"python-version": RELEASE_PYTHON_VERSION},
        }
        if python_setup.get("uses") != pins["actions/setup-python"]:
            raise ValueError(f"Release CLI requires the pinned Python setup action in {name}")
        if "if" in python_setup or "continue-on-error" in python_setup:
            raise ValueError(
                f"Release CLI Python setup must be unconditional and fail closed in {name}"
            )
        python_setup.setdefault("with", {})["python-version"] = RELEASE_PYTHON_VERSION
        if python_steps:
            steps.remove(python_setup)
        steps[1:1] = [
            python_setup,
            {"uses": pins["actions/setup-go"], "with": {"go-version": "1.27.4", "cache": False}},
            installer(),
        ]
