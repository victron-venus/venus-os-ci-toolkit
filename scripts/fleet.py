#!/usr/bin/env python3
"""Audit, render, validate and submit the reviewed CI release migration fleet.

Defaults are read-only. Submission requires --execute and creates draft PRs;
merging, release publication and Terraform apply are separate operations.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_FILE = ".release-policy.json"


def run(args, directory, *, capture=False, check=True):
    """Run one command in its selected repository, optionally capturing its output."""
    return subprocess.run(
        args, cwd=directory, text=True, capture_output=capture, check=check
    )


def validate_identity(directory, item):
    """Require both origin URLs and the local policy to match the fleet entry."""
    policy = json.loads((directory / POLICY_FILE).read_text())
    if policy.get("repository") != item["repository"]:
        raise ValueError("Local release policy does not match the fleet repository")
    for extra in ([], ["--push"]):
        remote = run(
            ["git", "remote", "get-url", *extra, "origin"], directory, capture=True
        ).stdout.strip()
        match = re.fullmatch(
            r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
            r"([^/]+/[^/]+?)(?:\.git)?",
            remote,
        )
        if not match or match[1].casefold() != item["repository"].casefold():
            raise ValueError(
                "Origin fetch/push identity does not match the fleet repository"
            )


def validate_generated_tracking(directory):
    """Reject generated files that an ordinary git add would silently omit."""
    policy = json.loads((directory / POLICY_FILE).read_text())
    required = [
        "scripts/release.py",
        "docs/release-workflow.md",
    ]
    if policy.get("ci_execution") != "local":
        required.append(".github/workflows/quality-gate.yml")
    if policy.get("mode", "release") == "release":
        required += [
            "RELEASING.md",
            "scripts/release_control.py",
            ".github/workflows/release-pipeline.yml",
            ".github/release-tests/test_release_control.py",
        ]
        if policy.get("container_assets") or policy.get("pypi_assets"):
            required.append("scripts/publish_verified.py")
        if policy.get("container_assets"):
            required.append("scripts/verified_images.py")
    # Do not pass --no-index: an already tracked path remains addable even if
    # a repository's ignore rules would exclude a newly generated copy.
    result = run(
        ["git", "check-ignore", "--", *required], directory, capture=True, check=False
    )
    if result.returncode not in (0, 1):
        result.check_returncode()
    if result.stdout.strip():
        raise ValueError(
            "Required generated files are ignored by Git: "
            + ", ".join(result.stdout.splitlines())
        )


def check_repository(directory, item):
    """Validate identity, staged contents and generated workflows before submission."""
    validate_identity(directory, item)
    validate_generated_tracking(directory)
    run(["git", "diff", "--check"], directory)
    run(["git", "diff", "--cached", "--check"], directory)
    run(
        [
            sys.executable,
            str(ROOT / "scripts/install_release.py"),
            str(directory),
            "--check",
        ],
        ROOT,
    )
    workflows = sorted(str(p) for p in (directory / ".github/workflows").glob("*.yml"))
    if workflows:
        run(["actionlint", "-shellcheck=", "-pyflakes=", *workflows], directory)


def submission_body(policy):
    """Describe the reviewed migration and its source-specific release blockers."""
    if policy.get("ci_execution") == "local":
        return (
            "Private GitHub-hosted checks cannot run with the existing account "
            "configuration. This change removes unavailable hosted workflows "
            "and automatic approval/merge callers "
            "from active workflow discovery, retaining their definitions as archived text.\n\n"
            "Validation and security checks remain available through "
            "python3 scripts/release.py check. The English runbook documents "
            "local nightlies and maintainer review; there is no required "
            "GitHub-hosted CI gate or paid security/environment feature. Existing working manual "
            "self-hosted deployment is preserved where applicable.\n\n"
            "Validation: local client rejection/ordering contracts, renderer checks and applicable "
            "project checks. Existing security findings remain failures in local validation and "
            "are documented in docs/security-checks.md where present. No paid settings, runner "
            "installation, Terraform apply or production deployment are part of this PR.\n"
        )
    body = (
        "CI and release handling need a consistent validation and "
        "operating policy across the repository fleet. "
        "The migration adds a mandatory CI gate, nightly validation, "
        "and local scripts that run the checked-in validation and "
        "packaging commands.\n\n"
    )
    if policy.get("mode", "release") == "release":
        body += (
            "Application builds now produce immutable beta/RC artifacts "
            "after validation. Stable publication requires an approved RC, "
            "matching source SHA, successful Actions provenance and "
            "verified asset checksums; it copies the RC bytes without "
            "rebuilding. Independent tag/main/latest publishers are "
            "retired. The English release strategy is in RELEASING.md; "
            "commands and recovery instructions are in docs/release-workflow.md, "
            "with brief README links.\n\n"
        )
    else:
        body += (
            "This repository uses validation-only policy. "
            "Infrastructure/site deployments remain explicit and validate "
            "the reviewed source revision.\n\n"
        )
    if policy.get("visibility") == "private":
        body += (
            "Private repositories require no paid GitHub security or governance "
            "features. OSS security scans fail on findings; scheduled hosted jobs "
            "require NIGHTLY_CHECKS_ENABLED=true. Local checks need no hosted quota.\n\n"
        )
    body += (
        "Validation: local workflow schema checks and release-tooling "
        "contract tests. Project-specific test/build evidence and "
        "limits are recorded in docs/release-workflow.md and the fleet "
        "rollout report. Hosted checks must pass before merging.\n\n"
        "Rollout: merge and verify the workflows first. Public repositories can "
        "opt into the Terraform CI gate and environment policies; private "
        "repositories use the documented manual process without paid features. "
        "This PR does not apply Terraform or deploy production.\n"
    )
    if policy.get("stable_blockers"):
        body += "\nRC/stable remain blocked at the candidate source revision:\n\n"
        body += "".join("- " + reason + "\n" for reason in policy["stable_blockers"])
    return body


def ensure_draft_pr(directory, item, branch):
    """Reuse an existing open PR or create one draft from the checked-in policy."""
    prs = json.loads(
        run(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                item["repository"],
                "--head",
                branch,
                "--state",
                "open",
                "--json",
                "url",
            ],
            directory,
            capture=True,
        ).stdout
    )
    policy = json.loads((directory / POLICY_FILE).read_text())
    title = (
        "ci: add gated release channels and release strategy"
        if policy.get("mode", "release") == "release"
        else "ci: add nightly validation and local operations"
    )
    if policy.get("ci_execution") == "local":
        title = "ci: replace unavailable private workflows with local checks"
    with tempfile.NamedTemporaryFile("w", suffix=".md") as handle:
        handle.write(submission_body(policy))
        handle.flush()
        if prs:
            run(
                [
                    "gh",
                    "pr",
                    "edit",
                    prs[0]["url"],
                    "--title",
                    title,
                    "--body-file",
                    handle.name,
                ],
                directory,
            )
            return prs[0]["url"]
        result = run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                item["repository"],
                "--base",
                item.get("default_branch", "main"),
                "--head",
                branch,
                "--draft",
                "--title",
                title,
                "--body-file",
                handle.name,
            ],
            directory,
            capture=True,
        )
    return result.stdout.strip()


def submit_repository(directory, item, execute, row):
    """Guard the branch and require explicit execution before any Git or PR writes."""
    branch = run(
        ["git", "branch", "--show-current"], directory, capture=True
    ).stdout.strip()
    if branch != item.get("branch", "ci/release-standard") or not branch.startswith(
        "ci/release-standard"
    ):
        raise ValueError(f"Refusing to submit unexpected branch {branch!r}")
    changes = run(["git", "status", "--short"], directory, capture=True).stdout.strip()
    row["changes"] = changes
    if not execute:
        row["action"] = (
            "Review changes, then rerun submit --execute to create a draft PR"
        )
        return row
    # These are dedicated migration worktrees; inspect status and test results
    # before using --execute. All checks run before this function is called.
    if changes:
        run(["git", "add", "--all"], directory)
        run(
            [
                "git",
                "commit",
                "-m",
                "ci: gate releases through nightly, candidates and verified promotion",
            ],
            directory,
        )
    run(["git", "push", "--set-upstream", "origin", branch], directory)
    row["pr"] = ensure_draft_pr(directory, item, branch)
    return row


def process_repository(directory, item, args, row):
    """Run one selected operation after checking directory and submission boundaries."""
    if directory.resolve().parent != args.root.resolve():
        raise ValueError("Repository directory escapes the selected fleet root")
    if args.command in ["check", "submit"]:
        check_repository(directory, item)
    if args.command == "render":
        run(
            [sys.executable, str(ROOT / "scripts/install_release.py"), str(directory)],
            ROOT,
        )
    if args.command == "submit":
        return submit_repository(directory, item, args.execute, row)
    if args.command == "status":
        return {
            "branch": run(
                ["git", "branch", "--show-current"], directory, capture=True
            ).stdout.strip(),
            "changes": run(
                ["git", "status", "--short"], directory, capture=True
            ).stdout.strip(),
        }
    return {}


def parse_args():
    """Parse the requested fleet operation, subset and explicit submission flag."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["status", "render", "check", "submit"])
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT.parent,
        help="Directory containing the isolated repository worktrees",
    )
    parser.add_argument(
        "--repo", action="append", help="Limit to OWNER/REPO (repeatable)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Commit, push a feature branch and create/update a draft PR",
    )
    return parser.parse_args()


def main():
    """Report each selected repository independently, preserving failures in JSONL."""
    args = parse_args()
    manifest = json.loads((ROOT / "fleet.json").read_text())
    failed = False
    for item in manifest["repositories"]:
        if item.get("excluded_reason") or (
            args.repo and item["repository"] not in args.repo
        ):
            continue
        directory = args.root / item["directory"]
        row = {"repository": item["repository"], "directory": str(directory)}
        try:
            row.update(process_repository(directory, item, args, row))
            row["ok"] = True
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            row.update(ok=False, error=str(exc))
            failed = True
        print(json.dumps(row), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
