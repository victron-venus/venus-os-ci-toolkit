#!/usr/bin/env python3
"""Classify a complete automatic main push before proving prior publication.

Only the explicitly reviewed test infrastructure below can avoid an automatic
release, after a separate exact-source publication proof. All other inputs retain
the existing full release classifier. Authenticated preparation calls this
read-only module and owns the publication proof and fixed receipt destination.
This module reads immutable Git objects; it never fetches or changes the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

if __package__:
    from . import change_scope
else:
    import change_scope


def validate_paths(paths):
    """Require a small explicit list of regular test-source paths, never globs."""
    if (
        not isinstance(paths, list)
        or not 1 <= len(paths) <= 32
        or any(
            not change_scope.literal_path(path)
            or not path.startswith("tests/")
            or any(part.startswith(".") for part in Path(path).parts)
            or Path(path).suffix not in {".py", ".js", ".cjs", ".mjs", ".ts"}
            for path in paths
        )
        or len(set(paths)) != len(paths)
    ):
        raise ValueError("Expected explicit test-source paths")
    return frozenset(paths)


def receipt_step(upload):
    """Retain the positive proof after preparation, without changing its ordering."""
    return {
        "name": "Retain test-only push admission",
        "if": "${{ steps.metadata.outputs.status == 'test-only-already-published' }}",
        "uses": upload,
        "with": {
            "name": "release-push-admission-${{ github.run_id }}-${{ github.run_attempt }}",
            "path": ".release-push-admission.json",
            "include-hidden-files": True,
            "if-no-files-found": "error",
            "retention-days": 14,
        },
    }


def validate_workflow(policy, workflow):
    """Admission never skips authenticated preparation or changes the forced scope."""
    validate_paths(policy["change_scope"]["automatic_release_test_paths"])
    jobs = workflow.get("jobs", {})
    scope = jobs.get("scope", {})
    steps = scope.get("steps", [])
    classifiers = [step for step in steps if step.get("id") == "scope"]
    prepare = jobs.get("prepare", {})
    receipts = [
        step
        for step in prepare.get("steps", [])
        if step.get("name") == "Retain test-only push admission"
    ]
    metadata = [
        step for step in prepare.get("steps", []) if step.get("id") == "metadata"
    ]
    if len(classifiers) != 1 or len(receipts) != 1 or len(metadata) != 1:
        raise ValueError("Missing or duplicate automatic push admission")
    expected = receipt_step(receipts[0].get("uses", ""))
    # Workflow contracts use BaseLoader; normalize its scalar representation.
    expected["with"]["include-hidden-files"] = "true"
    expected["with"]["retention-days"] = "14"
    if (
        not receipts[0].get("uses", "").startswith("actions/upload-artifact@")
        or receipts[0] != expected
        or classifiers[0].get("env", {}).get("FORCE_FULL") != "true"
        or classifiers[0].get("run")
        != (
            'args=()\nif [[ "$FORCE_FULL" == true ]]; then args+=(--force); fi\n'
            'python3 scripts/change_scope.py "${args[@]}" '
            '--documentation-paths-json "$DOCUMENTATION_PATHS" '
            '--required-paths-json "$REQUIRED_PATHS"\n'
        )
        or any(key in classifiers[0] for key in ("if", "continue-on-error"))
        or scope.get("outputs")
        != {
            "run": "${{ steps.scope.outputs.run }}",
            "reason": "${{ steps.scope.outputs.reason }}",
        }
        or any(key in scope for key in ("if", "continue-on-error"))
        or prepare.get("needs") != "scope"
        or prepare.get("if") != "${{ needs.scope.outputs.run == 'true' }}"
        or metadata[0].get("run")
        != 'python3 scripts/release_versioned.py prepare --repo "$GITHUB_REPOSITORY"'
        or any(key in metadata[0] for key in ("if", "continue-on-error"))
        or prepare["steps"].index(receipts[0]) <= prepare["steps"].index(metadata[0])
    ):
        raise ValueError("Automatic push admission differs from reviewed policy")


def decision(reason, *, before=None, after=None, raw=None, paths=()):
    """Keep the receipt bounded and exclude arbitrary paths or event content."""
    return {
        "schema": 1,
        "test_only": reason == "test-infrastructure-only",
        "reason": reason,
        "before": before,
        "after": after,
        "diff_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "test_paths": sorted(paths),
    }


def regular_blob_change(header):
    """Accept only raw headers proving an ordinary regular-file addition/change."""
    fields = header.split()
    if len(fields) != 5:
        raise ValueError("Invalid raw header")
    before, after, old_hash, new_hash, status = fields
    if not all(
        change_scope.REVISION.fullmatch(value.decode("ascii"))
        for value in (old_hash, new_hash)
    ):
        raise ValueError("Invalid blob identity")
    zero = b"0" * 40
    if status == b"A":
        return (
            (before, after) == (b":000000", b"100644")
            and old_hash == zero
            and new_hash != zero
        )
    if status == b"M":
        return (
            (before, after) == (b":100644", b"100644")
            and zero not in (old_hash, new_hash)
            and old_hash != new_hash
        )
    return False


def test_paths(raw, allowed_paths):
    """Require a complete nonempty raw diff of ordinary allowlisted A/M files."""
    if not raw or len(raw) > change_scope.MAX_DIFF_BYTES or not raw.endswith(b"\0"):
        raise ValueError("Incomplete diff")
    records = raw[:-1].split(b"\0")
    if len(records) % 2:
        raise ValueError("Invalid record count")
    paths = set()
    for position in range(0, len(records), 2):
        valid = regular_blob_change(records[position])
        path = records[position + 1].decode("utf-8")
        if not valid or path not in allowed_paths or path in paths:
            raise ValueError("Not a proven test-only change")
        paths.add(path)
    return paths


def classify(
    repo, event_name, event, runner_sha, allowed_paths=(), default_branch="main"
):
    """Uncertainty keeps the full path; only an ordinary main push can opt out."""
    if event_name != "push":
        return decision("not-main-push")
    if not isinstance(event, dict):
        return decision("invalid-event")
    try:
        allowed_paths = validate_paths(allowed_paths)
    except ValueError:
        return decision("invalid-test-paths")
    repository = event.get("repository")
    if (
        event.get("ref") != "refs/heads/" + default_branch
        or not isinstance(repository, dict)
        or repository.get("default_branch") != default_branch
    ):
        return decision("not-main-push")
    if any(event.get(flag) is not False for flag in ("created", "deleted", "forced")):
        return decision("non-ordinary-push")
    try:
        before, after, separator = change_scope.event_revisions("push", event)
    except (KeyError, TypeError, ValueError):
        return decision("invalid-revisions")
    if runner_sha != after:
        return decision("runner-revision-mismatch", before=before, after=after)
    try:
        if change_scope.git(repo, "shallow").strip() != b"false":
            return decision("incomplete-history", before=before, after=after)
        for revision in (before, after):
            if change_scope.git(repo, "commit-type", revision).strip() != b"commit":
                return decision("invalid-revisions", before=before, after=after)
        raw = change_scope.git(repo, "diff", f"{before}{separator}{after}")
        paths = test_paths(raw, allowed_paths)
    except (OSError, ValueError, subprocess.SubprocessError):
        return decision("full-release-required", before=before, after=after)
    return decision(
        "test-infrastructure-only", before=before, after=after, raw=raw, paths=paths
    )


def current_push(repo, allowed_paths, default_branch):
    """Read only the bounded runner event and immutable local commit objects."""
    try:
        with Path(os.environ.get("GITHUB_EVENT_PATH", "")).open("rb") as stream:
            raw = stream.read(change_scope.MAX_DIFF_BYTES + 1)
        event = json.loads(raw) if len(raw) <= change_scope.MAX_DIFF_BYTES else None
    except (OSError, ValueError):
        event = None
    return classify(
        repo,
        os.environ.get("GITHUB_EVENT_NAME", ""),
        event,
        os.environ.get("GITHUB_SHA", ""),
        allowed_paths,
        default_branch,
    )
