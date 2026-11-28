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
INVALID_COPIED_SOURCE_SUCCESSOR_CHAIN = "Invalid compact copied-source successor chain: "


def digest(data: bytes) -> str:
    """Hash exact bytes, including consumer headers and line endings."""
    return hashlib.sha256(data).hexdigest()


def _copied_successor_hashes(layers: object, previous: str, name: str) -> list[str]:
    """Validate the exact source-bound postimage chain for copied files."""
    if not isinstance(layers, list):
        raise ValueError(INVALID_COPIED_SOURCE_SUCCESSOR_CHAIN + name)
    accepted = [previous]
    for layer in layers:
        if (not isinstance(layer, dict) or layer.get("before_sha256") != previous or
                not isinstance(layer.get("after_sha256"), str) or
                len(layer["after_sha256"]) != 64 or
                any(char not in "0123456789abcdef" for char in layer["after_sha256"]) or
                not isinstance(layer.get("canonical_source_before_sha256"), str) or
                len(layer["canonical_source_before_sha256"]) != 64 or
                not isinstance(layer.get("canonical_source_sha256"), str) or
                len(layer["canonical_source_sha256"]) != 64 or
                not isinstance(layer.get("source_hunks"), list) or not layer["source_hunks"]):
            raise ValueError(INVALID_COPIED_SOURCE_SUCCESSOR_CHAIN + name)
        previous = layer["after_sha256"]
        accepted.append(previous)
    return accepted


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
    layers = specification.get("successor_layers", [])
    copy_layers = specification.get("copy_successor_layers", [])
    expected_source = (layers[-1].get("canonical_source_sha256") if layers
                       else copy_layers[-1].get("canonical_source_sha256") if copy_layers
                       else specification["source_sha256"])
    if data is None or digest(data) != expected_source:
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
    states, successor_states, additions, changes = set(), set(), {}, {}
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
        if entry.get("addition") is True:
            copy_layers = entry.get("copy_successor_layers", [])
            addition_group = entry.get("addition_group", "default")
            if (entry.get("before_sha256") is not None or entry.get("hunks") or
                    not isinstance(copy_layers, list) or
                    not isinstance(addition_group, str) or not addition_group):
                raise ValueError("Invalid compact migration addition: " + name)
            previous = entry["after_sha256"]
            accepted = _copied_successor_hashes(copy_layers, previous, name)
            if identity is None:
                additions.setdefault(addition_group, []).append(False)
                source_bytes = canonical_copy(entry)
                if digest(source_bytes) != accepted[-1]:
                    raise ValueError("Compact migration postimage changed: " + name)
                if name.endswith(".py"):
                    ast.parse(source_bytes, filename=name)
                changes[name] = source_bytes.decode("utf-8")
                continue
            if identity not in accepted:
                raise ValueError("Unknown compact migration preimage: " + name)
            generation = accepted.index(identity)
            additions.setdefault(addition_group, []).append(generation == len(copy_layers))
            if generation == len(copy_layers):
                continue
            after = canonical_copy(entry)
            if digest(after) != accepted[-1]:
                raise ValueError("Compact migration copied-source postimage changed: " + name)
            if name.endswith(".py"):
                ast.parse(after, filename=name)
            changes[name] = after.decode("utf-8")
            continue
        layers = entry.get("successor_layers", [])
        test_layers = entry.get("test_only_successor_layers", [])
        test_overlays = entry.get("test_only_overlay_successors", [])
        if not isinstance(layers, list):
            raise ValueError("Invalid compact migration successor layers: " + name)
        if (not isinstance(test_layers, list) or not isinstance(test_overlays, list) or
                ((test_layers or test_overlays) and name != ".github/release-tests/test_release_control.py")):
            raise ValueError("Invalid independent compact test successor layers: " + name)
        prior_layer_digest = entry["after_sha256"]
        production_digests = [prior_layer_digest]
        for layer in layers:
            if (not isinstance(layer, dict) or
                    layer.get("before_sha256") != prior_layer_digest or
                    not isinstance(layer.get("after_sha256"), str) or
                    len(layer["after_sha256"]) != 64 or
                    any(char not in "0123456789abcdef" for char in layer["after_sha256"]) or
                    not isinstance(layer.get("hunks"), list) or not layer["hunks"]):
                raise ValueError("Invalid compact migration successor chain: " + name)
            prior_layer_digest = layer["after_sha256"]
            production_digests.append(prior_layer_digest)
        overlay_groups = []
        for layer in test_layers:
            if (not isinstance(layer, dict) or
                    not isinstance(layer.get("after_sha256"), str) or
                    len(layer["after_sha256"]) != 64 or
                    any(char not in "0123456789abcdef" for char in layer["after_sha256"]) or
                    not isinstance(layer.get("hunks"), list) or not layer["hunks"]):
                raise ValueError("Invalid independent compact test successor chain: " + name)
        if test_layers:
            base = test_layers[0].get("before_sha256")
            if base not in production_digests:
                raise ValueError("Independent test overlay has an unknown production base: " + name)
            previous = base
            for layer in test_layers:
                if layer.get("before_sha256") != previous:
                    raise ValueError("Invalid independent compact test successor chain: " + name)
                previous = layer["after_sha256"]
            overlay_groups.append({"base_sha256": base, "layers": test_layers})
        for group in test_overlays:
            if not isinstance(group, dict) or group.get("base_sha256") not in production_digests:
                raise ValueError("Independent test overlay has an unknown production base: " + name)
            group_layers = group.get("layers")
            if not isinstance(group_layers, list) or not group_layers:
                raise ValueError("Invalid independent test overlay successor: " + name)
            previous = group["base_sha256"]
            for layer in group_layers:
                if (not isinstance(layer, dict) or layer.get("before_sha256") != previous or
                        not isinstance(layer.get("after_sha256"), str) or
                        len(layer["after_sha256"]) != 64 or
                        any(char not in "0123456789abcdef" for char in layer["after_sha256"]) or
                        not isinstance(layer.get("hunks"), list) or not layer["hunks"]):
                    raise ValueError("Invalid independent test overlay successor chain: " + name)
                previous = layer["after_sha256"]
            overlay_groups.append({"base_sha256": group["base_sha256"], "layers": group_layers})
        applied_overlay = None
        if before is not None:
            matches = []
            for group_index, group in enumerate(overlay_groups):
                for index, layer in enumerate(group["layers"], 1):
                    if identity == layer["after_sha256"]:
                        matches.append((group_index, index))
            if len(matches) > 1:
                raise ValueError("Ambiguous independent test overlay state: " + name)
            if matches:
                group_index, applied_generation = matches[0]
                source_before_overlay = before.decode("utf-8")
                for layer in reversed(overlay_groups[group_index]["layers"][:applied_generation]):
                    for hunk in reversed(layer["hunks"]):
                        after_hunk = hunk.get("after")
                        if not after_hunk or source_before_overlay.count(after_hunk) != 1:
                            raise ValueError("Independent test successor cannot be reversed: " + name)
                        source_before_overlay = source_before_overlay.replace(after_hunk, hunk["before"], 1)
                before = source_before_overlay.encode("utf-8")
                identity = digest(before)
                if identity != overlay_groups[group_index]["base_sha256"]:
                    raise ValueError("Independent test overlay preimage changed: " + name)
                applied_overlay = (group_index, applied_generation)
        start_layer = 0
        if entry["before_sha256"] is None:
            if test_layers or test_overlays:
                raise ValueError("Independent test successors cannot extend additions: " + name)
            copy_layers = entry.get("copy_successor_layers", [])
            addition_layers = entry.get("successor_layers", [])
            previous = entry["after_sha256"]
            accepted = [previous]
            for layer in addition_layers:
                if (not isinstance(layer, dict) or layer.get("before_sha256") != previous or
                        not isinstance(layer.get("after_sha256"), str) or
                        len(layer["after_sha256"]) != 64 or
                        any(char not in "0123456789abcdef" for char in layer["after_sha256"]) or
                        not isinstance(layer.get("hunks"), list) or not layer["hunks"]):
                    raise ValueError(INVALID_COPIED_SOURCE_SUCCESSOR_CHAIN + name)
                previous = layer["after_sha256"]
                accepted.append(previous)
            accepted.extend(_copied_successor_hashes(copy_layers, previous, name)[1:])
            if identity is None:
                states.add("original")
                source_bytes = canonical_copy(entry)
                latest = accepted[-1]
                if digest(source_bytes) != latest:
                    raise ValueError("Compact migration postimage changed: " + name)
                if name.endswith(".py"):
                    ast.parse(source_bytes, filename=name)
                changes[name] = source_bytes.decode("utf-8")
                continue
            if identity not in accepted:
                raise ValueError("Unknown compact migration preimage: " + name)
            generation = accepted.index(identity)
            states.add("current")
            if copy_layers:
                successor_states.add(generation == len(accepted) - 1)
            elif addition_layers:
                successor_states.add(generation == len(accepted) - 1)
            if generation == len(accepted) - 1:
                continue
            source_bytes = canonical_copy(entry)
            if digest(source_bytes) != accepted[-1]:
                raise ValueError("Compact migration copied-source postimage changed: " + name)
            if name.endswith(".py"):
                ast.parse(source_bytes, filename=name)
            changes[name] = source_bytes.decode("utf-8")
            continue
        elif identity == entry["before_sha256"]:
            states.add("original")
            source = before.decode("utf-8")
            for hunk in entry["hunks"]:
                if not hunk["before"] or source.count(hunk["before"]) != 1:
                    raise ValueError("Compact migration hunk context changed: " + name)
                source = source.replace(hunk["before"], hunk["after"], 1)
            if digest(source.encode("utf-8")) != entry["after_sha256"]:
                raise ValueError("Compact migration postimage changed: " + name)
            if layers:
                successor_states.add(False)
        else:
            # A consumer already on an accepted postimage may advance through
            # successors, but every selected file must be on the same layer.
            prior = entry["after_sha256"]
            generation = 0 if identity == prior else None
            for index, layer in enumerate(layers, 1):
                if (not isinstance(layer, dict) or layer.get("before_sha256") != prior or
                        not isinstance(layer.get("hunks"), list) or not layer["hunks"]):
                    raise ValueError("Invalid compact migration successor chain: " + name)
                prior = layer.get("after_sha256")
                if identity == prior:
                    generation = index
            if generation is None:
                raise ValueError("Unknown compact migration preimage: " + name)
            states.add("current")
            if layers:
                successor_states.add(generation == len(layers))
            source = before.decode("utf-8")
            start_layer = generation
            if digest(before) != (entry["after_sha256"] if generation == 0 else layers[generation - 1]["after_sha256"]):
                raise ValueError("Compact migration successor preimage changed: " + name)
            if generation == len(layers):
                matching_overlay = [i for i, group in enumerate(overlay_groups)
                                    if group["base_sha256"] == identity]
                if not matching_overlay or (applied_overlay and
                        applied_overlay[0] == matching_overlay[0] and
                        applied_overlay[1] == len(overlay_groups[matching_overlay[0]]["layers"])):
                    continue

        for layer in layers[start_layer:]:
            for hunk in layer["hunks"]:
                if not hunk.get("before") or source.count(hunk["before"]) != 1:
                    raise ValueError("Compact migration successor context changed: " + name)
                source = source.replace(hunk["before"], hunk["after"], 1)
            if digest(source.encode("utf-8")) != layer["after_sha256"]:
                raise ValueError("Compact migration successor postimage changed: " + name)
        final_production = digest(source.encode("utf-8"))
        matching_overlay = [i for i, group in enumerate(overlay_groups)
                            if group["base_sha256"] == final_production]
        if len(matching_overlay) > 1:
            raise ValueError("Multiple independent test overlays target one production generation: " + name)
        if matching_overlay:
            group_index = matching_overlay[0]
            overlay_start = (applied_overlay[1] if applied_overlay and applied_overlay[0] == group_index
                             else 0)
            for layer in overlay_groups[group_index]["layers"][overlay_start:]:
                for hunk in layer["hunks"]:
                    if not hunk.get("before") or source.count(hunk["before"]) != 1:
                        raise ValueError("Independent test successor context changed: " + name)
                    source = source.replace(hunk["before"], hunk["after"], 1)
                if digest(source.encode("utf-8")) != layer["after_sha256"]:
                    raise ValueError("Independent test successor postimage changed: " + name)
        after = source.encode("utf-8")
        if name.endswith(".py"):
            ast.parse(after, filename=name)
        changes[name] = after.decode("utf-8")
    if len(states) > 1 or len(successor_states) > 1:
        raise ValueError("Partially migrated compact bundle requires explicit review")
    for group, generations in additions.items():
        if any(generations) != all(generations):
            raise ValueError("Partially migrated compact bundle requires explicit review")
        if group == "default" and generations and generations[0]:
            if states != {"current"} or successor_states != {True}:
                raise ValueError("Partially migrated compact bundle requires explicit review")
        elif (group == "default" and generations and states == {"current"}
              and successor_states == {True}):
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
