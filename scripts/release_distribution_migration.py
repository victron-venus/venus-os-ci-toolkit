"""Apply only reviewed compact-manifest scripts/tests/docs to known consumers.

The delta admits exact whole-file preimages and postimages. It does not refresh
workflows, policy, signing restrictions or a consumer's whole-toolkit revision.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DELTA = Path(__file__).with_name("release_distribution_delta.json")
HEADER = (
    "# Vendored release toolkit; change the toolkit source, then render again.\n"
    "# ruff: noqa\n# mypy: ignore-errors\n# pylint: skip-file\n# fmt: off\n"
)


def digest(data: bytes) -> str:
    """Hash exact bytes, including consumer headers and line endings."""
    return hashlib.sha256(data).hexdigest()


def destination(name: str) -> bool:
    """Keep every writable destination inside the explicit migration boundary."""
    parts = Path(name).parts
    return (name == Path(name).as_posix() and not Path(name).is_absolute() and ".." not in parts and
            ((name.startswith("scripts/") and name.endswith(".py")) or
             (name.startswith(".github/release-tests/") and name.endswith(".py")) or
             (name.startswith("docs/") and name.endswith(".md"))))


def local_path(directory: Path, name: str) -> Path:
    """Reject symlinks and non-directory parents before reading or staging."""
    root = directory.resolve(strict=True)
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid compact migration path")
    path = root / relative
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Compact migration rejects file aliases: " + name)
        if parent != path and (not parent.exists() or not parent.is_dir()):
            raise ValueError("Compact migration requires existing directories: " + name)
    return path


def read_optional(directory: Path, name: str) -> bytes | None:
    """Read only bounded, singly linked local regular files."""
    path = local_path(directory, name)
    if not path.exists():
        return None
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1_000_000:
        raise ValueError("Compact migration requires bounded regular files: " + name)
    return path.read_bytes()


def unique_object(pairs):
    """Do not admit duplicate policy or migration keys."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate compact migration JSON key")
        result[key] = value
    return result


def canonical_copy(specification: dict) -> bytes:
    """Copy only newly introduced, hash-pinned helper/test/document sources."""
    name = specification["source"]
    if not name.startswith(("scripts/", "tests/", "docs/")):
        raise ValueError("Unsupported compact source inventory")
    data = read_optional(ROOT, name)
    if data is None or digest(data) != specification["source_sha256"]:
        raise ValueError("Compact source changed; review migration inventory: " + name)
    source = data.decode("utf-8")
    if name.startswith("tests/"):
        source = source.replace("Path(__file__).parents[1]", "Path(__file__).parents[2]")
        source = source.replace("Path(__file__).resolve().parents[1]", "Path(__file__).resolve().parents[2]")
    if name.endswith(".py"):
        lines = source.splitlines(keepends=True)
        lines.insert(1 if lines and lines[0].startswith("#!") else 0, HEADER)
        source = "".join(lines)
    return source.encode("utf-8")


def render(directory: Path) -> dict[str, str]:
    """Validate the complete selected bundle, then return only known changes."""
    specification = json.loads(DELTA.read_bytes(), object_pairs_hook=unique_object)
    if (not isinstance(specification, dict) or type(specification.get("schema")) is not int or
            specification["schema"] != 1 or not isinstance(specification.get("files"), dict) or
            not specification["files"]):
        raise ValueError("Invalid compact migration inventory")
    policy_raw = read_optional(directory, ".release-policy.json")
    if policy_raw is None:
        raise ValueError("Compact migration requires an existing policy")
    policy = json.loads(policy_raw, object_pairs_hook=unique_object)
    if not isinstance(policy, dict):
        raise ValueError("Compact migration requires a policy object")
    versioning = policy.get("versioning")
    if (policy.get("repository") != specification["repository"] or
            policy.get("mode", "release") != "release" or
            policy.get("visibility", "public") != "public" or
            policy.get("ci_execution", "github") != "github" or
            not isinstance(versioning, dict) or type(versioning.get("schema")) is not int or
            versioning["schema"] != 1 or versioning.get("promotion") != "promote-bytes"):
        raise ValueError("Compact migration requires the reviewed public promote-bytes consumer")
    if "stable_distribution" in policy and policy["stable_distribution"] != "rc-payloads-v1":
        raise ValueError("stable_distribution must be rc-payloads-v1 or absent")
    if policy.get("stable_distribution") == "rc-payloads-v1" and policy.get("sign_releases") is True:
        raise ValueError("Compact stable_distribution does not support full-inventory signing")
    for name in specification.get("absent_paths", []):
        if not destination(name) or read_optional(directory, name) is not None:
            raise ValueError("Compact migration requires reviewed optional-module absence")
    states, changes = set(), {}
    for name, entry in specification["files"].items():
        if not destination(name):
            raise ValueError("Compact migration destination is outside scripts/tests/docs")
        if (not isinstance(entry.get("after_sha256"), str) or len(entry["after_sha256"]) != 64 or
                any(c not in "0123456789abcdef" for c in entry["after_sha256"])):
            raise ValueError("Invalid compact migration postimage identity")
        before = read_optional(directory, name)
        if before is None and entry.get("optional"):
            continue
        identity = digest(before) if before is not None else None
        if identity == entry["after_sha256"]:
            states.add("current")
            continue
        if identity != entry["before_sha256"]:
            raise ValueError("Unknown compact migration preimage: " + name)
        states.add("original")
        if entry["before_sha256"] is None:
            after = canonical_copy(entry)
        else:
            source = before.decode("utf-8")
            for hunk in entry["hunks"]:
                if not hunk["before"] or source.count(hunk["before"]) != 1:
                    raise ValueError("Compact migration hunk context changed: " + name)
                source = source.replace(hunk["before"], hunk["after"], 1)
            after = source.encode("utf-8")
        if digest(after) != entry["after_sha256"]:
            raise ValueError("Compact migration postimage changed: " + name)
        if name.endswith(".py"):
            ast.parse(after, filename=name)
        changes[name] = after.decode("utf-8")
    if len(states) > 1:
        raise ValueError("Partially migrated compact bundle requires explicit review")
    return changes


def stage(path: Path, data: bytes, mode: int) -> Path:
    """Prepare and fsync one sibling file without replacing its destination."""
    descriptor, name = tempfile.mkstemp(prefix=".release-distribution-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def apply(directory: Path, changes: dict[str, str]) -> None:
    """Stage all files, recheck preimages, replace atomically and roll back errors.

    Filesystem rename is atomic per file. A process crash is not a multi-file
    transaction; mixed states are deliberately rejected by the next invocation.
    """
    if render(directory) != changes:
        raise ValueError("Compact migration inputs changed before staging")
    before, prepared, backups, committed = {}, {}, {}, []
    try:
        for name, source in changes.items():
            path = local_path(directory, name)
            before[name] = read_optional(directory, name)
            mode = stat.S_IMODE(path.stat().st_mode) if before[name] is not None else (0o755 if name.endswith(".py") else 0o644)
            prepared[name] = stage(path, source.encode("utf-8"), mode)
            if before[name] is not None:
                backups[name] = stage(path, before[name], mode)
        for name, original in before.items():
            if read_optional(directory, name) != original:
                raise ValueError("Compact migration inputs changed during staging")
        for name, temporary in prepared.items():
            if read_optional(directory, name) != before[name]:
                raise ValueError("Compact migration input changed before replacement")
            os.replace(temporary, local_path(directory, name))
            committed.append(name)
    except BaseException:
        for name in reversed(committed):
            if read_optional(directory, name) != changes[name].encode("utf-8"):
                raise ValueError("Compact rollback found concurrent drift; explicit review required") from None
            path = local_path(directory, name)
            if before[name] is None:
                path.unlink()
            else:
                os.replace(backups[name], path)
        raise
    finally:
        for temporary in (*prepared.values(), *backups.values()):
            temporary.unlink(missing_ok=True)
