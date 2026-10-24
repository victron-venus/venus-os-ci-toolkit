#!/usr/bin/env python3
"""Assemble the pinned npm bundle with two verified upstream dependency replacements."""
import argparse
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE = 20 * 1024 * 1024
MAX_CONTENT = 40 * 1024 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def archive_files(path, pin):
    with Path(path).open("rb") as source:
        data = source.read(MAX_ARCHIVE + 1)
    require(len(data) <= MAX_ARCHIVE, "Archive exceeds size bound")
    require(hashlib.sha256(data).hexdigest() == pin["sha256"], "Archive digest mismatch")
    require("sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode() == pin["integrity"],
            "Archive integrity mismatch")
    files = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        for member in bundle:
            parts = member.name.split("/")
            require(len(parts) > 1 and parts[0] == "package" and
                    all(part not in ("", ".", "..") for part in parts) and
                    "\\" not in member.name and
                    PurePosixPath(member.name).as_posix() == member.name,
                    "Noncanonical archive member")
            require(member.isfile() and not member.issparse(), "Only regular package files are allowed")
            name = "/".join(parts[1:])
            require(name not in files, "Duplicate archive member")
            total += member.size
            require(0 <= member.size <= MAX_CONTENT and total <= MAX_CONTENT and len(files) < 5000,
                    "Unpacked package exceeds bound")
            content = bundle.extractfile(member).read(member.size + 1)
            require(len(content) == member.size, "Truncated package member")
            files[name] = (content, 0o755 if member.mode & 0o111 else 0o644)
    package = json.loads(files["package.json"][0])
    require(package["name"] == pin["name"] and package["version"] == pin["version"],
            "Unexpected package identity")
    return files


def inventory(files):
    return {name: {"sha256": hashlib.sha256(content).hexdigest(), "mode": mode}
            for name, (content, mode) in sorted(files.items())}


PATCHES = {
    "undici": ("6.29.0", "6.29.1", {"node_modules/node-gyp/package.json:dependencies": "^6.25.0"}),
    "ip-address": ("10.7.2", "10.7.4", {"node_modules/socks/package.json:dependencies": "^10.1.1"}),
}


def assemble(npm_archive, dependency_archives, pins):
    original = archive_files(npm_archive, pins["npm"])
    require(set(dependency_archives) == set(PATCHES), "Unexpected replacement set")
    files = dict(original)
    constraints = {}
    for name, (old_version, fixed_version, expected_consumers) in PATCHES.items():
        prefix = "node_modules/" + name + "/"
        current = json.loads(original[prefix + "package.json"][0])
        require(current["name"] == name and current["version"] == old_version,
                "Unexpected original dependency bundle")
        consumers = {}
        for path, (content, _) in original.items():
            if path.endswith("package.json"):
                package = json.loads(content)
                for kind in ("dependencies", "optionalDependencies", "peerDependencies"):
                    if name in package.get(kind, {}):
                        consumers[path + ":" + kind] = package[kind][name]
        require(consumers == expected_consumers, "Bundled consumers changed; review compatibility")
        require(pins[name]["version"] == fixed_version, "Unreviewed replacement version")
        replacement = archive_files(dependency_archives[name], pins[name])
        files = {path: value for path, value in files.items() if not path.startswith(prefix)}
        files.update({prefix + path: value for path, value in replacement.items()})
        constraints[name] = consumers
    before, after = inventory(original), inventory(files)
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    prefixes = tuple("node_modules/" + name + "/" for name in PATCHES)
    require(changed and all(name.startswith(prefixes) for name in changed), "Change escaped selected subtrees")
    digest = hashlib.sha256(json.dumps(after, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    require(digest == pins["bundle_tree_sha256"], "Unexpected assembled bundle inventory")
    lock = {"schema": 1, "npm": pins["npm"],
            "upstream_dependency_replacements": {name: pins[name] for name in PATCHES},
            "original_versions": {name: value[0] for name, value in PATCHES.items()},
            "consumer_constraints": constraints, "changed_files": changed,
            "bundle_tree_sha256": digest, "original_files": before, "installed_files": after}
    return files, lock


def install(npm_archive, dependency_archives, output, pins):
    files, lock = assemble(npm_archive, dependency_archives, pins)
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), "Output already exists")
    temporary = Path(tempfile.mkdtemp(dir=output.parent, prefix=".npm-bundle-"))
    try:
        for name, (content, mode) in files.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as destination:
                destination.write(content)
            path.chmod(mode)
        (temporary / "runner-bundle-lock.json").write_text(json.dumps(lock, sort_keys=True, indent=2) + "\n")
        # Build-only installer: no concurrent publisher is allowed. mkdir is the
        # exclusive claim; files are moved into it only after full validation.
        output.mkdir(mode=0o755)
        for path in temporary.iterdir():
            path.rename(output / path.name)
    finally:
        shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--npm", type=Path, required=True)
    parser.add_argument("--undici", type=Path, required=True)
    parser.add_argument("--ip-address", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pins = json.loads((ROOT / ".github/containers/runner-npm.json").read_text())
    install(args.npm, {"undici": args.undici, "ip-address": args.ip_address}, args.output, pins)


if __name__ == "__main__":
    main()
