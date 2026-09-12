#!/usr/bin/env python3
"""Run serial local fleet checks without updating source or requesting releases."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
ORIGIN = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([^/]+/[^/]+?)(?:\.git)?\Z"
)


def timestamp():
    """Use UTC in persistent reports regardless of scheduler timezone."""
    return datetime.now(timezone.utc).isoformat()


def inventory_entries(manifest):
    """Validate inventory identities and direct child directory names."""
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise ValueError("Expected fleet inventory schema 1")
    if not isinstance(manifest.get("repositories"), list):
        raise ValueError("Inventory repositories must be a list")
    entries = {}
    directories = set()
    for item in manifest["repositories"]:
        if not isinstance(item, dict):
            raise ValueError("Inventory repositories must be objects")
        name, directory = item["repository"], item["directory"]
        if not isinstance(name, str) or not REPOSITORY.fullmatch(name):
            raise ValueError("Invalid inventory repository identity")
        if (
            not isinstance(directory, str)
            or directory in {".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", directory)
        ):
            raise ValueError("Inventory directories must be single path components")
        if name.casefold() in entries or directory.casefold() in directories:
            raise ValueError("Duplicate inventory repository or directory")
        entries[name.casefold()] = item
        directories.add(directory.casefold())
    return entries


def selected_repositories(inventory, names):
    """Reject ambiguous paths and unknown selections before executing any checks."""
    entries = inventory_entries(json.loads(inventory.read_text(encoding="utf-8")))
    requested = {name.casefold() for name in names or []}
    unknown = requested - entries.keys()
    if unknown:
        raise ValueError("Unknown repository selection: " + ", ".join(sorted(unknown)))
    selected = [
        item
        for name, item in entries.items()
        if (not requested or name in requested) and not item.get("excluded_reason")
    ]
    if requested - {item["repository"].casefold() for item in selected}:
        raise ValueError("An explicitly selected repository is excluded from the fleet")
    if not selected:
        raise ValueError("No active repositories selected")
    return selected


def git(directory, *args, required=True):
    """Read local Git state without optional index writes or fsmonitor hooks."""
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if required and result.returncode:
        raise ValueError("Cannot read local Git state: " + args[0])
    return result.stdout.strip() if result.returncode == 0 else None


def inspect_checkout(root, item):
    """Confine the checkout and verify origin, policy and tracked check entrypoints."""
    directory = root / item["directory"]
    if directory.is_symlink() or directory.resolve().parent != root:
        raise ValueError("Repository checkout escapes the selected root")
    if Path(git(directory, "rev-parse", "--show-toplevel")).resolve() != directory:
        raise ValueError("Selected path is not the repository root")
    files = (".release-policy.json", "scripts/release.py", "scripts/ci.sh")
    for name in files:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.resolve() != path:
            raise ValueError(
                "Check entrypoints must be regular files inside the checkout"
            )
    git(directory, "ls-files", "--error-unmatch", "--", *files)
    policy = json.loads((directory / files[0]).read_text(encoding="utf-8"))
    if policy.get("repository") != item["repository"]:
        raise ValueError("Local policy does not match the fleet repository")
    for extra in ([], ["--push"]):
        match = ORIGIN.fullmatch(git(directory, "remote", "get-url", *extra, "origin"))
        if not match or match[1].casefold() != item["repository"].casefold():
            raise ValueError("Origin identity does not match the fleet repository")
    default_branch = item.get("default_branch", "main")
    git(directory, "check-ref-format", "refs/heads/" + default_branch)
    head = git(directory, "rev-parse", "HEAD")
    recorded_origin = git(
        directory,
        "rev-parse",
        "--verify",
        "refs/remotes/origin/" + default_branch,
        required=False,
    )
    return {
        "checkout": str(directory),
        "head": head,
        "committed_at": git(directory, "show", "-s", "--format=%cI", "HEAD"),
        "branch": git(directory, "branch", "--show-current") or "(detached)",
        "dirty": bool(
            git(directory, "status", "--porcelain", "--untracked-files=normal")
        ),
        "local_origin_ref": "origin/" + default_branch,
        "local_origin_sha": recorded_origin,
        "matches_local_origin": head == recorded_origin if recorded_origin else None,
        "remote_freshness": "unknown: no fetch or GitHub request performed",
    }


def stop_process_group(process):
    """Terminate descendants even if the group leader exits before its children."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def run_check(directory, log, timeout):
    """Bound one local check and terminate its process group on timeout/interruption."""
    with subprocess.Popen(
        [sys.executable, "scripts/release.py", "check"],
        cwd=directory,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    ) as process:
        try:
            return process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            stop_process_group(process)
            raise


def check_repository(root, item, log_path, timeout):
    """Persist one result, including preflight failures, and keep checking the fleet."""
    row = {
        "repository": item["repository"],
        "started_at": timestamp(),
        "log": str(log_path),
        "status": "failed",
    }
    with log_path.open("x", encoding="utf-8") as log:
        try:
            row.update(inspect_checkout(root, item))
            log.write(json.dumps(row, indent=2) + "\n\n")
            log.flush()
            if row["dirty"]:
                raise ValueError(
                    "Working tree has uncommitted changes; no files were reset"
                )
            row["returncode"] = run_check(row["checkout"], log, timeout)
            row["status"] = "passed" if row["returncode"] == 0 else "failed"
        except subprocess.TimeoutExpired:
            row.update(status="timeout", error=f"Check exceeded {timeout:g} seconds")
        except KeyboardInterrupt:
            row.update(status="interrupted", error="Local run interrupted")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            row["error"] = str(exc)
        row["finished_at"] = timestamp()
        log.write("\n\nRESULT\n" + json.dumps(row, indent=2) + "\n")
        log.flush()
        os.fsync(log.fileno())
    return row


@contextmanager
def locked_logs(path):
    """Create private reports and reject concurrent runs using the same log root."""
    if path.is_symlink():
        raise ValueError("Log root must not be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        path / ".nightly.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another nightly run already owns this log root") from exc
        yield


def save_summary(directory, summary):
    """Replace the summary after each repository, retaining an interrupted run's results."""
    path = directory / "summary.tmp"
    with path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    path.replace(directory / "summary.json")


def run_fleet(args, items):
    """Run checks serially with unique logs and a summary updated after every result."""
    directory = Path(
        tempfile.mkdtemp(
            prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=args.logs
        )
    )
    summary = {
        "started_at": timestamp(),
        "status": "running",
        "results": [],
        "scope": "local checks only; not release qualification or remote HEAD verification",
    }
    save_summary(directory, summary)
    print("Local nightly report: " + str(directory / "summary.json"), flush=True)
    with (directory / "results.jsonl").open("x", encoding="utf-8") as results:
        for index, item in enumerate(items, start=1):
            row = check_repository(
                args.root,
                item,
                directory / f"{index:03d}-{item['directory']}.log",
                args.timeout,
            )
            summary["results"].append(row)
            results.write(json.dumps(row) + "\n")
            results.flush()
            os.fsync(results.fileno())
            save_summary(directory, summary)
            print(f"{row['status']}: {row['repository']} ({row['log']})", flush=True)
            if row["status"] == "interrupted":
                break
    passed = all(row["status"] == "passed" for row in summary["results"])
    summary.update(status="passed" if passed else "failed", finished_at=timestamp())
    save_summary(directory, summary)
    return 0 if passed else 1


def main():
    """Select local repositories; never fetch, switch branches or install a scheduler."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Parent of existing repository checkouts",
    )
    parser.add_argument(
        "--logs",
        type=Path,
        required=True,
        help="Persistent log directory outside selected checkouts",
    )
    parser.add_argument(
        "--repo",
        action="append",
        help="OWNER/REPO to check (repeatable; default: all active entries)",
    )
    parser.add_argument("--inventory", type=Path, default=ROOT / "fleet.json")
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600,
        help="Maximum seconds per repository (default: 3600)",
    )
    args = parser.parse_args()
    try:
        if not 0 < args.timeout <= 86400:
            raise ValueError("Timeout must be positive and at most 86400 seconds")
        args.root = args.root.resolve(strict=True)
        items = selected_repositories(args.inventory, args.repo)
        if any(
            args.logs.resolve().is_relative_to(args.root / item["directory"])
            for item in items
        ):
            raise ValueError("Logs must be outside the selected checkouts")
        with locked_logs(args.logs):
            args.logs = args.logs.resolve()
            return run_fleet(args, items)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"local-nightly: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
