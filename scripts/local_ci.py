#!/usr/bin/env python3
"""Run checked-in local validation without requesting GitHub Actions or releases."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def configuration():
    """Reject incomplete local policies before executing any repository command."""
    policy = json.loads((ROOT / ".release-policy.json").read_text())
    if policy.get("ci_execution") != "local" or policy.get("mode") != "validation-only":
        raise ValueError("This client requires a local validation-only policy")
    commands = policy.get("local_checks")
    if not isinstance(commands, list) or not commands:
        raise ValueError("local_checks must be a nonempty list of reviewed commands")
    for command in commands:
        if not isinstance(command, str) or not command.strip():
            raise ValueError("Each local check must be a nonempty reviewed command")
    return policy


def main():
    """Execute local checks or describe configuration; never contact GitHub."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check", "status", "doctor"])
    args = parser.parse_args()
    try:
        policy = configuration()
        if args.command == "check":
            # These are checked-in repository programs, like scripts/ci.sh, not
            # commands assembled from CLI arguments or remote event inputs.
            for command in policy["local_checks"]:
                subprocess.run(
                    ["bash", "-e", "-o", "pipefail", "-c", command],
                    cwd=ROOT,
                    check=True,
                )
        else:
            print(
                json.dumps(
                    {
                        "repository": policy["repository"],
                        "ci_execution": "local",
                        "local_checks": policy["local_checks"],
                        "note": (
                            "Configuration only. Run check to validate; "
                            "no GitHub CI gate is configured."
                        ),
                    },
                    indent=2,
                )
            )
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"local-ci: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
