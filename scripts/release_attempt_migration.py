"""Admit only understood, release-exclusive artifact graphs for a narrow migration."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
# The only subprocess reads a pinned local Git object, using fixed argv and no shell.
import subprocess  # nosec B404

import yaml

from release_attempt_workflow import SUFFIX, producer_selectors, scope_selector, separate_download_identity

TOOLKIT = "victron-venus/venus-os-ci-toolkit"
PIPELINE = ".github/workflows/release-pipeline.yml"
BUILD = "./.github/workflows/release-build.yml"


def load_workflow(source: str, loader: type) -> dict:
    """The caller may preserve workflow scalars, but never construct Python objects."""
    if not isinstance(loader, type) or not issubclass(loader, yaml.SafeLoader):
        raise ValueError("Release-attempt migration requires a safe workflow loader")
    return yaml.load(source, Loader=loader)  # nosec B506


def unshared_edits(workflow: dict, selectors: set[str]) -> None:
    """Preserve immutable aliases, but never mutate a shared artifact edge."""
    counts: dict[int, int] = {}

    def count(value):
        if not isinstance(value, (dict, list)):
            return
        counts[id(value)] = counts.get(id(value), 0) + 1
        if counts[id(value)] > 1:
            return
        for child in value.values() if isinstance(value, dict) else value:
            count(child)

    count(workflow)
    jobs = workflow["jobs"]
    for job in jobs.values():
        steps = job.get("steps", [])
        for step in steps:
            action = step.get("uses", "").split("@", 1)[0]
            if action not in {"actions/upload-artifact", "actions/download-artifact"}:
                continue
            options = step.get("with", {})
            if action == "actions/download-artifact" and separate_download_identity(options):
                continue
            for key in ("name", "pattern"):
                value = options.get(key)
                if scope_selector(value, selectors) == value:
                    continue
                chain = (workflow, jobs, job, steps, step, options, value)
                if any(counts.get(id(node), 0) > 1
                       or getattr(getattr(node, "anchor", None), "value", None)
                       or getattr(node, "merge", None) for node in chain):
                    raise ValueError("Release artifact edits do not support YAML anchors, aliases or merges")


def expression(value: object, expected: str) -> bool:
    """Accept whitespace variations of one known GitHub expression only."""
    return isinstance(value, str) and bool(re.fullmatch(
        r"\$\{\{\s*" + re.escape(expected) + r"\s*\}\}", value))


def policy_supported(policy: dict) -> None:
    """Do not convert legacy releases or local/private CI as a side effect."""
    if not isinstance(policy, dict):
        raise ValueError("Release-attempt migration requires a release policy object")
    versioning = policy.get("versioning")
    if (policy.get("mode", "release") != "release"
            or policy.get("visibility", "public") != "public"
            or policy.get("ci_execution", "github") != "github"
            or not isinstance(versioning, dict) or type(versioning.get("schema")) is not int
            or versioning.get("schema") != 1):
        raise ValueError("Release-attempt migration requires an existing public hosted versioned release")
    pattern = policy.get("artifact_pattern", "release-assets*")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ValueError("Release-attempt migration requires an explicit nonempty artifact pattern")


def workflow_supported(workflow: dict) -> None:
    """Reject malformed or shared mappings before the existing helper mutates them."""
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        raise ValueError("Release-attempt migration requires an explicit workflow job mapping")
    for job in workflow["jobs"].values():
        if not isinstance(job, dict) or not isinstance(job.get("uses", ""), str):
            raise ValueError("Unsupported release job mapping")
        if not isinstance(job.get("with", {}), dict) or not isinstance(job.get("steps", []), list):
            raise ValueError("Unsupported release job inputs or steps")
        for step in job.get("steps", []):
            if (not isinstance(step, dict) or not isinstance(step.get("uses", ""), str)
                    or not isinstance(step.get("with", {}), dict)
                    or not isinstance(step.get("run", ""), str)):
                raise ValueError("Unsupported release step mapping")


def pinned_fuzz_workflow(toolkit: Path, reference: str, loader: type) -> dict:
    """Read the exact local Git object, without replacement objects or lazy fetch."""
    if not re.fullmatch(r"[0-9a-f]{40}", reference):
        raise ValueError("Shared producer revision must be a full commit SHA")
    executable = shutil.which("git")
    if executable is None:
        raise ValueError("Git is required to inspect the pinned shared producer")
    try:
        # Fixed argv and a validated commit SHA; no commands come from the workflow.
        result = subprocess.run(  # nosec B603
            [str(Path(executable).resolve()), "--no-replace-objects", "-C", str(toolkit), "show",
             reference + ":.github/workflows/fuzz.yml"],
            env={"LC_ALL": "C", "LANG": "C", "GIT_NO_LAZY_FETCH": "1",
                 "GIT_NO_REPLACE_OBJECTS": "1", "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Cannot inspect the pinned shared producer locally") from error
    if result.returncode or len(result.stdout) > 262144:
        raise ValueError("Use a toolkit Git checkout containing producer revision " + reference)
    workflow = load_workflow(result.stdout, loader)
    workflow_supported(workflow)
    return workflow


def shared_producer(workflow: dict, policy: dict, toolkit: Path, loader: type) -> None:
    """Require compatible existing producer pins; never silently upgrade them."""
    jobs = workflow["jobs"]
    for name, job in jobs.items():
        if job.get("uses") and not job["uses"].startswith("./") and name != "fuzz":
            raise ValueError("Review the unsupported remote release job before migration: " + name)
    fuzz = jobs.get("fuzz")
    configured = policy.get("fuzzing")
    if fuzz is None and configured is None:
        return
    reference = re.fullmatch(re.escape(TOOLKIT) + r"/\.github/workflows/fuzz\.yml@([0-9a-f]{40})",
                             fuzz.get("uses", "") if isinstance(fuzz, dict) else "")
    options = fuzz.get("with", {}) if isinstance(fuzz, dict) else {}
    if (not reference or not isinstance(configured, dict)
            or configured.get("toolkit_ref") != reference[1]
            or options.get("toolkit-ref") != reference[1]
            or options.get("evidence-mode", "release") != "release"
            or not expression(options.get("release-plan-artifact"), "needs.prepare.outputs.plan_artifact")):
        raise ValueError("Release-attempt migration requires matching source-pinned release-mode fuzz wiring")
    producer = pinned_fuzz_workflow(toolkit, reference[1], loader)
    names = [step.get("with", {}).get("name", "")
             for job in producer.get("jobs", {}).values() for step in job.get("steps", [])
             if step.get("uses", "").split("@", 1)[0] == "actions/upload-artifact"]
    release_names = [name for name in names if isinstance(name, str) and name.startswith("release-assets")]
    if release_names != ["release-assets-fuzz" + SUFFIX]:
        raise ValueError("Pinned shared producer is not attempt-scoped; review a coordinated producer upgrade first")


def pipeline_supported(workflow: dict, policy: dict, toolkit: Path, loader: type) -> None:
    """A versioned policy alone does not prove that its saved workflow was migrated."""
    workflow_supported(workflow)
    jobs = workflow.get("jobs", {})
    build = jobs.get("build", {})
    prepare = jobs.get("prepare", {})
    if (build.get("uses") != BUILD
            or not expression(build.get("with", {}).get("release_plan_artifact"),
                              "needs.prepare.outputs.plan_artifact")
            or not expression(prepare.get("outputs", {}).get("plan_artifact"),
                              "steps.metadata.outputs.plan_artifact")):
        raise ValueError("Existing release workflow lacks the supported version-plan wiring")
    for name in ("candidate", "final"):
        steps = jobs.get(name, {}).get("steps", [])
        plan = any(step.get("uses", "").split("@", 1)[0] == "actions/download-artifact"
                   and expression(step.get("with", {}).get("name"), "needs.prepare.outputs.plan_artifact")
                   for step in steps)
        publish = any(re.search(r"\bpython3\s+scripts/release_versioned\.py\s+publish\b",
                                step.get("run", "")) for step in steps)
        if not plan or not publish:
            raise ValueError("Existing release workflow lacks the supported versioned publisher: " + name)
    pattern = policy.get("artifact_pattern", "release-assets*")
    unshared_edits(workflow, {pattern})
    for name in ("candidate", "final", *(["sign"] if "sign" in jobs else [])):
        collectors = [step.get("with", {}) for step in jobs[name].get("steps", [])
                      if step.get("uses", "").split("@", 1)[0] == "actions/download-artifact"
                      and step.get("with", {}).get("path") == ".release-download"]
        if (len(collectors) != 1 or collectors[0].get("pattern") not in {pattern, pattern + SUFFIX}
                or collectors[0].get("merge-multiple") is not False):
            raise ValueError("Saved package collector does not match the release artifact policy: " + name)
    for job in jobs.values():
        for step in job.get("steps", []):
            merge_multiple = step.get("with", {}).get("merge-multiple")
            if (step.get("uses", "").split("@", 1)[0] == "actions/download-artifact"
                    and (merge_multiple is True
                         or isinstance(merge_multiple, str) and merge_multiple.lower() == "true")):
                raise ValueError("Release collection must keep separate artifact directories")
    shared_producer(workflow, policy, toolkit, loader)


def regular_workflow(directory: Path, name: str) -> Path:
    """Keep local adapter discovery inside the selected consumer checkout."""
    path = directory / name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Release adapter must be a regular local workflow: " + name)
    return path


def exclusive_adapters(directory: Path, workflows: dict, loader: type, pattern: str) -> None:
    """Do not rename outputs consumed by unrelated CI or opaque build producers."""
    selectors = {pattern}
    for workflow in workflows.values():
        selectors.update(producer_selectors(workflow))
    for name, workflow in workflows.items():
        workflow_supported(workflow)
        unshared_edits(workflow, selectors)
        if not isinstance(workflow.get("on"), dict) or set(workflow["on"]) != {"workflow_call"}:
            raise ValueError("Release adapter must expose only workflow_call: " + name)
        for job in workflow.get("jobs", {}).values():
            uses = job.get("uses", "")
            if uses and not uses.startswith("./.github/workflows/"):
                raise ValueError("Review the remote build adapter before migration: " + name)
            if any(step.get("uses", "").startswith("./") for step in job.get("steps", [])):
                raise ValueError("Review the local composite build action before migration: " + name)
    for path in sorted((directory / ".github/workflows").iterdir()):
        relative = path.relative_to(directory).as_posix()
        if path.suffix not in {".yml", ".yaml"} or relative in {PIPELINE, *workflows}:
            continue
        regular_workflow(directory, relative)
        other = load_workflow(path.read_text(), loader)
        if not isinstance(other, dict):
            continue
        for job in other.get("jobs", {}).values():
            if job.get("uses", "").removeprefix("./") in workflows:
                raise ValueError("Release adapter is also consumed outside the release graph: " + relative)


def operator_guide() -> str:
    """Explain the bounded migration and coherent retry contract."""
    return """## Migrating artifact selection without upgrading a consumer

For an existing public, hosted, versioned release pipeline, run the following
from a toolkit Git checkout containing the consumer's pinned shared fuzz commit:

```sh
python3 scripts/install_release.py /path/to/consumer --release-attempts-only --check
python3 scripts/install_release.py /path/to/consumer --release-attempts-only
```

The check reports drift without writing. The migration changes artifact names
and selectors in the existing generated release pipeline and its exclusive local
build adapters. It retains workflow pins, commands, gates, permissions, version
plans, exact-ID downloads and runtime files. Review and merge the producer and
collector changes together. This mode does not update dependencies, run builds,
dispatch releases, fetch Git objects, or delete retained Actions artifacts.

Unsupported or ambiguous layouts stop before any files are written. These include
legacy pipelines, aliases on changed artifact edges, adapters used by ordinary CI, opaque remote
build producers and shared fuzz revisions that still upload unscoped artifacts.
Such revisions need a separately reviewed producer upgrade; changing only the
collector would leave it without a matching input. The pinned producer is checked
from its exact local Git object, with replacement objects and lazy fetching disabled.

Every ordinary release artifact name now ends in
`-run-<run_id>-attempt-<run_attempt>`. A full same-run retry can retain previous
attempts without mixing their payloads. After a partial release failure, use
**Re-run all jobs**, so every required producer creates inputs for the same new
attempt. Re-running only a failed job can leave no current inputs, or an incomplete
set; older packages must not fill those gaps. Duplicate payload names within the
current attempt remain errors, even when their bytes match.

Name selection is not receipt verification. Existing publication checks still
verify source, repository, run and attempt. Platform completeness depends on the
project's explicit artifact declarations; a valid receipt for one platform does
not establish that all required platforms were built. The regression suite uses
synthetic packages with the actual selector, collector and receipt verifiers;
it does not claim a live release retry or device acceptance.
"""
