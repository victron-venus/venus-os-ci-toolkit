#!/usr/bin/env python3
"""Validate closed-build requests and stage only verified public release inputs.

This module never receives closed storage credentials or processes closed data.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

if __package__:
    from . import release_control as rc, release_versioned as lifecycle, version_plan
    from .version_receipt import verify_declared_artifacts, verify_receipts
else:
    import release_control as rc
    import release_versioned as lifecycle
    import version_plan
    from version_receipt import verify_declared_artifacts, verify_receipts

REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", re.ASCII)
TAG = re.compile(r"v" + rc.VERSION_PATTERN + r"(?:-(?:beta|rc)\.[1-9]\d*)?", re.ASCII)
INPUTS = ("action", "closed_request", "release_plan_artifact", "release_tag", "public_context_artifact")


def configuration(policy: dict) -> dict | None:
    """Require an explicit public, versioned policy and a fixed local adapter."""
    if "closed_build" not in policy:
        return None
    value = policy["closed_build"]
    if (
        not isinstance(value, dict)
        or set(value) != {"workflow", "environment", "enabled"}
        or type(value.get("enabled")) is not bool
        or not isinstance(value.get("workflow"), str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.yml", value["workflow"], re.ASCII)
        or value["workflow"] in {"release-pipeline.yml", "release-build.yml", "quality-gate.yml", "release-quality-gate.yml"}
        or value.get("environment") != "closed-build"
        or policy.get("mode") != "release"
        or policy.get("visibility") != "public"
        or policy.get("ci_execution", "github") != "github"
        or policy.get("single_entry_ci") is not True
        or not isinstance(policy.get("versioning"), dict)
    ):
        raise ValueError("Closed builds require a public versioned policy and fixed closed-build adapter")
    return value


def caller(policy: dict, event: dict, environ: dict) -> tuple[str, str]:
    """Validate the trusted workflow identity before interpreting its inputs."""
    if not isinstance(event, dict) or not isinstance(event.get("repository"), dict):
        raise ValueError("Invalid closed-build event")
    repository = event.get("repository", {})
    kind = environ.get("GITHUB_EVENT_NAME")
    sha = environ.get("GITHUB_SHA", "")
    if (
        kind not in {"push", "schedule", "workflow_dispatch"}
        or repository.get("private") is not False
        or repository.get("full_name") != policy.get("repository")
        or environ.get("GITHUB_REPOSITORY") != policy.get("repository")
        or repository.get("default_branch") != policy.get("default_branch", "main")
        or environ.get("GITHUB_REF") != "refs/heads/" + policy.get("default_branch", "main")
        or not rc.SHA_RE.fullmatch(sha)
    ):
        raise ValueError("Invalid closed-build caller")
    return kind, sha


def request(policy: dict, event: dict, environ: dict) -> dict[str, str]:
    """Reject invalid dispatches before any release allocation or secret access."""
    config = configuration(policy)
    enabled = config is not None and config["enabled"]
    kind, sha = caller(policy, event, environ)
    inputs = event.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise ValueError("Invalid dispatch inputs")
    operation = inputs.get("operation", "release") if kind == "workflow_dispatch" else "release"
    identifier = inputs.get("closed_request", "") if kind == "workflow_dispatch" else ""
    tag = inputs.get("existing_release", "") if kind == "workflow_dispatch" else ""
    expected = inputs.get("expected_sha", "")
    if (
        operation not in {"release", "closed-rebuild"}
        or not isinstance(identifier, str)
        or (identifier and not REQUEST.fullmatch(identifier))
        or not isinstance(tag, str)
        or not isinstance(expected, str)
        or (expected and expected != sha)
        or (operation == "release" and tag)
        or (not enabled and bool(identifier))
    ):
        raise ValueError("Invalid closed-build request")
    if operation == "closed-rebuild":
        validate_rebuild(enabled, tag, inputs)
    return {
        "operation": operation,
        "closed_required": str(enabled).lower(),
        "closed_request": identifier,
        "release_tag": tag,
    }


def validate_rebuild(enabled: bool, tag: str, inputs: dict) -> None:
    """An existing-release rebuild cannot allocate or promote a public version."""
    if not enabled or not TAG.fullmatch(tag) or inputs.get("version") or inputs.get("rc_tag"):
        raise ValueError("Invalid closed rebuild request")


def resolve(gh: rc.GitHub, tag: str, output: Path, current_sha: str, current_run_id: int) -> dict:
    """Verify retained qualification and download exact public bytes atomically."""
    if not TAG.fullmatch(tag) or output.exists() or output.is_symlink():
        raise ValueError("Invalid public release destination")
    info = rc.repository_info(gh)
    if info.get("private") is not False:
        raise ValueError("Closed build source must be public")
    release = gh.api(f"releases/tags/{tag}")
    assets = gh.pages(f"releases/{rc.positive(release.get('id'), 'release ID')}/assets")
    manifests = [item for item in assets if item.get("name") == rc.MANIFEST]
    rc.require(len(manifests) == 1 and manifests[0].get("size", 2_000_001) <= 2_000_000,
               "Public release manifest is missing or oversized")
    raw = gh.binary(f"releases/assets/{rc.positive(manifests[0].get('id'), 'manifest ID')}")
    rc.require(len(raw) <= 2_000_000, "Public release manifest is oversized")
    manifest = rc.parse_json(raw, "public release manifest")
    rc.require(isinstance(manifest, dict), "Invalid public manifest")
    source = manifest.get("source_sha")
    rc.require(isinstance(source, str) and rc.SHA_RE.fullmatch(source), "Invalid public source")
    snapshot = rc.source_policy_snapshot(gh, source)
    policy = snapshot["data"]
    rc.require_release_policy(policy, gh.repo, qualified=True)
    plan = version_plan.validate_plan(manifest.get("version_plan"), policy, source)
    rc._validate_manifest_assets(manifest)
    rc._validate_manifest_plan(manifest, plan["base_version"])
    run_id = rc.positive(manifest.get("run_id"), "public source run")
    rc.require(run_id != current_run_id, "Public release must come from a different run")
    rc.require_current_fuzzing(gh, policy, current_sha)
    rc.check_ancestry(gh, source, info["default_branch"])
    lifecycle.verify_scheduled_reuse(gh, info, snapshot, plan, run_id, tag)
    initial = lifecycle.published_plan_snapshot(gh, tag, source, tag != f"v{plan['base_version']}")
    expected = {entry["name"]: entry for entry in manifest["assets"]}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="closed-public-", dir=output.parent) as temporary:
        stage = Path(temporary) / "context"
        payloads = stage / "payloads"
        payloads.mkdir(parents=True)
        for asset in initial[2]:
            name = asset["name"]
            if name == rc.MANIFEST:
                continue
            identity = rc.download_asset(gh, asset["id"], payloads / name)
            rc.require(identity == {key: expected[name][key] for key in ("size", "sha256")},
                       "Public release payload changed")
        verify_receipts(payloads, plan, manifest["assets"], policy)
        verify_declared_artifacts(payloads, policy, plan)
        if policy.get("sign_releases"):
            if __package__:
                from .release_signing import verify
            else:
                from release_signing import verify
            verify(payloads, policy, source)
        # Repeat the retained-evidence checks after transfer; a rerun or deletion
        # during this read cannot turn stale inputs into a successful context.
        lifecycle.verify_scheduled_reuse(gh, info, snapshot, plan, run_id, tag)
        rc.require(rc.snapshot_identity(initial) == rc.snapshot_identity(
            lifecycle.published_plan_snapshot(gh, tag, source, tag != f"v{plan['base_version']}")),
            "Public release changed during transfer")
        context = {
            "schema": 1, "repository": gh.repo, "workflow": rc.WORKFLOW,
            "run_id": str(run_id), "run_attempt": str(manifest["run_attempt"]),
            "source_sha": source, "plan_sha256": version_plan.plan_digest(plan),
            "payloads": {name: {key: item[key] for key in ("sha256", "size")}
                         for name, item in expected.items()},
            "mode": "published", "release_tag": tag,
        }
        (stage / "context.json").write_bytes(rc.json_bytes(context))
        (stage / "version-plan.json").write_bytes(rc.json_bytes(plan))
        stage.rename(output)
    return context


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("request", "resolve"))
    parser.add_argument("--purpose", choices=("stable", "rebuild"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        policy = json.loads(Path(".release-policy.json").read_text())
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        selected = request(policy, event, dict(os.environ))
        if args.action == "request":
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                stream.write("".join(f"{key}={value}\n" for key, value in selected.items()))
            print("CLOSED_REQUEST_ACCEPTED")
            return 0
        if selected["closed_required"] != "true" or args.output is None:
            raise ValueError("Closed resolution requires an enabled policy")
        inputs = event.get("inputs", {})
        if args.purpose == "stable" and selected["operation"] == "release" and inputs.get("channel") == "stable":
            tag = inputs.get("rc_tag", "")
            if not re.fullmatch(r"v" + rc.VERSION_PATTERN + r"-rc\.[1-9]\d*", tag, re.ASCII):
                raise ValueError("Stable resolution requires an exact RC")
        elif args.purpose == "rebuild" and selected["operation"] == "closed-rebuild":
            tag = selected["release_tag"]
        else:
            raise ValueError("Resolution purpose differs from dispatch")
        gh = rc.GitHub(policy["repository"])
        info = rc.repository_info(gh)
        run_id = rc.positive(os.environ["GITHUB_RUN_ID"], "run ID")
        run = gh.api(f"actions/runs/{run_id}")
        rc.require(run.get("id") == run_id, "Resolver run ID mismatch")
        rc.validate_run(gh, run, info, os.environ["GITHUB_SHA"],
                        rc.positive(os.environ["GITHUB_RUN_ATTEMPT"], "run attempt"), False, gate=False)
        resolve(gh, tag, args.output, os.environ["GITHUB_SHA"], run_id)
        print("PUBLIC_CONTEXT_VERIFIED")
        return 0
    except (OSError, ValueError, KeyError, TypeError, rc.ReleaseError):
        # Dispatch values and parser exceptions are never echoed into logs.
        print("CLOSED_REQUEST_REJECTED" if args.action == "request" else "PUBLIC_CONTEXT_REJECTED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
