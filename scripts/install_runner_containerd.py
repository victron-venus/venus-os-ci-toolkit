#!/usr/bin/env python3
"""Install only the reviewed standalone containerd from Docker's static archive."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import struct
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE = 150 * 1024 * 1024
MEMBERS = {"docker/" + name for name in (
    "containerd", "runc", "containerd-shim-runc-v2", "docker", "docker-proxy",
    "ctr", "dockerd", "docker-init",
)}


def require(value, message):
    if not value:
        raise ValueError(message)


def extract(archive, pins, architecture):
    require(architecture in ("amd64", "arm64"), "Unsupported architecture")
    selected = pins["artifacts"][architecture]
    with Path(archive).open("rb") as source:
        data = source.read(MAX_ARCHIVE + 1)
    require(len(data) <= MAX_ARCHIVE, "Archive exceeds size bound")
    require(hashlib.sha256(data).hexdigest() == selected["archive_sha256"],
            "Archive digest mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as bundle:
        members = bundle.getmembers()
        names = [member.name.rstrip("/") for member in members]
        require(len(names) == len(set(names)), "Duplicate archive member")
        require(set(names) == {"docker", *MEMBERS}, "Unexpected archive inventory")
        for member in members:
            require((member.name.rstrip("/") == "docker" and member.isdir()) or
                    (member.name in MEMBERS and member.isfile()),
                    "Archive links and special entries are prohibited")
        member = bundle.getmember("docker/containerd")
        require(member.size == selected["binary_bytes"], "Binary size mismatch")
        binary = bundle.extractfile(member).read(member.size + 1)
    require(hashlib.sha256(binary).hexdigest() == selected["binary_sha256"],
            "Binary digest mismatch")
    require(len(binary) >= 20 and binary[:6] == b"\x7fELF\x02\x01" and
            struct.unpack_from("<H", binary, 18)[0] == {"amd64": 62, "arm64": 183}[architecture],
            "ELF architecture mismatch")
    return binary


def install(archive, output, pins, architecture):
    binary = extract(archive, pins, architecture)
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), "Output already exists")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".containerd-", delete=False) as staged:
            temporary = Path(staged.name)
            staged.write(binary)
            staged.flush()
            os.fchmod(staged.fileno(), 0o755)
            os.fsync(staged.fileno())
        # Exclusive publication refuses both existing files and raced symlinks.
        os.link(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--architecture", choices=("amd64", "arm64"), required=True)
    args = parser.parse_args()
    pins = json.loads((ROOT / ".github/containers/runner-containerd.json").read_text())
    install(args.archive, args.output, pins, args.architecture)


if __name__ == "__main__":
    main()
