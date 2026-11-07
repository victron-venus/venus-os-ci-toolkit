#!/usr/bin/env python3
"""Skip a backup schedule only after a recent, completed production sweep."""

import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PRODUCER_STEP = "Update reviewed CI and application dependencies"
PRODUCTION_MARKER = "Record successful Renovate production sweep"
# Covers the 24h self-hosted queue limit, 150m job ceiling and daily cycle.
HISTORY_HOURS = 72
MAX_PAGES = 3
MAX_JOB_LOOKUPS = 12


class MetadataError(RuntimeError):
    """Incomplete metadata must not authorize another expensive sweep."""


def timestamp(value):
    if not isinstance(value, str):
        raise MetadataError("Missing Actions timestamp")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MetadataError("Invalid Actions timestamp") from error
    if parsed.tzinfo is None:
        raise MetadataError("Actions timestamp has no timezone")
    return parsed


def cycle_start(now, primary_time):
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", primary_time):
        raise MetadataError("Invalid primary UTC schedule time")
    hour, minute = map(int, primary_time.split(":"))
    start = now.astimezone(dt.timezone.utc).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return start if now >= start else start - dt.timedelta(days=1)


def positive_id(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise MetadataError("Invalid Actions identifier")
    return int(value)


class GitHub:
    def __init__(self, token):
        if not token:
            raise MetadataError("Read-only Actions token is missing")
        self.token = token

    def get(self, path):
        request = urllib.request.Request(
            "https://api.github.com/" + path,
            headers={"Authorization": "Bearer " + self.token,
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                value = json.load(response)
        except urllib.error.HTTPError as error:
            raise MetadataError(f"Actions metadata request failed (HTTP {error.code})") from None
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            raise MetadataError("Actions metadata request failed") from None
        if not isinstance(value, dict):
            raise MetadataError("Unexpected Actions response")
        return value


def records(api, path, key):
    result = []
    expected = None
    for page in range(1, MAX_PAGES + 1):
        separator = "&" if "?" in path else "?"
        value = api.get(f"{path}{separator}per_page=100&page={page}")
        count = value.get("total_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise MetadataError("Missing Actions list count")
        if expected is not None and expected != count:
            raise MetadataError("Actions list changed while paginating")
        expected = count
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise MetadataError("Incomplete Actions list")
        result.extend(items)
        if len(items) < 100:
            if len(result) != expected:
                raise MetadataError("Incomplete Actions pagination")
            return result
    raise MetadataError("Actions history exceeds the bounded metadata budget")


def decide(event, repository, run_id, branch, api, now, primary_time="09:23", legacy_production=None):
    # Dispatch retains its explicit dry-run/production input and never needs history.
    if event == "workflow_dispatch":
        return True, "manual-dispatch"
    if event != "schedule":
        raise MetadataError("Unsupported updater event")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise MetadataError("Invalid repository")
    run_id = positive_id(run_id)
    prefix = f"repos/{repository}/actions"
    current = api.get(f"{prefix}/runs/{run_id}")
    workflow_id = positive_id(current.get("workflow_id"))
    if current.get("event") != "schedule" or current.get("head_branch") != branch:
        raise MetadataError("Current run context does not match the schedule")
    if current.get("path") != ".github/workflows/renovate.yml":
        raise MetadataError("Unexpected updater workflow")
    current_created = timestamp(current.get("created_at"))
    cutoff = cycle_start(now, primary_time)
    since = (now - dt.timedelta(hours=HISTORY_HOURS)).isoformat()
    query = urllib.parse.urlencode({"branch": branch, "created": ">=" + since})
    runs = records(api, f"{prefix}/workflows/{workflow_id}/runs?{query}", "workflow_runs")
    lookups = 0
    for candidate in runs:
        candidate_id = positive_id(candidate.get("id"))
        if candidate_id == run_id or candidate.get("head_branch") != branch:
            continue
        if candidate.get("event") not in {"schedule", "workflow_dispatch"}:
            continue
        if candidate.get("status") == "in_progress":
            if timestamp(candidate.get("created_at")) <= current_created:
                # Existing workflow concurrency normally settles this before our job.
                return False, f"earlier-run-still-active:{candidate_id}"
            continue
        if candidate.get("status") != "completed" or candidate.get("conclusion") != "success":
            continue
        if timestamp(candidate.get("updated_at")) < cutoff:
            continue
        lookups += 1
        if lookups > MAX_JOB_LOOKUPS:
            raise MetadataError("Too many recent successful runs to inspect safely")
        attempt = positive_id(candidate.get("run_attempt"))
        jobs = records(api, f"{prefix}/runs/{candidate_id}/attempts/{attempt}/jobs", "jobs")
        for job in jobs:
            if job.get("name") != "renovate" or job.get("conclusion") != "success":
                continue
            steps = job.get("steps")
            if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
                raise MetadataError("Missing updater steps")
            producer = [s for s in steps if s.get("name") == PRODUCER_STEP]
            marker = [s for s in steps if s.get("name") == PRODUCTION_MARKER]
            if len(producer) > 1 or len(marker) > 1:
                raise MetadataError("Ambiguous updater steps")
            if not producer or producer[0].get("conclusion") != "success":
                continue
            marked = bool(marker and marker[0].get("conclusion") == "success")
            legacy = bool(legacy_production and candidate_id == legacy_production[0]
                          and candidate.get("head_sha") == legacy_production[1]
                          and now < legacy_production[2])
            # Existing scheduled full sweeps predate the marker. Unmarked manual
            # runs may have been dry runs, except an explicitly reviewed exact run.
            if not (marked or candidate.get("event") == "schedule" or legacy):
                continue
            completed = timestamp(producer[0].get("completed_at"))
            if completed > now + dt.timedelta(minutes=5):
                raise MetadataError("Production completion is unexpectedly in the future")
            if completed >= cutoff:
                return False, f"production-in-current-cycle:{candidate_id}"
    return True, "no-production-in-current-cycle"


def main():
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    legacy = None
    legacy_id = os.environ.get("RENOVATE_PREVIOUS_PRODUCTION_RUN", "")
    legacy_head = os.environ.get("RENOVATE_PREVIOUS_PRODUCTION_HEAD", "")
    legacy_expires = os.environ.get("RENOVATE_PREVIOUS_PRODUCTION_EXPIRES", "")
    if legacy_id or legacy_head or legacy_expires:
        if not re.fullmatch(r"[0-9a-f]{40}", legacy_head):
            raise MetadataError("Invalid reviewed production head")
        legacy = (positive_id(legacy_id), legacy_head, timestamp(legacy_expires))
    api = None if event == "workflow_dispatch" else GitHub(os.environ.get("GH_TOKEN", ""))
    run, reason = decide(event, os.environ.get("GITHUB_REPOSITORY", ""),
                         os.environ.get("GITHUB_RUN_ID", ""),
                         os.environ.get("GITHUB_REF_NAME", ""), api,
                         dt.datetime.now(dt.timezone.utc),
                         os.environ.get("RENOVATE_PRIMARY_TIME", ""), legacy)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"run={str(run).lower()}\nreason={reason}\n")
    print(f"Renovate schedule decision: {reason}")


if __name__ == "__main__":
    try:
        main()
    except (MetadataError, KeyError) as error:
        print(f"Schedule admission failed: {error}", file=sys.stderr)
        sys.exit(1)
