"""Keep Actions release artifact names inside one workflow run attempt."""

from __future__ import annotations

import fnmatch
import re

SUFFIX = "-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}"
PLAN_NAMES = {
    "inputs.release_plan_artifact", "inputs.release-plan-artifact",
    "needs.prepare.outputs.plan_artifact", "steps.metadata.outputs.plan_artifact",
}


def scoped_name(value: str) -> str:
    """Add the exact attempt suffix once, preserving matrix target selectors."""
    return value if value.endswith(SUFFIX) else value + SUFFIX


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
            selector = re.sub(r"\$\{\{.*?\}\}", "*", name.removesuffix(SUFFIX))
            if not selector.strip("*"):
                raise ValueError("Release build artifact names require a literal component")
            selectors.add(selector)
    return selectors


def overlaps(value: str, selector: str) -> bool:
    """Conservatively include intersecting globs rather than miss an input edge."""
    value = re.sub(r"\$\{\{.*?\}\}", "*", value.removesuffix(SUFFIX))
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
