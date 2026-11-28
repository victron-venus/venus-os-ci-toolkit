#!/usr/bin/env python3
"""Report an incomplete test environment once, with the command that repairs it.

Most modules import the hash-locked test dependencies during discovery, so a bare
interpreter reports one import error per module instead of one actionable message.
Both `scripts/ci.sh` and pytest run this preflight before collecting tests.

Missing requirements stop the run. A `python3` that is too old for the helper
scripts and a host that cannot run the supervised release contracts only warn:
the remaining tests can still pass there, but their skips must not look green.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import async_process  # noqa: E402

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
MINIMUM_PYTHON3 = (3, 12)
RELEASE_CONTRACTS_RUNTIME = (
    "Linux Python 3.12+ or macOS Python 3.13+ with waitid(WNOWAIT)"
)


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


def python3_version() -> tuple[int, ...] | None:
    """Version of the `python3` that the suite starts its helper scripts with."""
    binary = shutil.which("python3")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "-c", "import sys; print(*sys.version_info[:3])"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return tuple(int(part) for part in result.stdout.split()[:3])
    except ValueError:
        return None


def release_contracts_skip_reason() -> str | None:
    """Why the supervised release contracts skip on this host, or None when they run."""
    try:
        async_process._require_supported_runtime()
    except NotImplementedError:
        return RELEASE_CONTRACTS_RUNTIME
    return None


def environment_warnings() -> list[str]:
    """Conditions that make a local run pass with less coverage than CI."""
    messages = []
    version = python3_version()
    if version is not None and version < MINIMUM_PYTHON3:
        messages.append(
            "`python3` on PATH is " + ".".join(str(part) for part in version) +
            "; the suite starts helper scripts with it and needs " +
            ".".join(str(part) for part in MINIMUM_PYTHON3) + " or newer.\n"
            "Activate the test environment first, as CONTRIBUTING.md documents."
        )
    reason = release_contracts_skip_reason()
    if reason is not None:
        messages.append(
            "release-process contracts will skip on this host; they need " + reason + ".\n"
            "CI runs them on Linux, so a local pass without them is not the CI result."
        )
    return messages


def report_warnings(stream=None) -> None:
    for message in environment_warnings():
        print("warning: " + message, file=stream or sys.stderr)


def main() -> int:
    absent = missing_modules()
    if absent:
        print(
            "Test environment is incomplete: missing " + ", ".join(absent) + ".\n"
            "Install the hash-locked test requirements first, as CONTRIBUTING.md documents:\n\n"
            f"    {INSTALL}\n",
            file=sys.stderr,
        )
        return 2
    report_warnings()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
