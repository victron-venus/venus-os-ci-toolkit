#!/usr/bin/env python3
"""Plan or apply the private OTTPlay CI runner switch using verified smoke runs.

Uses the operator's gh authentication; never reads billing or dispatches builds.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

ORG = "open-ott-play"
VARIABLE = "CI_RUNNER_MODE"
SMOKE_PATH = ".github/workflows/runner-smoke.yml"
CI_GROUP = "ottplay-private-ci"
RELEASE_GROUP = "ottplay-private-release"
POOLS = {
    "ottplay-core": {"smoke-linux": ("ottplay-k3s-linux-x64", CI_GROUP)},
    "ottplay-android": {
        "smoke-linux": ("ottplay-k3s-linux-x64", CI_GROUP),
        "smoke-kvm": ("ottplay-k3s-kvm-x64", CI_GROUP),
        "smoke-release": ("ottplay-k3s-release-x64", RELEASE_GROUP),
    },
}


class PreflightError(ValueError):
    """Readiness could not be established; no variable should be changed."""


def api(path: str, method: str = "GET", payload: dict | None = None):
    """Keep credentials out of subprocess arguments and error output."""
    command = ["gh", "api", "--hostname", "github.com", path, "--method", method]
    if payload is not None:
        command += ["--input", "-"]
    result = subprocess.run(
        command,
        input=json.dumps(payload) if payload is not None else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=45,
    )
    if result.returncode:
        raise PreflightError(
            f"GitHub API {method} {path} failed; check gh permissions."
        )
    return json.loads(result.stdout) if result.stdout.strip() else None


def collection(path: str, key: str) -> list:
    """Read every page so a successful first page cannot hide conflicting state."""
    result = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        data = api(f"{path}{separator}per_page=100&page={page}")[key]
        result.extend(data)
        if len(data) < 100:
            return result
    raise PreflightError(f"Pagination limit reached for {path}.")


def repo_state(repo: str) -> tuple[dict, dict | None]:
    """This switch deliberately excludes public/free hosted consumers."""
    metadata = api(f"repos/{repo}")
    if metadata.get("private") is not True or metadata.get("full_name") != repo:
        raise PreflightError("Only the selected private repository may be switched.")
    variables = collection(f"repos/{repo}/actions/variables", "variables")
    variable = next((v for v in variables if v["name"] == VARIABLE), None)
    value = variable["value"] if variable else None
    if value not in (None, "github", "k3s"):
        raise PreflightError(
            f"Unexpected {VARIABLE} value; resolve it explicitly first."
        )
    return metadata, variable


def verify_groups(repo: str, groups: list[dict]) -> dict[str, int]:
    """Check actual access policy, especially the signing pool's workflow fence."""
    required = {group for _, group in POOLS[repo.split("/")[1]].values()}
    result = {}
    for name in sorted(required):
        matches = [group for group in groups if group["name"] == name]
        if len(matches) != 1:
            raise PreflightError(f"Runner group {name} is missing or ambiguous.")
        group = matches[0]
        if group.get("allows_public_repositories") is not False:
            raise PreflightError(
                f"Runner group {name} must prohibit public repositories."
            )
        if group.get("visibility") != "selected":
            raise PreflightError(
                f"Runner group {name} must select repositories explicitly."
            )
        allowed = collection(
            f"orgs/{ORG}/actions/runner-groups/{group['id']}/repositories",
            "repositories",
        )
        names = {item["full_name"] for item in allowed}
        maximum = {f"{ORG}/ottplay-android"}
        if name == CI_GROUP:
            maximum.add(f"{ORG}/ottplay-core")
        if repo not in names or not names <= maximum:
            raise PreflightError(
                f"Runner group {name} has unexpected repository access."
            )
        if any(item.get("private") is not True for item in allowed):
            raise PreflightError(f"Runner group {name} contains a public repository.")
        if name == RELEASE_GROUP:
            workflows = {
                f"{ORG}/ottplay-android/.github/workflows/{file}@refs/heads/main"
                for file in ("release.yml", "runner-smoke.yml")
            }
            if (
                group.get("restricted_to_workflows") is not True
                or set(group.get("selected_workflows", [])) != workflows
            ):
                raise PreflightError(
                    "Release group requires enforced workflow restrictions for "
                    "release.yml and runner-smoke.yml on main; labels alone are insufficient."
                )
        result[name] = group["id"]
    return result


def verify_smoke(repo: str, metadata: dict, run_id: int, now: datetime) -> tuple:
    """A recent real job is evidence even when ARC has scaled back to zero."""
    branch = metadata["default_branch"]
    if branch != "main":
        raise PreflightError(
            "This rollout is configured for the protected main branch."
        )
    tip = api(f"repos/{repo}/branches/{quote(branch, safe='')}")
    if tip.get("protected") is not True:
        raise PreflightError("The default branch must be protected before activation.")
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    expected = {
        "event": "workflow_dispatch",
        "path": SMOKE_PATH,
        "status": "completed",
        "conclusion": "success",
        "head_branch": branch,
        "head_sha": tip["commit"]["sha"],
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise PreflightError(
            "Smoke must succeed from runner-smoke.yml at current main SHA."
        )
    if run.get("head_repository", {}).get("full_name") != repo:
        raise PreflightError("Smoke source repository does not match.")
    created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    age = now - created
    if age < timedelta(0) or age > timedelta(hours=24):
        raise PreflightError(
            "Smoke must be less than 24 hours old; rerun after pool changes."
        )
    groups = verify_groups(
        repo, collection(f"orgs/{ORG}/actions/runner-groups", "runner_groups")
    )
    attempt = run.get("run_attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise PreflightError("Smoke run has no valid attempt number.")
    jobs = collection(
        f"repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs"
    )
    for name, (label, group) in POOLS[repo.split("/")[1]].items():
        matches = [job for job in jobs if job.get("name") == name]
        if len(matches) != 1:
            raise PreflightError(f"Smoke job {name} is missing or ambiguous.")
        job = matches[0]
        if (
            job.get("status") != "completed"
            or job.get("conclusion") != "success"
            or job.get("labels") != [label]
            or job.get("runner_group_id") != groups[group]
            or not job.get("runner_name")
        ):
            raise PreflightError(
                f"Smoke job {name} did not pass on {label} in {group}."
            )
    return (run["head_sha"], attempt, run["created_at"], tuple(sorted(groups.items())))


def main(argv=None) -> int:
    """Read-only by default; the only write is the explicitly requested variable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, choices=sorted(POOLS))
    parser.add_argument("--mode", choices=("github", "k3s"))
    parser.add_argument("--smoke-run", type=int, help="Successful manual smoke run ID")
    parser.add_argument(
        "--apply", action="store_true", help="Write the repository variable"
    )
    args = parser.parse_args(argv)
    if args.apply and args.mode is None:
        parser.error("--apply requires --mode")
    repo = f"{ORG}/{args.repo}"
    try:
        metadata, current = repo_state(repo)
        state = (
            current["value"]
            if current
            else "repository override unset; organization value not inspected"
        )
        print(f"{repo}: {VARIABLE}={state}")
        if args.mode is None:
            return 0
        print(
            f"Requested: {args.mode}; required k3s pools: "
            + ", ".join(label for label, _ in POOLS[args.repo].values())
        )
        if args.mode == "k3s":
            if not args.smoke_run or args.smoke_run < 1:
                raise PreflightError(
                    "Run runner-smoke.yml on main and supply --smoke-run ID."
                )
            evidence = verify_smoke(
                repo, metadata, args.smoke_run, datetime.now(timezone.utc)
            )
            print("Recent smoke and runner group policies verified.")
        if not args.apply:
            print(
                "Dry run: no variable changed. Add --apply to use this mode for new runs."
            )
            return 0
        if (
            args.mode == "k3s"
            and verify_smoke(repo, metadata, args.smoke_run, datetime.now(timezone.utc))
            != evidence
        ):
            raise PreflightError(
                "Smoke evidence changed during preflight; inspect and retry."
            )
        # Last read before the write; GitHub Variables has no atomic compare-and-swap.
        latest_metadata, latest = repo_state(repo)
        if (
            latest != current
            or latest_metadata["default_branch"] != metadata["default_branch"]
        ):
            raise PreflightError(
                "Mode or default branch changed during preflight; inspect and retry."
            )
        path = f"repos/{repo}/actions/variables"
        payload = {"name": VARIABLE, "value": args.mode}
        api(
            path if current is None else f"{path}/{VARIABLE}",
            "POST" if current is None else "PATCH",
            payload,
        )
        _, actual = repo_state(repo)
        if actual is None or actual["value"] != args.mode:
            raise PreflightError(
                "Write could not be verified; inspect the repository variable."
            )
        print(
            f"Verified {VARIABLE}={actual['value']}. Existing queued/running jobs are unchanged."
        )
        print(
            "Start a NEW workflow run on the intended ref; no runs were dispatched here."
        )
        return 0
    except (
        PreflightError,
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
        KeyError,
    ) as error:
        print(f"Runner switch stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
