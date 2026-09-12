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


def run(args, directory, *, capture=False, check=True):
    """Run one command in its selected repository, optionally capturing its output."""
    return subprocess.run(
        args, cwd=directory, text=True, capture_output=capture, check=check
    )


def validate_identity(directory, item):
    """Require both origin URLs and the local policy to match the fleet entry."""
    policy = json.loads((directory / ".release-policy.json").read_text())
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


def main():
    """Execute one fleet operation with repository identity and branch safeguards."""
    # Keep audited submission steps together; no writes occur outside --execute.
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    # pylint: disable=too-many-nested-blocks
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
    args = parser.parse_args()
    manifest = json.loads((ROOT / "fleet.json").read_text())
    rows, failed = [], False
    for item in manifest["repositories"]:
        if item.get("excluded_reason") or (
            args.repo and item["repository"] not in args.repo
        ):
            continue
        directory = args.root / item["directory"]
        row = {"repository": item["repository"], "directory": str(directory)}
        try:
            if directory.resolve().parent != args.root.resolve():
                raise ValueError("Repository directory escapes the selected fleet root")
            if args.command in ["check", "submit"]:
                validate_identity(directory, item)
            if args.command in ["check", "submit"]:
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
                workflows = sorted(
                    str(p) for p in (directory / ".github/workflows").glob("*.yml")
                )
                run(["actionlint", "-shellcheck=", "-pyflakes=", *workflows], directory)
            if args.command == "render":
                run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/install_release.py"),
                        str(directory),
                    ],
                    ROOT,
                )
            if args.command == "submit":
                branch = run(
                    ["git", "branch", "--show-current"], directory, capture=True
                ).stdout.strip()
                if branch != item.get(
                    "branch", "ci/release-standard"
                ) or not branch.startswith("ci/release-standard"):
                    raise ValueError(f"Refusing to submit unexpected branch {branch!r}")
                row["changes"] = run(
                    ["git", "status", "--short"], directory, capture=True
                ).stdout.strip()
                if args.execute:
                    # These are dedicated migration worktrees; inspect `status`
                    # and test results before using --execute.
                    if row["changes"]:
                        run(["git", "add", "--all"], directory)
                        run(
                            [
                                "git",
                                "commit",
                                "-m",
                                (
                                    "ci: gate releases through nightly, candidates and verified "
                                    "promotion"
                                ),
                            ],
                            directory,
                        )
                    run(["git", "push", "--set-upstream", "origin", branch], directory)
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
                    if prs:
                        row["pr"] = prs[0]["url"]
                    else:
                        policy = json.loads(
                            (directory / ".release-policy.json").read_text()
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
                                "retired.\n\n"
                            )
                        else:
                            body += (
                                "This repository uses validation-only policy. "
                                "Infrastructure/site deployments remain explicit and use "
                                "protected environments where configured.\n\n"
                            )
                        body += (
                            "Validation: local workflow schema checks and release-tooling "
                            "contract tests. Project-specific test/build evidence and "
                            "limits are recorded in docs/release-workflow.md and the fleet "
                            "rollout report. Hosted checks must pass before merging.\n\n"
                            "Rollout: merge the workflow first, then enable the additive "
                            "Terraform CI gate and release/production environment policies "
                            "for this repository. This PR does not apply Terraform or "
                            "deploy production.\n"
                        )
                        if policy.get("stable_blockers"):
                            body += (
                                "\nRC/stable remain blocked at the candidate "
                                "source revision:\n\n"
                            )
                            body += "".join(
                                "- " + reason + "\n"
                                for reason in policy["stable_blockers"]
                            )
                        with tempfile.NamedTemporaryFile("w", suffix=".md") as handle:
                            handle.write(body)
                            handle.flush()
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
                                    "ci: add gated nightly and release workflows",
                                    "--body-file",
                                    handle.name,
                                ],
                                directory,
                                capture=True,
                            )
                        row["pr"] = result.stdout.strip()
                else:
                    row["action"] = (
                        "Review changes, then rerun submit --execute to create a draft PR"
                    )
            elif args.command == "status":
                row["branch"] = run(
                    ["git", "branch", "--show-current"], directory, capture=True
                ).stdout.strip()
                row["changes"] = run(
                    ["git", "status", "--short"], directory, capture=True
                ).stdout.strip()
            row["ok"] = True
        except (OSError, ValueError, subprocess.CalledProcessError) as exc:
            row.update(ok=False, error=str(exc))
            failed = True
        rows.append(row)
        print(json.dumps(row), flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
