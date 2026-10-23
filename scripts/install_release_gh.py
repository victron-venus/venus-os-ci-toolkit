#!/usr/bin/env python3
"""Install or verify the exact GitHub CLI qualified for release operations."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github/gh/release.json"
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 64 * 1024 * 1024


def native_target() -> str:
    system = {"Linux": "linux", "Darwin": "macOS"}.get(platform.system())
    machine = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
    if not system or not machine:
        raise ValueError("Qualified release CLI supports native Linux/macOS amd64/arm64 only")
    return f"{system}_{machine}"


def download(url: str) -> bytes:
    # A pinned stdlib-only helper enforces the same full-chain FIPS profile as gh.
    env = {key: value for key, value in os.environ.items() if not key.startswith(("GO", "CGO"))}
    env.update({"GOTOOLCHAIN": "local", "GO111MODULE": "off", "GOWORK": "off", "GOENV": "off",
                "CGO_ENABLED": "0", "GODEBUG": "fips140=on", "GOMAXPROCS": "2", "GOFLAGS": "-p=2"})
    result = subprocess.run(["go", "version"], env=env, text=True, capture_output=True, check=True, timeout=15)
    if not result.stdout.startswith("go version go1.27.3 "):
        raise ValueError("Release CLI downloader requires exactly Go 1.27.3")
    with tempfile.TemporaryDirectory(prefix="release-gh-download-") as directory:
        output = Path(directory) / "artifact"
        subprocess.run(["go", "run", str(ROOT / "scripts/download_release_gh.go"), url, str(output)],
                       env=env, cwd=directory, check=True, timeout=180)
        data = output.read_bytes()
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Release CLI archive exceeded its size limit")
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def binary_from_archive(data: bytes, artifact: dict) -> bytes:
    if digest(data) != artifact["archive_sha256"]:
        raise ValueError("Release CLI archive checksum differs from the reviewed artifact")
    # Read exactly one regular member, never extract archive paths onto disk.
    name = artifact["member"]
    if artifact["asset"].endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [item for item in archive.infolist() if item.filename == name]
            if len(members) != 1 or members[0].is_dir() or members[0].file_size > MAX_BINARY:
                raise ValueError("Release CLI ZIP member is missing, ambiguous or oversized")
            mode = members[0].external_attr >> 16
            if mode and (mode & 0o170000) not in {0, 0o100000}:
                raise ValueError("Release CLI ZIP member is not a regular file")
            with archive.open(members[0]) as source:
                binary = source.read(MAX_BINARY + 1)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = [item for item in archive.getmembers() if item.name == name]
            if len(members) != 1 or not members[0].isfile() or members[0].size > MAX_BINARY:
                raise ValueError("Release CLI TAR member is missing, ambiguous or oversized")
            with archive.extractfile(members[0]) as source:
                binary = source.read(MAX_BINARY + 1)
    if len(binary) > MAX_BINARY or digest(binary) != artifact["binary_sha256"]:
        raise ValueError("Release CLI executable differs from the reviewed Go build")
    return binary


def verify(path: Path, artifact: dict, version: str) -> None:
    with path.open("rb") as source:
        binary = source.read(MAX_BINARY + 1)
    if len(binary) > MAX_BINARY or digest(binary) != artifact["binary_sha256"]:
        raise ValueError("Release CLI executable does not match the qualified native build")
    if os.environ.get("GODEBUG") != "fips140=on":
        raise ValueError("Release operations require exactly GODEBUG=fips140=on")
    result = subprocess.run([str(path.resolve()), "--version"], text=True, capture_output=True, check=True, timeout=15)
    if not result.stdout.startswith(f"gh version {version} "):
        raise ValueError("Release CLI version does not match the reviewed version")


def install(output: Path, manifest: dict, artifact: dict, archive: Path | None = None) -> None:
    if archive is None:
        url = f'https://github.com/cli/cli/releases/download/v{manifest["version"]}/{artifact["asset"]}'
        data = download(url)
    else:
        with archive.open("rb") as source:
            data = source.read(MAX_ARCHIVE + 1)
        if len(data) > MAX_ARCHIVE:
            raise ValueError("Release CLI archive exceeded its size limit")
    binary = binary_from_archive(data, artifact)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".gh-", delete=False) as temporary:
        temporary.write(binary)
        temporary_path = Path(temporary.name)
    try:
        temporary_path.chmod(0o755)
        verify(temporary_path, artifact, manifest["version"])
        os.replace(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--output", type=Path)
    choice.add_argument("--check", type=Path)
    parser.add_argument("--archive", type=Path,
                        help="Install a previously downloaded archive without network access; requires --output")
    args = parser.parse_args()
    if args.archive and not args.output:
        parser.error("--archive requires --output")
    manifest = json.loads(MANIFEST.read_text())
    target = native_target()
    artifact = manifest["artifacts"][target]
    if args.output:
        install(args.output, manifest, artifact, args.archive)
    else:
        verify(args.check, artifact, manifest["version"])
    print(f'Qualified gh {manifest["version"]}, {manifest["go_version"]}, {target}, FIPS TLS enabled')


if __name__ == "__main__":
    main()
