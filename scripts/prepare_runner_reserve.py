#!/usr/bin/env python3
"""Adopt opt-in shared runner routing without enabling it or upgrading releases."""
from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path

from runner_selection import ConsumerRunnerAdapter, adapt_jobs, can_adapt_job, yaml_editor
from workflow_contracts import validate_fuzz_pins

TOOLKIT = "victron-venus/venus-os-ci-toolkit/.github/workflows/"
AUTOMATION = {"auto-approve.yml", "auto-merge.yml", "coderabbit-review.yml", "coderabbit-autofix.yml"}
JOB_PROFILE = re.compile(r"([A-Za-z0-9_-]+\.ya?ml):([A-Za-z_][A-Za-z0-9_-]*)=(ci|automation|release|hosted|preserve)")
CREDENTIAL = re.compile(r"secrets\s*(?:\.|\[)|github\s*(?:\.\s*token\b|\[\s*\\?['\"]token\\?['\"])|"
                        r'"[^"\s]*(?:token|password|private[_-]?key|api[_-]?key|credentials)[^"\s]*"\s*:', re.I)
PUBLISH = re.compile(r"deploy-pages|gh-action-pypi-publish|docker/(?:login|build-push)-action|"
                     r"action-gh-release|upload-release-action|actions-gh-pages|wrangler-action|"
                     r"\b(?:git\s+push|docker\s+push|twine\s+upload|npm\s+publish|gh\s+release)\b", re.I)


def select_profile(name, workflow, profiles, job_profiles):
    """Separate release and metadata routing from source validation."""
    if name in AUTOMATION:
        # PR-target metadata wrappers must not execute checked-out PR code.
        if any(job.get("steps") and job_profiles.get(job_name) not in {"automation", "hosted", "preserve"}
               for job_name, job in workflow["jobs"].items()):
            raise ValueError("Review local automation steps: " + name)
        return "automation"
    if name.startswith(("release-", "nightly", "vendor-")):
        return "release"
    return profiles.get(name, "ci")


def parse_job_profiles(values):
    """Accept explicit, reviewable job roles; reject misspellings and duplicates."""
    result = {}
    for value in values:
        match = JOB_PROFILE.fullmatch(value)
        if not match:
            raise ValueError("Use --job-profile WORKFLOW:JOB=ci|automation|release|hosted|preserve")
        filename, job, profile = match.groups()
        jobs = result.setdefault(filename, {})
        if job in jobs:
            raise ValueError("Repeated job profile: " + filename + ":" + job)
        jobs[job] = profile
    return result


def permissions_need_review(workflow, job):
    """A job permission map replaces, rather than extends, workflow permissions."""
    permissions = job.get("permissions", workflow.get("permissions"))
    if permissions == "read-all":
        return False
    return (not isinstance(permissions, dict)
            or any(value not in {"read", "none"} for value in permissions.values()))


def string_values(value):
    """Keep actual newlines in commands when inspecting YAML scalar values."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from string_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from string_values(child)


def job_review_reasons(workflow, job):
    """Flag concrete privileged indicators, not certify arbitrary commands as safe."""
    reasons = []
    if permissions_need_review(workflow, job):
        reasons.append("write or unknown permissions")
    fields = {"job": job, "workflow_env": workflow.get("env", {})}
    payload = json.dumps(fields)
    if CREDENTIAL.search(payload):
        reasons.append("credential reference")
    if any(PUBLISH.search(value) for value in string_values(fields)) or job.get("environment"):
        reasons.append("publication or deployment")
    return reasons


def validate_requested_jobs(jobs, requested, name):
    """Only a named local job can acknowledge a role; custom routing stays intact."""
    for job_name, profile in requested.items():
        job = jobs.get(job_name)
        if not isinstance(job, dict) or "runs-on" not in job or "uses" in job:
            raise ValueError("Job profile must name an existing local job: " + name + ":" + job_name)
        if profile == "hosted" and not (can_adapt_job(job) or re.fullmatch(
                r"ubuntu-(?:latest|\d{2}\.\d{2}(?:-arm)?)", str(job["runs-on"]))):
            raise ValueError("Use preserve for reviewed custom runner selection: " + name + ":" + job_name)


def review_job_profiles(workflow, requested, name):
    """Refuse ambiguous privilege migration before constructing any edit set."""
    validate_requested_jobs(workflow["jobs"], requested, name)
    events = workflow.get("on", {})
    workflow_run = events == "workflow_run" or isinstance(events, (dict, list)) and "workflow_run" in events
    for job_name, job in workflow["jobs"].items():
        if not can_adapt_job(job):
            continue
        reasons = job_review_reasons(workflow, job)
        if reasons and job_name not in requested:
            raise ValueError("Review job profile " + name + ":" + job_name + " (" + ", ".join(reasons) + ")")
        if reasons and workflow_run and requested.get(job_name) not in {"hosted", "preserve"}:
            raise ValueError("Keep workflow_run publisher hosted or preserve reviewed routing: " + name + ":" + job_name)


def update_fuzz_runtime(job, old_ref, fuzz_ref, toolkit_ref):
    """Keep the reviewed wrapper, helper checkout and policy on one revision."""
    parameters = job.get("with")
    if (old_ref != fuzz_ref or not isinstance(parameters, dict)
            or parameters.get("toolkit-ref") != fuzz_ref):
        raise ValueError("Existing fuzz workflow, runtime and policy pins must match")
    if parameters["toolkit-ref"] == toolkit_ref:
        return False
    parameters["toolkit-ref"] = toolkit_ref
    return True


def update_pins(workflow, toolkit_ref, fuzz_ref):
    """Pin only reusable calls belonging to this toolkit."""
    changed = False
    for job in workflow["jobs"].values():
        reference = job.get("uses", "")
        if not reference.startswith(TOOLKIT):
            continue
        name, separator, old_ref = reference[len(TOOLKIT):].partition("@")
        if not separator or not re.fullmatch(r"[A-Za-z0-9_-]+\.ya?ml", name):
            raise ValueError("Invalid reusable workflow reference")
        if name == "fuzz.yml":
            changed = update_fuzz_runtime(job, old_ref, fuzz_ref, toolkit_ref) or changed
        if old_ref != toolkit_ref:
            job["uses"] = TOOLKIT + name + "@" + toolkit_ref
            changed = True
    return changed


def render_workflow(path, profiles, toolkit_ref, fuzz_ref, job_profiles):
    """Preserve syntax/comments and refuse file or parent-directory symlinks."""
    if path.resolve() != path:
        raise ValueError("Refusing workflow symlink: " + path.name)
    editor = yaml_editor()
    workflow = editor.load(path.read_text())
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        raise ValueError("Invalid workflow: " + path.name)
    profile = select_profile(path.name, workflow, profiles, job_profiles)
    review_job_profiles(workflow, job_profiles, path.name)
    changed = adapt_jobs(workflow, profile, path.name, job_profiles=job_profiles)
    pinned = update_pins(workflow, toolkit_ref, fuzz_ref)
    if not (changed or pinned):
        return None
    output = io.StringIO()
    editor.dump(workflow, output)
    return output.getvalue()


def existing_fuzz_reference(directory, policy):
    """Validate the existing consumer contract before preparing any updates."""
    if policy.get("fuzzing") is None:
        return None
    path = directory / ".github/workflows/release-pipeline.yml"
    if path.resolve() != path or not path.is_file():
        raise ValueError("A regular release pipeline is required for fuzzing")
    workflow = yaml_editor().load(path.read_text())
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        raise ValueError("Invalid release pipeline")
    validate_fuzz_pins(directory, {"release-pipeline.yml": workflow})
    return policy["fuzzing"]["toolkit_ref"]


def update_policy_pins(policy, toolkit_ref):
    """Refresh owned helper pins without changing the release engine revision."""
    changed = False
    for section in ("coverage", "fuzzing"):
        if policy.get(section) and policy[section]["toolkit_ref"] != toolkit_ref:
            policy[section]["toolkit_ref"] = toolkit_ref
            changed = True
    return changed


def render(directory, toolkit_ref, *, job_profiles=()):
    """Return a complete, validated edit set before writing any consumer files."""
    directory = Path(directory).resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", toolkit_ref):
        raise ValueError("Use a reviewed immutable toolkit commit SHA")
    overrides = parse_job_profiles(job_profiles)
    policy_path = directory / ".release-policy.json"
    if policy_path.is_symlink():
        raise ValueError("Refusing policy symlink")
    policy = json.loads(policy_path.read_text()) if policy_path.exists() else {}
    fuzz_ref = existing_fuzz_reference(directory, policy)
    profiles = {}
    if policy.get("single_entry_ci"):
        adapter = ConsumerRunnerAdapter(directory, policy)
        for name, profile in adapter.roots.items():
            adapter.visit(name, profile)
        profiles.update(adapter.visited)
    files = {}
    seen_workflows = set()
    for path in sorted((directory / ".github/workflows").glob("*")):
        if path.suffix not in {".yml", ".yaml"}:
            continue
        seen_workflows.add(path.name)
        text = render_workflow(path, profiles, toolkit_ref, fuzz_ref, overrides.get(path.name, {}))
        if text is not None:
            files[str(path.relative_to(directory))] = text
    if set(overrides) - seen_workflows:
        raise ValueError("Job profile names an unknown workflow")
    if update_policy_pins(policy, toolkit_ref):
        files[".release-policy.json"] = json.dumps(policy, indent=2) + "\n"
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--toolkit-ref", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--job-profile", action="append", default=[], metavar="WORKFLOW:JOB=PROFILE",
                        help="Reviewed local job role: ci, automation, release, hosted, or preserve; repeat per job")
    args = parser.parse_args()
    files = render(args.repository, args.toolkit_ref, job_profiles=args.job_profile)
    if args.check and files:
        parser.exit(1, "Runner adoption drift: " + ", ".join(files) + "\n")
    if not args.check:
        for path, source in files.items():
            (args.repository / path).write_text(source)
    print(f"Runner adoption: {len(files)} files; no Actions settings changed")


if __name__ == "__main__":
    main()
