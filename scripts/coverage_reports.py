#!/usr/bin/env python3
"""Validate coverage transport and context without executing consumer code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.parsers import expat

MAX_REPORT_BYTES = 20 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARTIFACTS = 1000
SHA = re.compile(r"[0-9a-f]{40}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
FLAG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
FORMATS = {"cobertura", "go", "lcov"}


def require(condition, message):
    """Keep checks effective even when Python is invoked with optimization."""
    if not condition:
        raise ValueError(message)


def safe_path(value):
    """Accept a single explicit report path, never globs or traversal."""
    require(isinstance(value, str) and 0 < len(value) <= 240, "Invalid report path")
    parts = value.split("/")
    require(
        not PurePosixPath(value).is_absolute()
        and all(
            p not in {"", ".", ".."} and re.fullmatch(r"[A-Za-z0-9_.-]+", p)
            for p in parts
        ),
        "Report path must be a confined relative file",
    )
    return value


def configuration(environment, event):
    """Derive trust and attribution only from the current GitHub event."""
    name = environment.get("COVERAGE_ARTIFACT", "")
    report = safe_path(environment.get("COVERAGE_FILE", ""))
    format_name = environment.get("COVERAGE_FORMAT", "")
    flag = environment.get("COVERAGE_FLAGS", "")
    attempt = environment.get("GITHUB_RUN_ATTEMPT", "")
    require(NAME.fullmatch(name) is not None, "Invalid artifact base name")
    require(FLAG.fullmatch(flag) is not None, "Invalid coverage flag")
    require(format_name in FORMATS, "Unsupported coverage format")
    require(
        re.fullmatch(r"[1-9]\d*", attempt, flags=re.ASCII) is not None,
        "Invalid run attempt",
    )
    require(
        re.fullmatch(r"[1-9]\d*", environment.get("GITHUB_RUN_ID", ""), flags=re.ASCII)
        is not None,
        "Invalid run ID",
    )
    repository = event.get("repository", {})
    require(
        repository.get("private") is False, "Shared coverage publication is public-only"
    )
    require(
        repository.get("full_name") == environment.get("GITHUB_REPOSITORY")
        and repository.get("full_name"),
        "Repository context mismatch",
    )
    tested_sha = environment.get("GITHUB_SHA", "")
    require(SHA.fullmatch(tested_sha) is not None, "Invalid tested source SHA")
    event_name = environment.get("GITHUB_EVENT_NAME")
    require(
        event_name
        in {"pull_request", "push", "workflow_dispatch", "schedule", "merge_group"},
        "Unsupported coverage event",
    )
    commit, pull_request, mode = tested_sha, "", "oidc"
    if event_name == "pull_request":
        pr = event.get("pull_request", {})
        require(
            pr.get("base", {}).get("repo", {}).get("full_name")
            == repository["full_name"],
            "PR base repository mismatch",
        )
        head = pr.get("head", {})
        head_repo = head.get("repo", {}).get("full_name")
        require(isinstance(head_repo, str) and head_repo, "Missing PR head repository")
        commit = head.get("sha", "")
        require(
            isinstance(commit, str) and SHA.fullmatch(commit) is not None,
            "Invalid PR head SHA",
        )
        number = pr.get("number")
        require(type(number) is int and number > 0, "Invalid PR number")
        pull_request = str(number)
        if head_repo != repository["full_name"]:
            mode = "tokenless"
    elif event_name == "merge_group":
        require(
            event.get("merge_group", {}).get("head_sha") == tested_sha,
            "Merge-group source mismatch",
        )
        require(
            event.get("merge_group", {}).get("base_ref")
            == "refs/heads/" + repository.get("default_branch", ""),
            "Merge-group must target the default branch",
        )
    else:
        branch = repository.get("default_branch")
        require(
            isinstance(branch, str)
            and branch
            and environment.get("GITHUB_REF") == "refs/heads/" + branch,
            "Coverage requires the default branch",
        )
        if event_name == "push":
            require(
                event.get("after") == tested_sha and event.get("deleted") is False,
                "Push source mismatch",
            )
    return {
        "artifact_name": name + "-" + attempt,
        "report_file": report,
        "format": format_name,
        "flags": flag,
        "mode": mode,
        "commit": commit,
        "pull_request": pull_request,
        "tested_sha": tested_sha,
    }


def github_artifact_json(environment, path):
    """Read only the configured GitHub API; never redirect an installation token."""
    base = environment.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    parsed = urlsplit(base)
    require(
        parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment,
        "Invalid GitHub API origin",
    )
    token = environment.get("GITHUB_TOKEN", "")
    require(
        isinstance(token, str)
        and 0 < len(token) <= 4096
        and all(33 <= ord(c) <= 126 for c in token),
        "GitHub read-only artifact token is missing or invalid",
    )

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("GitHub artifact metadata redirects are forbidden")

    request = Request(
        base + "/" + path,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        # Preserve operator-configured HTTPS proxies; TLS and redirect refusal
        # keep the token bound to the configured API origin.
        with build_opener(NoRedirect()).open(request, timeout=30) as response:
            require(response.status == 200, "GitHub artifact inventory request failed")
            raw = response.read(MAX_METADATA_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise ValueError("GitHub artifact inventory request failed") from None
    require(
        len(raw) <= MAX_METADATA_BYTES,
        "GitHub artifact inventory response is too large",
    )
    return json.loads(raw)


def artifact_inventory(environment, fetch=None):
    """List one immutable run, with finite pagination and fail-closed metadata."""
    repository = environment.get("GITHUB_REPOSITORY", "")
    require(
        isinstance(repository, str)
        and re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9_.-]{1,100}",
            repository,
        )
        is not None
        and repository.rsplit("/", 1)[1] not in {".", ".."},
        "Invalid artifact repository",
    )
    run = environment.get("GITHUB_RUN_ID", "")
    require(re.fullmatch(r"[1-9][0-9]*", run) is not None, "Invalid artifact run")
    if fetch is None:

        def fetch(path):
            return github_artifact_json(environment, path)

    records, total = [], None
    for page in range(1, MAX_ARTIFACTS // 100 + 1):
        value = fetch(
            f"repos/{repository}/actions/runs/{run}/artifacts?per_page=100&page={page}"
        )
        require(isinstance(value, dict), "Malformed artifact inventory")
        count, batch = value.get("total_count"), value.get("artifacts")
        require(
            type(count) is int and 0 <= count <= MAX_ARTIFACTS,
            "Artifact inventory exceeds its supported bound",
        )
        require(
            isinstance(batch, list) and len(batch) <= 100, "Malformed artifact page"
        )
        if total is None:
            total = count
        require(count == total, "Artifact inventory changed during pagination")
        records.extend(batch)
        require(len(records) <= total, "Artifact inventory count mismatch")
        if len(records) == total:
            return records
        require(len(batch) == 100, "Artifact inventory is incomplete")
    raise ValueError("Artifact inventory is incomplete")


def select_artifact(environment, event, artifacts, *, now=None):
    """Select newest named attempt first, then validate; never downgrade past bad data."""
    config = configuration(environment, event)
    require(
        config["mode"] == environment.get("COVERAGE_MODE"), "Wrong upload trust job"
    )
    require(
        isinstance(artifacts, list) and len(artifacts) <= MAX_ARTIFACTS,
        "Malformed artifact inventory",
    )
    current = int(environment["GITHUB_RUN_ATTEMPT"])
    pattern = re.compile(
        re.escape(environment["COVERAGE_ARTIFACT"]) + r"-([1-9][0-9]*)"
    )
    prefix = environment["COVERAGE_ARTIFACT"] + "-"
    candidates, ids = [], set()
    for artifact in artifacts:
        require(
            isinstance(artifact, dict) and isinstance(artifact.get("name"), str),
            "Malformed artifact record",
        )
        identifier = artifact.get("id")
        require(
            type(identifier) is int and identifier > 0 and identifier not in ids,
            "Invalid or duplicate coverage artifact ID",
        )
        ids.add(identifier)
        name = artifact["name"]
        if not name.startswith(prefix):
            continue
        match = pattern.fullmatch(name)
        if match is None:
            # A configured producer may have a hyphenated name sharing this
            # prefix. The final canonical attempt suffix belongs to its full
            # base, not to the shorter requested producer.
            sibling = re.fullmatch(
                r"([A-Za-z0-9][A-Za-z0-9_.-]{0,99})-([1-9][0-9]*)", name
            )
            if sibling and sibling[1] != environment["COVERAGE_ARTIFACT"]:
                continue
        require(match is not None, "Malformed producer attempt name")
        attempt = int(match[1])
        if attempt <= current:
            candidates.append((attempt, artifact))
    require(
        candidates, "No coverage artifact exists in this run at an eligible attempt"
    )
    latest = max(attempt for attempt, _ in candidates)
    selected = [artifact for attempt, artifact in candidates if attempt == latest]
    require(len(selected) == 1, "The newest coverage artifact name is ambiguous")
    artifact = selected[0]
    require(
        artifact.get("expired") is False, "The newest coverage artifact has expired"
    )
    expires = artifact.get("expires_at")
    require(isinstance(expires, str), "Artifact expiry is missing")
    try:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        require(expiry.utcoffset() is not None, "Artifact expiry needs a timezone")
        require(
            expiry > (now or datetime.now(timezone.utc)),
            "The newest coverage artifact has expired",
        )
    except (TypeError, ValueError):
        raise ValueError(
            "The newest coverage artifact expiry is invalid or expired"
        ) from None
    digest = artifact.get("digest")
    require(
        isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest),
        "The newest coverage artifact digest is invalid",
    )
    source = artifact.get("workflow_run")
    require(isinstance(source, dict), "Artifact source metadata is missing")
    repository_id = event["repository"].get("id")
    head_repository_id = (
        event["pull_request"]["head"]["repo"].get("id")
        if environment["GITHUB_EVENT_NAME"] == "pull_request"
        else repository_id
    )
    require(
        type(repository_id) is int
        and repository_id > 0
        and type(head_repository_id) is int
        and head_repository_id > 0,
        "Original event repository IDs are missing",
    )
    require(
        type(source.get("id")) is int
        and source["id"] == int(environment["GITHUB_RUN_ID"])
        and type(source.get("repository_id")) is int
        and source["repository_id"] == repository_id
        and type(source.get("head_repository_id")) is int
        and source["head_repository_id"] == head_repository_id
        and source.get("head_sha") == config["commit"],
        "The newest coverage artifact is not bound to this immutable run and head",
    )
    base = ""
    if environment["GITHUB_EVENT_NAME"] == "pull_request":
        base = event["pull_request"]["base"].get("sha", "")
        require(
            isinstance(base, str) and SHA.fullmatch(base),
            "Original PR base SHA is invalid",
        )
    # GitHub reruns retain the original GITHUB_SHA/REF. REST run.pull_requests
    # is mutable current PR metadata and must never be a source-binding authority.
    return {
        "artifact_id": artifact["id"],
        "artifact_name": artifact["name"],
        "artifact_digest_metadata": digest,
        "producer_attempt": latest,
        "uploader_attempt": current,
        "run_id": int(environment["GITHUB_RUN_ID"]),
        "head_sha": config["commit"],
        "tested_sha": config["tested_sha"],
        "event_base_sha": base,
    }


def integer(value, *, positive=False):
    """Reject negative, fractional and non-finite coverage counters."""
    require(
        isinstance(value, str)
        and re.fullmatch(r"\d+", value, flags=re.ASCII) is not None,
        "Invalid coverage counter",
    )
    result = int(value)
    require(result > 0 if positive else result >= 0, "Invalid coverage counter")
    return result


def cobertura_tree(text):
    """Accept generator DTD headers without loading DTDs or expanding entities."""

    def doctype(name, system_id, _public_id, internal_subset):
        require(
            name == "coverage" and bool(system_id) and not internal_subset,
            "Only an external Cobertura DOCTYPE without an internal subset is allowed",
        )

    def reject_entity(*_args):
        raise ValueError("XML entity declarations/references are forbidden")

    tree = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartElementHandler = tree.start
    parser.EndElementHandler = tree.end
    parser.CharacterDataHandler = tree.data
    parser.StartDoctypeDeclHandler = doctype
    parser.EntityDeclHandler = reject_entity
    parser.ExternalEntityRefHandler = reject_entity
    parser.SkippedEntityHandler = reject_entity
    # External DTDs are metadata only: no external parser or I/O is provided.
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    try:
        parser.Parse(text, True)
    except expat.ExpatError as error:
        raise ValueError("Malformed Cobertura XML") from error
    return tree.close()


def cobertura(text):
    """Require real file/line measurements, not an empty XML shell."""
    root = cobertura_tree(text)
    require(root.tag == "coverage", "Expected Cobertura coverage root")
    measured = set()
    for item in root.findall("./packages/package/classes/class"):
        filename = item.get("filename", "")
        require(bool(filename.strip()), "Missing coverage source filename")
        for line in item.findall("./lines/line"):
            number = integer(line.get("number", ""), positive=True)
            integer(line.get("hits", ""))
            measured.add((filename, number))
    require(bool(measured), "Cobertura report contains no measured lines")
    return len(measured)


def go_profile(text):
    """Require a Go coverage mode and at least one valid measured block."""
    lines = text.splitlines()
    require(
        bool(lines) and lines[0] in {"mode: set", "mode: count", "mode: atomic"},
        "Invalid Go coverage mode",
    )
    records = {}
    for line in lines[1:]:
        match = re.fullmatch(
            r"(.+):(\d+)\.(\d+),(\d+)\.(\d+) (\d+) (\d+)", line, flags=re.ASCII
        )
        require(match is not None, "Invalid Go coverage block")
        start = (integer(match[2], positive=True), integer(match[3], positive=True))
        end = (integer(match[4], positive=True), integer(match[5], positive=True))
        require(start <= end, "Invalid Go coverage range")
        statements = integer(match[6])
        integer(match[7])
        key = (match[1], start, end)
        require(
            records.get(key, statements) == statements,
            "Inconsistent statement count for duplicate Go block",
        )
        records[key] = statements
    measured = sum(statements > 0 for statements in records.values())
    require(measured > 0, "Go report contains no measured statements")
    return measured


def lcov(text):
    """Require terminated LCOV file records with meaningful line counts."""
    measured, current, counts, summary = 0, None, {}, {}
    for line in text.splitlines():
        if line.startswith("SF:"):
            require(
                current is None and bool(line[3:].strip()), "Invalid LCOV source record"
            )
            current, counts, summary = line[3:], {}, {}
        elif line.startswith("DA:"):
            require(current is not None, "LCOV lines lack a source")
            fields = line[3:].split(",")
            require(len(fields) in {2, 3}, "Invalid LCOV line")
            number, hits = integer(fields[0], positive=True), integer(fields[1])
            require(number not in counts, "Duplicate LCOV line")
            counts[number] = hits
        elif line.startswith(("LF:", "LH:")):
            require(
                current is not None and line[:2] not in summary, "Invalid LCOV summary"
            )
            summary[line[:2]] = integer(line[3:])
        elif line == "end_of_record":
            require(current is not None, "LCOV record lacks a source")
            require(
                summary.get("LF", len(counts)) == len(counts),
                "LCOV line total mismatch",
            )
            hits = sum(value > 0 for value in counts.values())
            require(summary.get("LH", hits) == hits, "LCOV hit total mismatch")
            measured += len(counts)
            current = None
        else:
            require(
                re.fullmatch(r"(?:TN|VER|FN|FNDA|FNF|FNH|BRDA|BRF|BRH):[^\x00]*", line)
                is not None,
                "Unsupported LCOV record",
            )
    require(current is None and bool(measured), "Unterminated or empty LCOV report")
    return measured


def validate_report(directory, report_file, format_name):
    """Only the declared regular report may be present in the downloaded artifact."""
    directory = Path(directory)
    safe_path(report_file)
    require(format_name in FORMATS, "Unsupported coverage format")
    require(
        directory.is_dir() and not directory.is_symlink(), "Missing report directory"
    )
    files = []
    for path in directory.rglob("*"):
        require(not path.is_symlink(), "Report artifact contains a symlink")
        if path.is_dir():
            continue
        require(path.is_file(), "Report artifact contains a special file")
        files.append(path.relative_to(directory).as_posix())
    require(
        files == [report_file], "Report artifact must contain exactly the declared file"
    )
    path = directory / report_file
    require(
        0 < path.stat().st_size <= MAX_REPORT_BYTES,
        "Empty or oversized coverage report",
    )
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    require("\x00" not in text, "Coverage report contains NUL bytes")
    measured = {"cobertura": cobertura, "go": go_profile, "lcov": lcov}[format_name](
        text
    )
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "measured": measured,
    }


def main():
    """Use environment inputs so workflow expressions never become shell code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "artifact", "report"])
    parser.add_argument("--directory", type=Path)
    args = parser.parse_args()
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    config = configuration(os.environ, event)
    require(config["mode"] == os.environ["COVERAGE_MODE"], "Wrong upload trust job")
    if args.phase == "prepare":
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.writelines(f"{key}={value}\n" for key, value in config.items())
    elif args.phase == "artifact":
        selected = select_artifact(os.environ, event, artifact_inventory(os.environ))
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.writelines(f"{key}={value}\n" for key, value in selected.items())
        (Path(os.environ["RUNNER_TEMP"]) / "coverage-selection.json").write_text(
            json.dumps(selected, indent=2) + "\n"
        )
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(
                f"Coverage report artifact `{selected['artifact_id']}` from producer attempt "
                f"`{selected['producer_attempt']}` selected for uploader attempt "
                f"`{selected['uploader_attempt']}` in the same immutable run "
                f"`{selected['run_id']}`. This selection does not retest code or qualify "
                "a newer producer attempt; required CI gates remain authoritative.\n"
            )
    else:
        require(args.directory is not None, "Report directory is required")
        result = validate_report(
            args.directory, config["report_file"], config["format"]
        )
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
            summary.write(
                f"Coverage report validated ({config['format']}, {result['bytes']} bytes).\n\nTested checkout: `{config['tested_sha']}`; provider commit: `{config['commit']}`.\n"
            )
        print(json.dumps(result))


if __name__ == "__main__":
    main()
