#!/usr/bin/env python3
"""Report an incomplete test environment once, with the command that repairs it.

Most modules import the hash-locked test dependencies during discovery, so a bare
interpreter reports one import error per module instead of one actionable message.
Both `scripts/ci.sh` and pytest run this preflight before collecting tests.
"""

from __future__ import annotations

import importlib.util
import sys

INSTALL = (
    "python3 -m pip install --require-hashes --only-binary=:all: "
    "-r .github/requirements-generator.txt -r tools/secret_control/requirements-github-lock.txt"
)
# Top-level module names, as imported by the suite.
REQUIRED = {
    "yaml": "PyYAML",
    "ruamel": "ruamel.yaml",
    "nacl": "PyNaCl",
}


def missing_modules() -> list[str]:
    """Installed requirement names that the suite imports at discovery time."""
    absent = []
    for module, distribution in REQUIRED.items():
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            absent.append(distribution)
    return absent


def main() -> int:
    absent = missing_modules()
    if not absent:
        return 0
    print(
        "Test environment is incomplete: missing " + ", ".join(absent) + ".\n"
        "Install the hash-locked test requirements first, as CONTRIBUTING.md documents:\n\n"
        f"    {INSTALL}\n",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
