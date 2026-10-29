"""Standard interleaved tar payloads; encryption/compression remain external.

Preparation and verification require trusted metadata. Preparation is POSIX-only;
the payload is ordinary USTAR and has no platform-specific container format. No keys, network access,
archive extraction or deletion of source files are performed by this helper.
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from pathlib import Path

BLOCK = 1024 * 1024
MAX_MANIFEST = 4 * BLOCK
METADATA_CUSHION = 64 * 1024


def require(value, message):
    if not value:
        raise ValueError(message)


def integer(value):
    return type(value) is int and value >= 0


def sha(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def reserve_gate(path, reserve, pending=0):
    require(integer(reserve) and integer(pending), "Invalid disk reserve")
    quantum = max(4096, os.statvfs(path).f_frsize)
    allocation = ((pending + quantum - 1) // quantum) * quantum + METADATA_CUSHION
    require(shutil.disk_usage(path).free >= reserve + allocation, "Disk reserve exhausted")


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def regular(path):
    before = path.lstat()
    require(stat.S_ISREG(before.st_mode), "Input must be a regular non-link file")
    stream = os.fdopen(os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb")
    try:
        require(
            identity(os.fstat(stream.fileno())) == identity(before),
            "Input changed while opening",
        )
    except BaseException:
        stream.close()
        raise
    return stream, before


def check_catalog(catalog, maximum):
    require(
        set(catalog) == {"schema", "chunk_bytes", "files"}
        and type(catalog["schema"]) is int
        and catalog["schema"] == 1,
        "Unknown manifest schema",
    )
    chunk = catalog["chunk_bytes"]
    require(integer(chunk) and 0 < chunk <= 256 * BLOCK, "Invalid chunk size")
    files = catalog["files"]
    require(
        isinstance(files, list) and 0 < len(files) <= 64, "Invalid original inventory"
    )
    names = set()
    for original in files:
        require(
            set(original) == {"name", "bytes", "sha256", "parts"},
            "Unknown original fields",
        )
        name = original["name"]
        require(
            isinstance(name, str)
            and re.fullmatch("[a-z][a-z0-9_-]{0,31}", name)
            and name not in names,
            "Invalid or duplicate original name",
        )
        names.add(name)
        require(
            integer(original["bytes"]) and sha(original["sha256"]),
            "Invalid original identity",
        )
        parts = original["parts"]
        require(
            isinstance(parts, list)
            and len(parts) == (original["bytes"] + chunk - 1) // chunk
            and len(parts) <= 10000,
            "Invalid part count",
        )
        for number, part in enumerate(parts):
            require(set(part) == {"name", "bytes", "sha256"}, "Unknown part fields")
            require(
                part["name"] == f"parts/{name}/{number:06d}.part"
                and integer(part["bytes"])
                and part["bytes"] == min(chunk, original["bytes"] - number * chunk)
                and sha(part["sha256"]),
                "Invalid part identity/order",
            )
    require(
        integer(maximum) and sum(f["bytes"] for f in files) <= maximum,
        "Original byte budget exceeded",
    )
    require(len(encoded(catalog)) <= MAX_MANIFEST, "Manifest too large")
    return catalog


def interleaved(catalog):
    for number in range(max(len(f["parts"]) for f in catalog["files"])):
        for original in catalog["files"]:
            if number < len(original["parts"]):
                yield original, original["parts"][number]


def prepare(inputs, destination, chunk, maximum, reserve=0):
    require(os.name == "posix", "Recovery preparation requires POSIX")
    require(integer(reserve), "Invalid disk reserve")
    # Construct and validate names, counts and expected sizes before any writes.
    catalog = {"schema": 1, "chunk_bytes": chunk, "files": []}
    require(
        isinstance(inputs, list) and 0 < len(inputs) <= 64 and integer(maximum),
        "Invalid source inventory",
    )
    require(integer(chunk) and 0 < chunk <= 256 * BLOCK, "Invalid chunk size")
    require(
        all(isinstance(item, dict) and integer(item.get("bytes")) for item in inputs),
        "Invalid input sizes",
    )
    require(
        sum(item["bytes"] for item in inputs) <= maximum,
        "Original byte budget exceeded",
    )
    require(
        sum((item["bytes"] + chunk - 1) // chunk for item in inputs) <= 10000,
        "Too many parts",
    )
    for item in inputs:
        require(isinstance(item.get("path"), str), "Source path must be a string")
        require(
            set(item) == {"name", "path", "bytes", "sha256"}, "Invalid source fields"
        )
        require(
            integer(item["bytes"])
            and item["bytes"] <= maximum
            and integer(chunk)
            and 0 < chunk <= 256 * BLOCK
            and (item["bytes"] + chunk - 1) // chunk <= 10000,
            "Invalid source size",
        )
        catalog["files"].append(
            {k: item[k] for k in ("name", "bytes", "sha256")}
            | {
                "parts": [
                    {
                        "name": f"parts/{item['name']}/{i:06d}.part",
                        "bytes": min(chunk, item["bytes"] - i * chunk),
                        "sha256": "0" * 64,
                    }
                    for i in range((item["bytes"] + chunk - 1) // chunk)
                ]
            }
        )
    check_catalog(catalog, maximum)
    reserve_gate(destination.parent, reserve)
    destination.mkdir(mode=0o700, parents=False, exist_ok=False)
    sync_directory(destination.parent)
    reserve_gate(destination, reserve)
    written = 0
    for item, original in zip(inputs, catalog["files"]):
        source = Path(item["path"])
        stream, before = regular(source)
        whole = hashlib.sha256()
        with stream:
            require(before.st_size == original["bytes"], "Source size differs")
            for part in original["parts"]:
                target = destination / part["name"]
                for directory in (destination / "parts", target.parent):
                    if not directory.exists():
                        reserve_gate(directory.parent, reserve)
                        directory.mkdir(mode=0o700)
                        sync_directory(directory.parent)
                        reserve_gate(directory, reserve)
                    require(directory.is_dir() and not directory.is_symlink(), "Unsafe part directory")
                remaining = part["bytes"]
                digest = hashlib.sha256()
                reserve_gate(target.parent, reserve)
                with target.open("xb", buffering=0) as output:
                    os.fchmod(output.fileno(), 0o600)
                    reserve_gate(target.parent, reserve)
                    while remaining:
                        data = stream.read(min(BLOCK, remaining))
                        require(data, "Truncated original")
                        require(
                            written + len(data) <= maximum, "Staging budget exceeded"
                        )
                        reserve_gate(target.parent, reserve, len(data))
                        require(
                            output.write(data) == len(data), "Incomplete part write"
                        )
                        whole.update(data)
                        digest.update(data)
                        remaining -= len(data)
                        written += len(data)
                        reserve_gate(target.parent, reserve)
                    os.fsync(output.fileno())
                sync_directory(target.parent)
                reserve_gate(target.parent, reserve)
                part["sha256"] = digest.hexdigest()
            require(stream.read(1) == b"", "Original grew")
            require(
                identity(os.fstat(stream.fileno())) == identity(before)
                and identity(source.lstat()) == identity(before),
                "Original changed",
            )
            require(whole.hexdigest() == original["sha256"], "Original hash differs")
    check_catalog(catalog, maximum)
    raw = encoded(catalog)
    pending = destination / "manifest.json.pending"
    final = destination / "manifest.json"
    linked = False
    pending_identity = None
    try:
        reserve_gate(destination, reserve, len(raw))
        with pending.open("xb") as output:
            os.fchmod(output.fileno(), 0o600)
            require(output.write(raw) == len(raw), "Incomplete manifest write")
            output.flush()
            os.fsync(output.fileno())
            pending_identity = identity(os.fstat(output.fileno()))[:2]
        sync_directory(destination)
        reserve_gate(destination, reserve)
        os.link(pending, final)
        linked = True
        sync_directory(destination)
        reserve_gate(destination, reserve)
        pending.unlink()
        sync_directory(destination)
        reserve_gate(destination, reserve)
    except BaseException:
        # A failed prepare must not leave a final catalog accepted by pack().
        if linked and final.exists() and identity(final.lstat())[:2] == pending_identity:
            final.unlink()
            sync_directory(destination)
        raise
    return catalog


def physical_size(catalog):
    sizes = [len(encoded(catalog))] + [p["bytes"] for _, p in interleaved(catalog)]
    size = sum(512 + ((n + 511) // 512) * 512 for n in sizes) + 1024
    return ((size + 10239) // 10240) * 10240


class Bounded:
    def __init__(self, stream, limit):
        self.stream, self.limit, self.count = stream, limit, 0

    def read(self, size):
        require(integer(size) and size <= BLOCK, "Unbounded stream read")
        data = self.stream.read(min(size, self.limit - self.count + 1))
        self.count += len(data)
        require(self.count <= self.limit, "Tar input budget exceeded")
        return data

    def write(self, data):
        require(self.count + len(data) <= self.limit, "Tar output budget exceeded")
        count = self.stream.write(data)
        require(count == len(data), "Incomplete tar write")
        self.count += count
        return count


class CheckedPart:
    def __init__(self, stream, part):
        self.stream, self.part, self.count = stream, part, 0
        self.digest = hashlib.sha256()

    def read(self, size):
        require(integer(size) and size <= BLOCK, "Unbounded part read")
        data = self.stream.read(size)
        self.count += len(data)
        self.digest.update(data)
        require(self.count <= self.part["bytes"], "Part grew")
        return data

    def check(self):
        require(
            self.count == self.part["bytes"]
            and self.digest.hexdigest() == self.part["sha256"],
            "Part identity differs",
        )


def check_staging(directory, catalog):
    require(not directory.is_symlink(), "Staging directory is a link")
    files = {"manifest.json"} | {p["name"] for _, p in interleaved(catalog)}
    directories = {
        str(parent)
        for name in files
        for parent in Path(name).parents
        if str(parent) != "."
    }
    seen = set()

    def visit(path):
        with os.scandir(path) as entries:
            for entry in entries:
                relative = str(Path(entry.path).relative_to(directory))
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    require(relative in directories, "Unexpected staging directory")
                    visit(Path(entry.path))
                else:
                    require(
                        stat.S_ISREG(mode) and relative in files,
                        "Unexpected staging file or link",
                    )
                    seen.add(relative)

    visit(directory)
    require(seen == files, "Staging inventory incomplete")
    with (directory / "manifest.json").open("rb") as source:
        require(
            source.read(MAX_MANIFEST + 1) == encoded(catalog), "Staged manifest differs"
        )


def pack(directory, catalog, output, maximum):
    check_catalog(catalog, maximum)
    check_staging(directory, catalog)
    bound = Bounded(output, physical_size(catalog))
    with tarfile.open(fileobj=bound, mode="w|", format=tarfile.USTAR_FORMAT) as archive:
        entries = [("manifest.json", encoded(catalog))] + [
            (p["name"], p) for _, p in interleaved(catalog)
        ]
        for name, value in entries:
            info = tarfile.TarInfo(name)
            info.mode = 0o600
            info.uid = info.gid = info.mtime = 0
            if isinstance(value, bytes):
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
                continue
            target = directory / name
            require(
                not target.parent.is_symlink()
                and not target.parent.parent.is_symlink(),
                "Part directory is a link",
            )
            stream, before = regular(target)
            require(before.st_size == value["bytes"], "Part size differs")
            with stream:
                checked = CheckedPart(stream, value)
                info.size = value["bytes"]
                archive.addfile(info, checked)
                checked.check()
                require(
                    identity(os.fstat(stream.fileno())) == identity(before)
                    and identity(target.lstat()) == identity(before),
                    "Part changed",
                )
    require(bound.count == physical_size(catalog), "Unexpected tar framing")
    return {
        "tar_bytes": bound.count,
        "manifest_sha256": hashlib.sha256(encoded(catalog)).hexdigest(),
    }


def verify(stream, catalog, maximum):
    check_catalog(catalog, maximum)
    bound = Bounded(stream, physical_size(catalog))
    hashes = {f["name"]: hashlib.sha256() for f in catalog["files"]}
    totals = {f["name"]: 0 for f in catalog["files"]}
    with tarfile.open(fileobj=bound, mode="r|") as archive:
        expected = [("manifest.json", None, encoded(catalog))] + [
            (p["name"], f, p) for f, p in interleaved(catalog)
        ]
        for name, original, value in expected:
            member = archive.next()
            size = len(value) if isinstance(value, bytes) else value["bytes"]
            require(
                member is not None
                and member.name == name
                and member.type == tarfile.REGTYPE
                and member.size == size
                and not member.pax_headers
                and not member.issparse(),
                "Unexpected tar member",
            )
            content = archive.extractfile(member)
            if original is None:
                require(
                    content.read(MAX_MANIFEST + 1) == value,
                    "Manifest differs from trusted metadata",
                )
                continue
            checked = CheckedPart(content, value)
            while checked.count < size:
                data = checked.read(min(BLOCK, size - checked.count))
                require(data, "Truncated part")
                hashes[original["name"]].update(data)
                totals[original["name"]] += len(data)
            checked.check()
        require(archive.next() is None, "Unexpected extra tar member")
        while True:
            tail = archive.fileobj.read(10240)
            if not tail:
                break
            require(not tail.strip(b"\0"), "Unexpected nonzero tar trailer")
    require(bound.count == physical_size(catalog), "Truncated or oversized tar stream")
    for original in catalog["files"]:
        require(
            totals[original["name"]] == original["bytes"]
            and hashes[original["name"]].hexdigest() == original["sha256"],
            "Reassembled original differs",
        )
    return {
        "verified": True,
        "manifest_sha256": hashlib.sha256(encoded(catalog)).hexdigest(),
        "originals": totals,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["prepare", "pack", "verify"])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--max-bytes", type=int, required=True)
    parser.add_argument("--chunk-bytes", type=int, default=128 * BLOCK)
    parser.add_argument("--reserve-bytes", type=int, default=0)
    args = parser.parse_args()
    with args.manifest.open("rb") as source:
        raw = source.read(MAX_MANIFEST + 1)
    require(len(raw) <= MAX_MANIFEST, "Oversized metadata")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Duplicate metadata key")
            result[key] = value
        return result

    catalog = json.loads(raw, object_pairs_hook=unique)
    if args.operation == "prepare":
        require(args.directory is not None, "Staging directory required")
        result = prepare(
            catalog,
            args.directory,
            args.chunk_bytes,
            args.max_bytes,
            args.reserve_bytes,
        )
    elif args.operation == "pack":
        require(args.directory is not None, "Staging directory required")
        result = pack(args.directory, catalog, sys.stdout.buffer, args.max_bytes)
    else:
        result = verify(sys.stdin.buffer, catalog, args.max_bytes)
    print(json.dumps(result), file=sys.stderr)


if __name__ == "__main__":
    main()
