"""Keep Actions release artifact names inside one workflow run attempt."""

from __future__ import annotations

import fnmatch
import re

SUFFIX = "-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}"
RUN_SELECTOR = "-run-${{ github.run_id }}-attempt-*"
PLAN_NAMES = {
    "inputs.release_plan_artifact", "inputs.release-plan-artifact",
    "needs.prepare.outputs.plan_artifact", "steps.metadata.outputs.plan_artifact",
}


def unscoped(value: str) -> str:
    """Strip the attempt identity a generated name already carries."""
    for suffix in (RUN_SELECTOR, SUFFIX):
        value = value.removesuffix(suffix)
    return value


def all_attempts_name(value: str) -> str:
    """Select one artifact across every attempt of this workflow run."""
    if value.endswith(RUN_SELECTOR):
        return value
    return unscoped(value) + RUN_SELECTOR


def scoped_name(value: str) -> str:
    """Add the exact attempt suffix once, preserving matrix target selectors."""
    return value if value.endswith((SUFFIX, RUN_SELECTOR)) else value + SUFFIX


def producer_selectors(workflow: dict) -> set[str]:
    """Identify local build outputs, including native assembly intermediates."""
    selectors = set()
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if step.get("uses", "").split("@", 1)[0] != "actions/upload-artifact":
                continue
            name = step.get("with", {}).get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Release build uploads require explicit artifact names")
            selector = re.sub(r"\$\{\{.*?\}\}", "*", unscoped(name))
            if not selector.strip("*"):
                raise ValueError("Release build artifact names require a literal component")
            selectors.add(selector)
    return selectors


def overlaps(value: str, selector: str) -> bool:
    """Conservatively include intersecting globs rather than miss an input edge."""
    value = re.sub(r"\$\{\{.*?\}\}", "*", unscoped(value))
    if fnmatch.fnmatchcase(value, selector) or fnmatch.fnmatchcase(selector, value):
        return True
    # Compatible fixed ends are an over-approximation for glob intersections.
    # Scoping an extra local selector can fail a build, but never admits an old input.
    prefixes = [re.split(r"[\[*?]", item, maxsplit=1)[0] for item in (value, selector)]
    suffixes = [re.split(r"[\]*?]", item)[-1] for item in (value, selector)]
    return (
        (prefixes[0].startswith(prefixes[1]) or prefixes[1].startswith(prefixes[0]))
        and (suffixes[0].endswith(suffixes[1]) or suffixes[1].endswith(suffixes[0]))
    )


def separate_download_identity(options: dict) -> bool:
    """Require exact candidate IDs or unambiguous current-run name selection."""
    if "artifact-ids" in options:
        identity = options["artifact-ids"]
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("Release artifact IDs require explicit lineage")
        if any(key in options for key in ("name", "pattern")):
            raise ValueError("Release artifact IDs cannot be mixed with name selectors")
        # Exact IDs retain the caller's independent candidate-lineage verification.
        return True
    for key, expression in (("run-id", "github.run_id"), ("repository", "github.repository")):
        if key in options:
            value = options[key]
            if not isinstance(value, str) or not re.fullmatch(
                r"\$\{\{\s*" + re.escape(expression) + r"\s*\}\}", value
            ):
                # A literal ID cannot be proven current across generated executions.
                raise ValueError("External or ambiguous release downloads require artifact IDs and explicit lineage")
    selectors = [key for key in ("name", "pattern") if key in options]
    if len(selectors) != 1:
        raise ValueError("Release downloads require exactly one explicit name or pattern selector")
    value = options[selectors[0]]
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Release download selectors must be nonempty strings")
    return False


def scope_selector(value: object, selectors: set[str]) -> object:
    """Keep named release plans and reject unknown dynamic name resolution."""
    if isinstance(value, str) and re.fullmatch(r"\$\{\{.*?\}\}", value):
        if value[3:-2].strip() in PLAN_NAMES:
            return value
        raise ValueError("Dynamic release artifact selector needs explicit lineage")
    if isinstance(value, str) and any(overlaps(value, selector) for selector in selectors):
        return scoped_name(value)
    return value


def collect_every_attempts(workflow: dict, pattern: str) -> bool:
    """Let a publisher collect each artifact from its newest attempt of this run.

    A failed-only rerun executes only the failed jobs, so the uploads of the
    jobs that succeeded stay in an earlier attempt of the same run. The
    collector still keeps one attempt per artifact and the publisher still
    verifies repository, run, source and plan.
    """
    changed = False
    for job in workflow.get("jobs", {}).values():
        steps = job.get("steps", [])
        if not any(re.search(r"scripts/release\.py collect\b", str(step.get("run", "")))
                   for step in steps):
            continue
        for step in steps:
            if step.get("uses", "").split("@", 1)[0] != "actions/download-artifact":
                continue
            options = step.get("with", {})
            for key in ("name", "pattern"):
                value = options.get(key)
                if not isinstance(value, str) or not value.startswith(pattern.removesuffix("*")):
                    continue
                scoped = all_attempts_name(value)
                if scoped != value:
                    changed = True
                    options[key] = scoped
    return changed


def scope(workflow: dict, pattern: str, producers: set[str] | None = None) -> bool:
    """Scope matching uploads/downloads without changing plans or diagnostics."""
    changed = False
    selectors = {pattern, *(producers or set())}
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            action = step.get("uses", "").split("@", 1)[0]
            if action not in {"actions/upload-artifact", "actions/download-artifact"}:
                continue
            options = step.get("with", {})
            if action == "actions/download-artifact" and separate_download_identity(options):
                continue
            for key in ("name", "pattern"):
                value = options.get(key)
                scoped = scope_selector(value, selectors)
                if scoped != value:
                    changed = True
                    options[key] = scoped
    return changed
