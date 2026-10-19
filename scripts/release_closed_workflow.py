"""Add reviewed closed-build branches without changing the public gate inventory."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from release_closed import INPUTS, configuration
from workflow_contracts import UniqueKeyLoader

RUNNER = "ubuntu-24.04"


def validate_adapter(root: Path, policy: dict) -> None:
    config = configuration(policy)
    if config is None:
        return
    path = root / ".github/workflows" / config["workflow"]
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Closed worker must be a regular local workflow")
    source = path.read_text()
    workflow = yaml.load(source, Loader=UniqueKeyLoader)
    call = workflow.get("on", {}).get("workflow_call", {})
    jobs = workflow.get("jobs", {})
    if (
        set(workflow.get("on", {})) != {"workflow_call"}
        or set(call.get("inputs", {})) != set(INPUTS)
        or any(value.get("type") != "string" for value in call["inputs"].values())
        or call.get("secrets")
        or len(jobs) != 1
    ):
        raise ValueError("Closed worker must declare only the reviewed reusable interface")
    job = next(iter(jobs.values()))
    if (
        job.get("runs-on") != RUNNER
        or job.get("environment") != config["environment"]
        or "uses" in job or "outputs" in job or "container" in job or "services" in job
        or job.get("continue-on-error", "false") != "false"
        or job.get("secrets") == "inherit"
        or re.search(r"secrets\s*\[", source)
    ):
        raise ValueError("Closed worker requires the fixed hosted environment without public outputs")
    for permissions in (workflow.get("permissions", {}), job.get("permissions", {})):
        if not isinstance(permissions, dict) or any(
            key not in {"contents", "actions"} or value not in {"read", "none"}
            for key, value in permissions.items()
        ):
            raise ValueError("Closed worker permissions must be read-only")
    for step in job.get("steps", []):
        if re.match(r"actions/(?:upload-artifact|upload-pages-artifact|cache)[@/]", step.get("uses", "")):
            raise ValueError("Closed worker cannot upload public artifacts or cache its workspace")


def condition(expression: str) -> str:
    return "${{ " + expression + " }}"


def closed_result(job: str) -> str:
    return (
        "((needs.prepare.outputs.closed_required == 'true' && "
        f"needs.{job}.result == 'success') || "
        "(needs.prepare.outputs.closed_required == 'false' && "
        f"needs.{job}.result == 'skipped'))"
    )


def apply(workflow: dict, policy: dict, pins: dict) -> None:
    config = configuration(policy)
    if config is None:
        return
    jobs = workflow["jobs"]
    workflow["concurrency"]["queue"] = "max"
    workflow["on"]["workflow_dispatch"]["inputs"].update({
        "operation": {"type": "choice", "description": "Release or rebuild closed inputs against an existing release",
                      "options": ["release", "closed-rebuild"], "default": "release", "required": True},
        "closed_request": {"type": "string", "description": "Optional opaque request ID; empty uses the approved default",
                           "required": False},
        "existing_release": {"type": "string", "description": "For closed-rebuild only: exact published version tag",
                             "required": False},
    })
    checkout = {"uses": pins["actions/checkout"], "with": {"persist-credentials": False}}
    request = {
        "runs-on": RUNNER, "timeout-minutes": 5,
        "permissions": {"contents": "read"},
        "outputs": {key: condition(f"steps.request.outputs.{key}")
                    for key in ("operation", "closed_required", "closed_request", "release_tag")},
        "steps": [checkout, {"id": "request", "run": "python3 scripts/release_closed.py request"}],
    }
    workflow["jobs"] = jobs = {"closed_request": request, **jobs}
    jobs["scope"]["needs"] = "closed_request"
    jobs["scope"]["if"] = condition("!cancelled() && needs.closed_request.result == 'success' && needs.closed_request.outputs.operation == 'release'")
    jobs["prepare"]["needs"] = ["scope", "closed_request"]
    jobs["prepare"]["if"] = condition("!cancelled() && needs.closed_request.result == 'success' && needs.scope.result == 'success' && needs.scope.outputs.run == 'true' && needs.closed_request.outputs.operation == 'release'")
    jobs["prepare"]["outputs"].update({
        key: condition(f"needs.closed_request.outputs.{key}") for key in ("closed_required", "closed_request")
    })
    trusted = (
        f"github.repository == '{policy['repository']}' && "
        f"github.ref == 'refs/heads/{policy.get('default_branch', 'main')}' && "
        "contains(fromJSON('[\"push\",\"schedule\",\"workflow_dispatch\"]'), github.event_name)"
    )
    fresh = (
        "!cancelled() && needs.prepare.result == 'success' && "
        "needs.gate.result == 'success' && needs.prepare.outputs.build == 'true' && "
        "needs.prepare.outputs.closed_required == 'true' && " + trusted
    )
    stable = (
        "!cancelled() && needs.prepare.result == 'success' && "
        "needs.prepare.outputs.channel == 'stable' && needs.prepare.outputs.build == 'false' && "
        "needs.prepare.outputs.closed_required == 'true' && " + trusted
    )
    rebuild = (
        "!cancelled() && needs.closed_request.result == 'success' && "
        "needs.closed_request.outputs.operation == 'closed-rebuild' && "
        "needs.closed_request.outputs.closed_required == 'true' && " + trusted
    )

    def context_name(purpose: str) -> str:
        return "closed-public-context-" + purpose + "-${{ github.run_id }}-${{ github.run_attempt }}"

    def worker(action: str, needs: list[str], predicate: str, context: str = "") -> dict:
        release_tag = "${{ inputs.rc_tag }}" if action == "validate" else ""
        if action == "rebuild":
            release_tag = condition("needs.closed_request.outputs.release_tag")
        return {
            "needs": needs, "if": condition(predicate),
            "uses": "./.github/workflows/" + config["workflow"],
            "permissions": {"contents": "read", "actions": "read"},
            "with": {
                "action": action,
                "closed_request": condition("needs.closed_request.outputs.closed_request" if action == "rebuild" else "needs.prepare.outputs.closed_request"),
                "release_plan_artifact": condition("needs.prepare.outputs.plan_artifact") if action == "build" else "",
                "release_tag": release_tag,
                "public_context_artifact": context,
            },
        }

    def resolver(purpose: str, needs: list[str], predicate: str) -> dict:
        # Only public payloads are downloaded here; no protected environment or
        # closed credentials are available to this job or its artifact uploader.
        result = {
            "needs": needs, "if": condition(predicate),
            "runs-on": RUNNER, "timeout-minutes": 20,
            "permissions": {"contents": "read", "actions": "read"},
            "steps": [checkout, {
                "name": "Verify the existing public release",
                "env": {"GH_TOKEN": "${{ github.token }}"},
                "run": "python3 scripts/release_closed.py resolve --purpose " + purpose + ' --output "$RUNNER_TEMP/closed-public-context"',
            }, {
                "name": "Stage verified public inputs",
                "uses": pins["actions/upload-artifact"],
                "with": {"name": context_name(purpose), "path": "${{ runner.temp }}/closed-public-context/",
                         "if-no-files-found": "error", "retention-days": 7},
            }],
        }
        if policy.get("sign_releases"):
            from release_signing_workflow import installer
            result["steps"][1:1] = installer(pins)
        return result

    jobs["closed_build"] = worker("build", ["prepare", "gate"], fresh)
    jobs["closed_stable_resolve"] = resolver("stable", ["prepare"], stable)
    jobs["closed_stable_validate"] = worker("validate", ["prepare", "closed_stable_resolve"],
        stable + " && needs.closed_stable_resolve.result == 'success'", context_name("stable"))
    jobs["closed_existing_resolve"] = resolver("rebuild", ["closed_request"], rebuild)
    jobs["closed_rebuild"] = worker("rebuild", ["closed_request", "closed_existing_resolve"],
        rebuild + " && needs.closed_existing_resolve.result == 'success'", context_name("rebuild"))
    for name in ("candidate", "final"):
        job = jobs[name]
        job["needs"] = [*job["needs"], "closed_build"]
        job["if"] = condition(job["if"][3:-3].strip() + " && needs.prepare.result == 'success' && " + closed_result("closed_build"))
    job = jobs["stable"]
    job["needs"] = ["prepare", "closed_stable_validate"]
    job["if"] = condition("!cancelled() && needs.prepare.result == 'success' && " + job["if"][3:-3].strip() + " && " + closed_result("closed_stable_validate"))
    job["steps"].extend([{
        "name": "Bind the unchanged RC to this public promotion",
        "env": {"GH_TOKEN": "${{ github.token }}"},
        "run": 'python3 scripts/release_closed.py record-promotion --output "$RUNNER_TEMP/release-promotion/promotion.json"',
    }, {
        "name": "Store immutable public promotion evidence",
        "uses": pins["actions/upload-artifact"],
        "with": {"name": "release-promotion-evidence", "path": "${{ runner.temp }}/release-promotion/promotion.json",
                 "if-no-files-found": "error", "retention-days": 90},
    }])
