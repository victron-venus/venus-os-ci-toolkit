#!/usr/bin/env python3
"""Plan or apply the private OTTPlay CI runner switch using verified smoke runs.

Uses the operator's gh authentication; never reads billing or dispatches builds.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

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
REPOSITORY_PATH = r"repos/open-ott-play/(?:ottplay-core|ottplay-android)"
PAGE_QUERY = r"(?:\?per_page=100&page=(?:[1-9]|[1-9][0-9]|100))?"
READ_PATHS = (
    REPOSITORY_PATH,
    REPOSITORY_PATH + r"/branches/main",
    REPOSITORY_PATH + r"/actions/variables" + PAGE_QUERY,
    REPOSITORY_PATH + r"/actions/runs/[1-9][0-9]*",
    REPOSITORY_PATH
    + r"/actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*/jobs"
    + PAGE_QUERY,
    r"orgs/open-ott-play/actions/runner-groups" + PAGE_QUERY,
    r"orgs/open-ott-play/actions/runner-groups/[1-9][0-9]*/repositories" + PAGE_QUERY,
)
WRITE_PATHS = {
    "POST": REPOSITORY_PATH + r"/actions/variables",
    "PATCH": REPOSITORY_PATH + r"/actions/variables/CI_RUNNER_MODE",
}


class PreflightError(ValueError):
    """Readiness could not be established; no variable should be changed."""


def validate_api_request(path: str, method: str, payload: dict | None) -> None:
    """Limit gh to this switch's exact endpoints, methods and variable schema."""
    if not isinstance(path, str) or not isinstance(method, str):
        raise PreflightError("Unsupported GitHub API request.")
    if method == "GET":
        if payload is not None or not any(
            re.fullmatch(route, path) for route in READ_PATHS
        ):
            raise PreflightError("Unsupported GitHub API read.")
        return
    if method not in WRITE_PATHS or not re.fullmatch(WRITE_PATHS[method], path):
        raise PreflightError("Unsupported GitHub API write.")
    if type(payload) is not dict or set(payload) != {"name", "value"}:
        raise PreflightError("Unexpected runner-mode variable payload.")
    if payload["name"] != VARIABLE or payload["value"] not in ("github", "k3s"):
        raise PreflightError("Unexpected runner-mode variable payload.")


def api(path: str, method: str = "GET", payload: dict | None = None):
    """Keep credentials out of subprocess arguments and error output."""
    validate_api_request(path, method, payload)
    command = ["gh", "api", "--hostname", "github.com", "--method", method]
    if payload is not None:
        command += ["--input", "-"]
    command += ["--", path]
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


def selected_group(name: str, groups: list[dict]) -> dict:
    """Resolve one group whose policy explicitly excludes public repositories."""
    matches = [group for group in groups if group["name"] == name]
    if len(matches) != 1:
        raise PreflightError(f"Runner group {name} is missing or ambiguous.")
    group = matches[0]
    if group.get("allows_public_repositories") is not False:
        raise PreflightError(f"Runner group {name} must prohibit public repositories.")
    if group.get("visibility") != "selected":
        raise PreflightError(
            f"Runner group {name} must select repositories explicitly."
        )
    return group


def verify_group_repositories(repo: str, name: str, group_id: int) -> None:
    """Read all selected repositories and enforce the per-group private scope."""
    allowed = collection(
        f"orgs/{ORG}/actions/runner-groups/{group_id}/repositories", "repositories"
    )
    names = {item["full_name"] for item in allowed}
    maximum = {f"{ORG}/ottplay-android"}
    if name == CI_GROUP:
        maximum.add(f"{ORG}/ottplay-core")
    if repo not in names or not names <= maximum:
        raise PreflightError(f"Runner group {name} has unexpected repository access.")
    if any(item.get("private") is not True for item in allowed):
        raise PreflightError(f"Runner group {name} contains a public repository.")


def verify_release_workflows(group: dict) -> None:
    """Require GitHub's workflow fence rather than trusting a runner label."""
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


def verify_groups(repo: str, groups: list[dict]) -> dict[str, int]:
    """Check actual access policy, especially the signing pool's workflow fence."""
    required = {group for _, group in POOLS[repo.split("/")[1]].values()}
    result = {}
    for name in sorted(required):
        group = selected_group(name, groups)
        verify_group_repositories(repo, name, group["id"])
        if name == RELEASE_GROUP:
            verify_release_workflows(group)
        result[name] = group["id"]
    return result


def reviewed_main(repo: str, branch: str, expected_sha: str | None) -> str:
    """Bind activation to the exact main revision reviewed by the operator."""
    if branch != "main":
        raise PreflightError("This rollout is configured for the main branch.")
    if not isinstance(expected_sha, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40}", expected_sha
    ):
        raise PreflightError(
            "k3s activation requires --expected-sha with the reviewed full 40-character commit SHA."
        )
    expected_sha = expected_sha.lower()
    tip = api(f"repos/{repo}/branches/main")
    if tip["commit"]["sha"] != expected_sha:
        raise PreflightError(
            "Current main differs from the operator-reviewed --expected-sha."
        )
    return expected_sha


def verify_smoke(
    repo: str, metadata: dict, run_id: int, now: datetime, expected_sha: str | None
) -> tuple:
    """A recent real job is evidence even when ARC has scaled back to zero."""
    branch = metadata["default_branch"]
    expected_sha = reviewed_main(repo, branch, expected_sha)
    run = api(f"repos/{repo}/actions/runs/{run_id}")
    expected = {
        "event": "workflow_dispatch",
        "path": SMOKE_PATH,
        "status": "completed",
        "conclusion": "success",
        "head_branch": branch,
        "head_sha": expected_sha,
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


def activation_evidence(
    repo: str, metadata: dict, run_id: int | None, expected_sha: str | None
) -> tuple:
    """Only k3s activation requires current smoke evidence."""
    if not run_id or run_id < 1:
        raise PreflightError("Run runner-smoke.yml on main and supply --smoke-run ID.")
    return verify_smoke(
        repo, metadata, run_id, datetime.now(timezone.utc), expected_sha
    )


def apply_mode(repo: str, args, metadata: dict, current: dict | None, evidence) -> None:
    """Recheck evidence and the latest variable before the narrowly scoped write."""
    if (
        args.mode == "k3s"
        and activation_evidence(repo, metadata, args.smoke_run, args.expected_sha)
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
    print("Start a NEW workflow run on the intended ref; no runs were dispatched here.")


def run_switch(args) -> int:
    """Show the requested plan, and apply it only after explicit operator intent."""
    repo = f"{ORG}/{args.repo}"
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
    evidence = None
    if args.mode == "k3s":
        evidence = activation_evidence(
            repo, metadata, args.smoke_run, args.expected_sha
        )
        print("Recent smoke and runner group policies verified.")
    if not args.apply:
        print(
            "Dry run: no variable changed. Add --apply to use this mode for new runs."
        )
        return 0
    apply_mode(repo, args, metadata, current, evidence)
    return 0


def main(argv=None) -> int:
    """Read-only by default; the only write is the explicitly requested variable."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, choices=sorted(POOLS))
    parser.add_argument("--mode", choices=("github", "k3s"))
    parser.add_argument("--smoke-run", type=int, help="Successful manual smoke run ID")
    parser.add_argument(
        "--expected-sha", help="Operator-reviewed full main commit SHA required for k3s"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Write the repository variable"
    )
    args = parser.parse_args(argv)
    if args.apply and args.mode is None:
        parser.error("--apply requires --mode")
    try:
        return run_switch(args)
    except (
        OSError,
        subprocess.TimeoutExpired,
        ValueError,
        KeyError,
    ) as error:
        print(f"Runner switch stopped: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
