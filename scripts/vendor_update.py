#!/usr/bin/env python3
"""Deliver a qualified Actions bundle as a signed, draft-only vendor update PR."""

# JSON booleans must not satisfy integer identity/size/schema checks.
# pylint: disable=unidiomatic-typecheck

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# -I excludes the script directory; add only this immutable toolkit sibling path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_control import (  # pylint: disable=wrong-import-position
    REPO_RE,
    GitHubError,
    ReleaseError,
    digest,
    json_bytes,
    parse_json,
    require,
)

MAX_ARCHIVE = 32 * 1024 * 1024
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = REPO_RE
MAIN_REF = "git/ref/heads/main"
OID = r"[0-9a-f]{40}"
RELATIVE = r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*"
VENDOR_BRANCH = r"codex/vendor-[0-9a-f]{64}"
REMOTE = (
    r"https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\.git"
)
PAGE = r"per_page=100&page=(?:[1-9][0-9]?|100)"
API_READS = (
    r"(?:|user)",
    r"actions/runs/[1-9][0-9]*",
    r"actions/runs/[1-9][0-9]*/attempts/[1-9][0-9]*/jobs(?:\?" + PAGE + r")?",
    r"actions/workflows/\.github%2Fworkflows%2F[A-Za-z0-9_.-]+\.ya?ml",
    r"actions/artifacts/[1-9][0-9]*(?:/zip)?",
    r"git/ref/heads/(?:main|" + VENDOR_BRANCH + r")",
    r"git/(?:commits|trees)/" + OID,
    r"commits/" + OID,
    r"pulls\?state=all&head=[A-Za-z0-9][A-Za-z0-9_.-]*%3Acodex%2Fvendor-[0-9a-f]{64}(?:&"
    + PAGE
    + r")?",
)
# Full argv forms, not just command names: no caller can supply new flags.
GIT_FORMS = (
    ("rev-parse", r"(?:--show-toplevel|HEAD|" + OID + ":" + RELATIVE + r")"),
    ("config", "--get", r"remote\.origin\.url"),
    ("status", "--porcelain", "--untracked-files=all"),
    ("show", OID + ":" + RELATIVE),
    ("ls-tree", OID, "--", RELATIVE),
    ("log", "-1", r"--format=(?:%G\?%n%GF|%B)", OID),
    ("rev-list", "--parents", "-n", "1", OID),
    ("merge-base", "--is-ancestor", OID, "HEAD"),
    ("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", OID),
    ("read-tree", OID),
    ("hash-object", "-w", "--stdin", "--no-filters"),
    ("update-index", "--add", "--cacheinfo", "100644," + OID + "," + RELATIVE),
    ("write-tree",),
    ("commit-tree", OID, "-p", OID, "-S"),
    ("fetch", "--no-tags", REMOTE, OID),
    ("push", REMOTE, OID + ":refs/heads/" + VENDOR_BRANCH),
)


def safe_path(value, *, filename=False):
    """Accept portable relative paths, never escapes or Git pathspec syntax."""
    require(isinstance(value, str) and len(value) <= 240, "Invalid path")
    require(
        bool(re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", value)),
        "Unsafe path",
    )
    require(
        all(part.casefold() not in {".", "..", ".git"} for part in value.split("/")),
        "Unsafe path component",
    )
    require(not filename or "/" not in value, "Expected a filename")
    return value


def clean_env():
    """Keep signing configuration while withholding tokens and trace switches."""
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {"SOURCE_TOKEN", "TARGET_TOKEN", "GH_TOKEN", "GITHUB_TOKEN", "GH_DEBUG"}
        and not key.startswith("GIT_TRACE")
    }


class GitHub:
    """Fixed-host API access with a separate token for each repository."""

    def __init__(self, repository, token):
        require(bool(REPOSITORY.fullmatch(repository)), "Invalid repository")
        require(bool(token), "Missing repository token")
        self.repo, self.token = repository, token

    def request(self, path, *, body=None, archive=False):
        """Use only helper-generated REST paths; never display credentials/errors."""
        if body is None:
            require(
                any(re.fullmatch(pattern, path, re.ASCII) for pattern in API_READS),
                "Unsupported API read",
            )
        else:
            require(
                path == "pulls"
                and isinstance(body, dict)
                and set(body) == {"title", "body", "head", "base", "draft"}
                and body["base"] == "main"
                and body["draft"] is True
                and all(isinstance(body[key], str) for key in ("title", "body", "head"))
                and re.fullmatch(VENDOR_BRANCH, body["head"]),
                "Unsupported API mutation",
            )
        require(
            archive == (body is None and path.endswith("/zip")),
            "Invalid API response mode",
        )
        endpoint = "user" if path == "user" else f"repos/{self.repo}/{path}".rstrip("/")
        env = clean_env() | {"GH_TOKEN": self.token, "GH_HOST": "github.com"}
        command = [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "POST" if body else "GET",
        ]
        if body:
            command += ["--input", "-"]
        # Actions ZIP endpoints require the normal REST Accept header; the API
        # redirects to binary storage. octet-stream is for release assets only.
        result = subprocess.run(
            command + ["--", endpoint],
            input=json_bytes(body) if body else None,
            capture_output=True,
            env=env,
            check=False,
            timeout=120,
        )
        if result.returncode:
            raise GitHubError("GitHub request failed", b"HTTP 404" in result.stderr)
        require(
            len(result.stdout) <= (MAX_ARCHIVE if archive else MAX_FILE),
            "Oversized GitHub response",
        )
        return (
            result.stdout if archive else parse_json(result.stdout, "GitHub response")
        )

    def pages(self, path, field=None):
        """Bound pagination and reject malformed API inventories."""
        records = []
        for page in range(1, 101):
            value = self.request(
                path + ("&" if "?" in path else "?") + f"per_page=100&page={page}"
            )
            items = value.get(field) if field else value
            require(isinstance(items, list), "Invalid paginated response")
            records.extend(items)
            if len(items) < 100:
                return records
        raise ReleaseError("API pagination limit exceeded")

    def optional(self, path):
        """Only a real 404 means an absent branch."""
        try:
            return self.request(path)
        except GitHubError as error:
            if error.not_found:
                return None
            raise


def git(directory, *args, data=None, token=None, extra_env=None):
    """Run Git plumbing without hooks, filters, shell expansion or token output."""
    require(
        any(
            len(form) == len(args)
            and all(
                isinstance(value, str) and re.fullmatch(pattern, value, re.ASCII)
                for pattern, value in zip(form, args, strict=True)
            )
            for form in GIT_FORMS
        ),
        "Unsupported Git command arguments",
    )
    directory = Path(directory)
    require(
        directory.is_absolute() and directory.is_dir() and not directory.is_symlink(),
        "Git requires an absolute checkout directory",
    )
    env = clean_env() | (extra_env or {})
    if token:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        for key, value in [
            ("credential.helper", ""),
            ("http.followRedirects", "false"),
            (
                "http.https://github.com/.extraheader",
                "AUTHORIZATION: basic "
                + base64.b64encode(("x-access-token:" + token).encode()).decode(),
            ),
        ]:
            env[f"GIT_CONFIG_KEY_{count}"], env[f"GIT_CONFIG_VALUE_{count}"] = (
                key,
                value,
            )
            count += 1
        env["GIT_CONFIG_COUNT"] = str(count)
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        cwd=directory,
        input=data,
        capture_output=True,
        env=env,
        check=False,
        timeout=120,
    )
    require(
        result.returncode == 0,
        f"Git {args[0]} failed; target branch was not overwritten",
    )
    return result.stdout


def git_text(directory, *args, **kwargs):
    """Read one Git value without altering object bytes."""
    return git(directory, *args, **kwargs).decode().strip()


def verify_checkout(directory, repository, revision=None):
    """Only dedicated, clean, credential-free immutable checkouts are accepted."""
    directory = Path(directory)
    require(
        directory.is_dir() and not directory.is_symlink(), "Invalid checkout directory"
    )
    require(
        Path(git_text(directory, "rev-parse", "--show-toplevel")).resolve()
        == directory.resolve(),
        "Not a checkout root",
    )
    origin = git_text(directory, "config", "--get", "remote.origin.url")
    require(
        origin
        in {
            f"https://github.com/{repository}",
            f"https://github.com/{repository}.git",
            f"git@github.com:{repository}.git",
        },
        "Checkout origin mismatch",
    )
    require(
        not git_text(directory, "status", "--porcelain", "--untracked-files=all"),
        "Checkout is dirty",
    )
    head = git_text(directory, "rev-parse", "HEAD")
    require(not revision or head == revision, "Checkout revision mismatch")
    return head


def validate_run(run, repository, args):
    """Require current-attempt trusted default-branch execution, not PR uploads."""
    require(
        repository.get("full_name") == args.source_repo
        and repository.get("default_branch") == "main",
        "Unexpected source repository/default branch",
    )
    require(
        run.get("repository", {}).get("full_name") == args.source_repo
        and run.get("head_repository", {}).get("full_name") == args.source_repo,
        "Untrusted run repository",
    )
    require(
        type(run.get("id")) is int
        and type(run.get("run_attempt")) is int
        and run.get("id") == args.run_id
        and run.get("run_attempt") == args.run_attempt
        and run.get("head_sha") == args.source_sha
        and run.get("head_branch") == "main"
        and run.get("path") == args.source_workflow,
        "Run identity mismatch",
    )
    require(
        run.get("event") in {"push", "workflow_dispatch"}, "Run event is not trusted"
    )
    require(
        (run.get("status"), run.get("conclusion"))
        in {("in_progress", None), ("completed", "success")},
        "Run is not qualified",
    )


def qualify(source, args):
    """Re-read the run and exact-attempt jobs, including immediately before push."""
    run = source.request(f"actions/runs/{args.run_id}")
    validate_run(run, source.request(""), args)
    workflow = source.request(
        "actions/workflows/" + quote(args.source_workflow, safe="")
    )
    require(
        workflow.get("path") == args.source_workflow
        and workflow.get("id") == run.get("workflow_id"),
        "Workflow identity mismatch",
    )
    jobs = source.pages(
        f"actions/runs/{args.run_id}/attempts/{args.run_attempt}/jobs", "jobs"
    )
    for name in args.qualifying_job:
        matching = [job for job in jobs if job.get("name") == name]
        require(
            len(matching) == 1
            and matching[0].get("status") == "completed"
            and matching[0].get("conclusion") == "success"
            and matching[0].get("run_id") == args.run_id
            and matching[0].get("run_attempt") == args.run_attempt,
            f"Required job did not succeed: {name}",
        )
    return run


def validate_artifact(artifact, run, args):
    """Bind immutable archive bytes to this exact trusted run and attempt."""
    require(
        type(artifact.get("id")) is int
        and artifact.get("id") == args.artifact_id
        and artifact.get("expired") is False,
        "Missing or expired artifact",
    )
    require(artifact.get("digest") == args.artifact_digest, "Artifact digest mismatch")
    require(
        artifact.get("workflow_run", {}).get("id") == args.run_id
        and artifact.get("workflow_run", {}).get("head_sha") == args.source_sha,
        "Artifact provenance mismatch",
    )
    require(
        type(artifact.get("size_in_bytes")) is int
        and 0 < artifact["size_in_bytes"] <= MAX_ARCHIVE,
        "Artifact size limit exceeded",
    )
    try:
        created = datetime.fromisoformat(artifact["created_at"].replace("Z", "+00:00"))
        started = datetime.fromisoformat(run["run_started_at"].replace("Z", "+00:00"))
        require(
            created.tzinfo is not None
            and started.tzinfo is not None
            and created >= started,
            "Artifact predates this attempt",
        )
    except (KeyError, ValueError, TypeError) as error:
        raise ReleaseError("Invalid artifact/run timestamp") from error


# Keep validation sequential: no ZIP entry is read before the whole inventory passes.
# pylint: disable-next=too-many-locals
def validate_bundle(archive, args):
    """Validate an exact bounded ZIP and manifest without extracting any paths."""
    require(
        len(archive) <= MAX_ARCHIVE
        and "sha256:" + digest(archive) == args.artifact_digest,
        "Archive digest mismatch",
    )
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            names = [entry.filename for entry in entries]
            require(
                len(names) == len(set(names)) == len(args.artifact_file)
                and set(names) == set(args.artifact_file),
                "Archive file inventory mismatch",
            )
            require(
                sum(entry.file_size for entry in entries) <= MAX_TOTAL,
                "Archive expansion limit exceeded",
            )
            for entry in entries:
                mode = entry.external_attr >> 16
                require(
                    not entry.is_dir()
                    and stat.S_IFMT(mode) in {0, stat.S_IFREG}
                    and not entry.flag_bits & 1
                    and entry.file_size <= MAX_FILE,
                    "Unsafe archive entry",
                )
            files = {entry.filename: zipped.read(entry) for entry in entries}
    except ReleaseError:
        raise
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ReleaseError("Invalid artifact ZIP") from error
    require(len(files[args.manifest]) <= 1_000_000, "Oversized manifest")
    manifest = parse_json(files[args.manifest], "vendor manifest")
    require(
        isinstance(manifest, dict)
        and type(manifest.get("schema")) is int
        and manifest["schema"] == 1
        and manifest.get("name") == args.manifest_name,
        "Unexpected manifest schema/name",
    )
    artifacts = manifest.get("artifacts")
    require(
        isinstance(artifacts, dict) and set(artifacts) == set(files) - {args.manifest},
        "Manifest artifact inventory mismatch",
    )
    for name, expected in artifacts.items():
        require(
            isinstance(expected, dict)
            and set(expected) == {"sha256", "bytes"}
            and type(expected["bytes"]) is int
            and expected["bytes"] == len(files[name])
            and expected["sha256"] == digest(files[name]),
            f"Artifact bytes differ: {name}",
        )
    source = manifest.get("source")
    require(
        isinstance(source, dict)
        and set(source) == {"sha256", "files"}
        and isinstance(source["files"], dict)
        and 0 < len(source["files"]) <= 2000,
        "Invalid source receipt",
    )
    require(list(source["files"]) == sorted(source["files"]), "Unsorted source receipt")
    # Match JSON.stringify of the packer's sorted, ASCII path -> hex map.
    receipt = json.dumps(
        source["files"], separators=(",", ":"), ensure_ascii=False
    ).encode()
    require(source["sha256"] == digest(receipt), "Source receipt hash mismatch")
    for name, checksum in source["files"].items():
        safe_path(name)
        require(
            isinstance(checksum, str) and HEX.fullmatch(checksum),
            "Invalid source file hash",
        )
    return files, manifest


def verify_source(source, args, manifest):
    """Bind receipt files to Git objects and reject an obsolete source subtree."""
    verify_checkout(args.source_dir, args.source_repo, args.source_sha)
    total = 0
    for name, expected in manifest["source"]["files"].items():
        require(
            re.match(
                r"100(?:644|755) blob ",
                git_text(
                    args.source_dir,
                    "ls-tree",
                    args.source_sha,
                    "--",
                    args.source_tree + "/" + name,
                ),
            ),
            "Source input is not a regular file",
        )
        body = git(
            args.source_dir, "show", f"{args.source_sha}:{args.source_tree}/{name}"
        )
        total += len(body)
        require(total <= MAX_TOTAL, "Source receipt size limit exceeded")
        require(
            len(body) <= MAX_FILE and digest(body) == expected,
            "Receipt does not match source checkout",
        )
    local_tree = git_text(
        args.source_dir, "rev-parse", f"{args.source_sha}:{args.source_tree}"
    )
    latest = source.request(MAIN_REF)["object"]
    require(
        latest.get("type") == "commit" and REVISION.fullmatch(latest.get("sha", "")),
        "Invalid main ref",
    )
    tree = source.request("git/commits/" + latest["sha"])["tree"]["sha"]
    for part in args.source_tree.split("/"):
        listing = source.request("git/trees/" + tree)
        require(listing.get("truncated") is False, "Truncated source tree")
        matches = [
            item
            for item in listing["tree"]
            if item.get("path") == part and item.get("type") == "tree"
        ]
        require(len(matches) == 1, "Source subtree missing")
        tree = matches[0]["sha"]
    require(tree == local_tree, "Source tree is stale; qualify the current main tree")


def target_paths(args):
    """Limit all Git mutations to explicitly declared regular vendor files."""
    return {name: args.destination_prefix + "/" + name for name in args.copy_file}


def signature(directory, revision, fingerprint):
    """Require a valid signature by this delivery identity, not any valid signer."""
    value = git_text(directory, "log", "-1", "--format=%G?%n%GF", revision).splitlines()
    require(
        value == ["G", fingerprint], "Commit lacks the expected valid signing identity"
    )


def existing_commit(args, revision, files, manifest_hash):
    """A retry may reuse only our exact one-commit, signed generated update."""
    signature(args.target_dir, revision, args.signing_fingerprint)
    message = git_text(args.target_dir, "log", "-1", "--format=%B", revision)
    require(
        f"Vendor-Manifest: {manifest_hash}" in message.splitlines()
        and "Vendor-Update: v1" in message.splitlines(),
        "Existing branch is foreign",
    )
    parents = git_text(
        args.target_dir, "rev-list", "--parents", "-n", "1", revision
    ).split()
    require(len(parents) == 2, "Existing vendor commit has unexpected parents")
    git(args.target_dir, "merge-base", "--is-ancestor", parents[1], "HEAD")
    changed = git(
        args.target_dir,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "-z",
        revision,
    ).split(b"\0")[:-1]
    allowed = target_paths(args)
    require(
        changed and set(changed) <= {path.encode() for path in allowed.values()},
        "Existing branch changes foreign paths",
    )
    for name, path in allowed.items():
        require(
            git(args.target_dir, "show", f"{revision}:{path}") == files[name],
            "Existing branch bytes differ",
        )
        require(
            git_text(args.target_dir, "ls-tree", revision, "--", path).startswith(
                "100644 blob "
            ),
            "Unexpected vendor file mode",
        )


def prepare_commit(args, base, files, manifest_hash):
    """Build one signed Git tree without checkout, hooks, filters or file writes."""
    with tempfile.TemporaryDirectory(prefix="vendor-index-") as temporary:
        env = {"GIT_INDEX_FILE": str(Path(temporary) / "index")}
        git(args.target_dir, "read-tree", base, extra_env=env)
        for name, path in target_paths(args).items():
            blob = git_text(
                args.target_dir,
                "hash-object",
                "-w",
                "--stdin",
                "--no-filters",
                data=files[name],
            )
            git(
                args.target_dir,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},{path}",
                extra_env=env,
            )
        tree = git_text(args.target_dir, "write-tree", extra_env=env)
    message = (
        f"chore: update {args.manifest_name} vendor package\n\nVendor-Update: v1\n"
        f"Vendor-Manifest: {manifest_hash}\nVendor-Source: {args.source_repo}@{args.source_sha}\n"
    )
    revision = git_text(
        args.target_dir, "commit-tree", tree, "-p", base, "-S", data=message.encode()
    )
    signature(args.target_dir, revision, args.signing_fingerprint)
    return revision


def target_matches(args, base, files):
    """Check destination types before deciding whether exact bytes are already pinned."""
    paths = target_paths(args)
    for path in paths.values():
        for parent in Path(path).parents:
            if str(parent) != ".":
                existing = git_text(args.target_dir, "ls-tree", base, "--", str(parent))
                require(
                    not existing or existing.startswith("040000 tree "),
                    "Vendor destination crosses a non-directory",
                )
    matches = []
    for name, path in paths.items():
        item = git_text(args.target_dir, "ls-tree", base, "--", path)
        require(
            not item or item.startswith("100644 blob "),
            "Vendor destination is not a regular file",
        )
        matches.append(
            bool(item) and git(args.target_dir, "show", f"{base}:{path}") == files[name]
        )
    return all(matches)


# Keep signing, freshness checks and the single non-forcing push visibly ordered.
# pylint: disable-next=too-many-locals
def deliver(source, target, args, files, manifest):
    """No force pushes, remote code execution, automatic approval or merging."""
    metadata = target.request("")
    require(
        metadata.get("full_name") == args.target_repo
        and metadata.get("default_branch") == "main",
        "Unexpected target repository/default branch",
    )
    base = target.request(MAIN_REF)["object"]["sha"]
    verify_checkout(args.target_dir, args.target_repo, base)
    manifest_hash = digest(files[args.manifest])
    if target_matches(args, base, files):
        return {"status": "noop", "manifest_sha256": manifest_hash, "target_sha": base}
    branch = "codex/vendor-" + manifest_hash
    ref = target.optional("git/ref/heads/" + branch)
    if ref:
        revision = ref["object"]["sha"]
        require(REVISION.fullmatch(revision), "Invalid vendor branch SHA")
        git(
            args.target_dir,
            "fetch",
            "--no-tags",
            f"https://github.com/{args.target_repo}.git",
            revision,
            token=target.token,
        )
        existing_commit(args, revision, files, manifest_hash)
    else:
        revision = prepare_commit(args, base, files, manifest_hash)
    if args.stage_only:
        return {
            "status": "staged",
            "commit": revision,
            "branch": branch,
            "manifest_sha256": manifest_hash,
        }
    qualify(source, args)
    verify_source(source, args, manifest)
    require(
        target.request(MAIN_REF)["object"]["sha"] == base,
        "Target main advanced; retry from its new immutable checkout",
    )
    # A normal push cannot discard commits even if a branch appears concurrently.
    if not ref:
        git(
            args.target_dir,
            "push",
            f"https://github.com/{args.target_repo}.git",
            f"{revision}:refs/heads/{branch}",
            token=target.token,
        )
    require(
        target.request("git/ref/heads/" + branch)["object"]["sha"] == revision,
        "Vendor branch changed concurrently",
    )
    remote_commit = target.request("commits/" + revision)
    require(
        remote_commit.get("sha") == revision
        and remote_commit.get("commit", {}).get("verification", {}).get("verified")
        is True,
        "GitHub did not verify the signed commit",
    )
    owner = target.request("user")["login"]
    pulls = target.pages(
        "pulls?state=all&head="
        + quote(args.target_repo.split("/")[0] + ":" + branch, safe="")
    )
    require(len(pulls) <= 1, "Multiple vendor pull requests exist")
    if pulls:
        pull = pulls[0]
        require(
            pull.get("user", {}).get("login") == owner
            and pull.get("head", {}).get("sha") == revision
            and pull.get("base", {}).get("ref") == "main"
            and pull.get("state") == "open",
            "Existing PR is foreign or closed",
        )
    else:
        body = (
            f"Update `{args.manifest_name}` from `{args.source_repo}@{args.source_sha}`.\n\n"
            f"Qualified run: https://github.com/{args.source_repo}/actions/runs/"
            f"{args.run_id}/attempts/{args.run_attempt}\n"
            f"Artifact ID: {args.artifact_id}; archive: `{args.artifact_digest}`.\n"
            f"Manifest: `{manifest_hash}`; source receipt: `{manifest['source']['sha256']}`.\n\n"
            "Only generated vendor files change. Consumer CI and normal review are required."
        )
        pull = target.request(
            "pulls",
            body={
                "title": f"chore: update {args.manifest_name}",
                "body": body,
                "head": branch,
                "base": "main",
                "draft": True,
            },
        )
    return {
        "status": "pull-request",
        "url": pull["html_url"],
        "commit": revision,
        "branch": branch,
        "manifest_sha256": manifest_hash,
    }


def parse_args(argv=None):
    """All identities and file lists are explicit; the workflow supplies signing."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in [
        "source-repo",
        "source-workflow",
        "source-sha",
        "source-dir",
        "artifact-digest",
        "manifest",
        "manifest-name",
        "target-repo",
        "target-dir",
        "destination-prefix",
        "signing-fingerprint",
    ]:
        parser.add_argument("--" + name, required=True)
    for name in ["run-id", "run-attempt", "artifact-id"]:
        parser.add_argument("--" + name, required=True, type=int)
    for name in ["artifact-file", "copy-file", "qualifying-job"]:
        parser.add_argument(
            "--" + name, action="append", required=name != "qualifying-job"
        )
    parser.add_argument("--source-tree", default="shared-core")
    parser.add_argument("--stage-only", action="store_true")
    args = parser.parse_args(argv)
    require(
        REPOSITORY.fullmatch(args.source_repo)
        and REPOSITORY.fullmatch(args.target_repo),
        "Invalid repository",
    )
    require(REVISION.fullmatch(args.source_sha), "Expected exact source SHA")
    require(
        re.fullmatch(r"sha256:[a-f0-9]{64}", args.artifact_digest),
        "Expected sha256: artifact digest",
    )
    require(
        all(
            getattr(args, name) > 0 for name in ["run_id", "run_attempt", "artifact_id"]
        ),
        "Invalid numeric identity",
    )
    for value in [args.source_tree, args.source_workflow, args.destination_prefix]:
        safe_path(value)
    for name in args.artifact_file + args.copy_file + [args.manifest]:
        safe_path(name, filename=True)
    require(
        len(args.artifact_file) <= 32
        and len(set(args.artifact_file)) == len(args.artifact_file)
        and len(set(args.copy_file)) == len(args.copy_file)
        and set(args.copy_file) <= set(args.artifact_file)
        and args.manifest in args.copy_file,
        "Invalid file whitelist/subset",
    )
    require(
        len({name.casefold() for name in args.artifact_file})
        == len(args.artifact_file),
        "Case-colliding files",
    )
    args.qualifying_job = args.qualifying_job or [
        "portable",
        "browser-client",
        "wire-contracts",
    ]
    # Preserve the documented relative CLI paths without resolving symlink targets.
    args.source_dir = Path(args.source_dir).absolute()
    args.target_dir = Path(args.target_dir).absolute()
    return args


def main(argv=None):
    """Perform remote qualification, byte verification, then one draft PR update."""
    try:
        args = parse_args(argv)
        source = GitHub(args.source_repo, os.environ.get("SOURCE_TOKEN"))
        target = GitHub(args.target_repo, os.environ.get("TARGET_TOKEN"))
        run = qualify(source, args)
        artifact = source.request(f"actions/artifacts/{args.artifact_id}")
        validate_artifact(artifact, run, args)
        archive = source.request(
            f"actions/artifacts/{args.artifact_id}/zip", archive=True
        )
        files, manifest = validate_bundle(archive, args)
        verify_source(source, args, manifest)
        print(
            json_bytes(deliver(source, target, args, files, manifest)).decode(), end=""
        )
    except (
        ReleaseError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.TimeoutExpired,
    ) as error:
        # Never emit subprocess output, environment values or API bodies.
        print(
            "Vendor delivery refused: "
            + (str(error) if isinstance(error, ReleaseError) else type(error).__name__)
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
