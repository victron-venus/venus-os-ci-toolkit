"""Keep Actions release artifact names inside one workflow run attempt."""

from __future__ import annotations

import fnmatch
import re

SUFFIX = "-run-${{ github.run_id }}-attempt-${{ github.run_attempt }}"


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
            if action == "actions/download-artifact" and any(
                key in options for key in ("artifact-ids", "run-id", "repository")
            ):
                # Explicit external/candidate identity has an independent contract.
                continue
            for key in ("name", "pattern"):
                value = options.get(key)
                if isinstance(value, str) and any(
                    fnmatch.fnmatchcase(value.removesuffix(SUFFIX), selector)
                    for selector in selectors
                ):
                    scoped = scoped_name(value)
                    changed = changed or scoped != value
                    options[key] = scoped
    return changed
