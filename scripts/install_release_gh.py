#!/usr/bin/env python3
"""Install or verify the exact GitHub CLI qualified for release operations."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github/gh/release.json"
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 64 * 1024 * 1024
EXECUTABLE_NAME = "gh"


def _path_has_parent_reference(path: Path) -> bool:
    """Reject lexical parent traversal before resolving a caller-supplied path."""
    return ".." in path.parts


def _trusted_path_roots(*, include_path: bool = False) -> list[Path]:
    """Return caller-owned roots from which release CLI files may be checked."""
    roots = [Path.cwd().absolute(), Path(tempfile.gettempdir()).absolute()]
    runner_temp = os.environ.get("RUNNER_TEMP")
    if runner_temp:
        roots.append(Path(runner_temp).absolute())
    if include_path:
        roots.extend(Path(entry or ".").absolute() for entry in os.environ.get("PATH", "").split(os.pathsep))
    return roots


def _verified_executable_path(path: Path, *, allow_temporary: bool = False) -> Path:
    """Resolve an explicit check target, requiring a real qualified gh file."""
    allowed_name = path.name == EXECUTABLE_NAME or (
        allow_temporary and path.name.startswith(".gh-")
    )
    if _path_has_parent_reference(path) or not allowed_name:
        raise ValueError("Release CLI check path must name gh without parent traversal")
    resolved = path.resolve(strict=True)
    roots = [root.resolve() for root in _trusted_path_roots(include_path=True)]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("Release CLI check target must be inside a trusted executable directory")
    metadata = resolved.stat()
    resolved_name_ok = resolved.name == EXECUTABLE_NAME or (
        allow_temporary and resolved.name.startswith(".gh-")
    )
    if not resolved_name_ok or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("Release CLI check target must be a regular gh file")
    return resolved


def _verify_executable_bytes(path: Path, artifact: dict, version: str) -> None:
    """Validate one already-resolved executable before it is used or installed."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        binary = source.read(MAX_BINARY + 1)
    if len(binary) > MAX_BINARY or digest(binary) != artifact["binary_sha256"]:
        raise ValueError("Release CLI executable does not match the qualified native build")
    if os.environ.get("GODEBUG") != "fips140=on":
        raise ValueError("Release operations require exactly GODEBUG=fips140=on")
    result = subprocess.run([str(path), "--version"], text=True, capture_output=True, check=True, timeout=15)
    if not result.stdout.startswith(f"gh version {version} "):
        raise ValueError("Release CLI version does not match the reviewed version")


def _install_destination(path: Path, *, allow_system_image: bool = False) -> Path:
    """Canonicalize a target under an allowed root or the exact image path."""
    if _path_has_parent_reference(path) or path.name != EXECUTABLE_NAME:
        raise ValueError("Release CLI output path must name gh without parent traversal")
    destination = path.parent.resolve() / EXECUTABLE_NAME
    roots = [root.resolve() for root in _trusted_path_roots()]
    private_image_target = Path("/usr/local/bin/gh")
    exact_system_install = (
        allow_system_image and path == private_image_target and os.name == "posix"
        and hasattr(os, "geteuid") and os.geteuid() == 0
        and destination == private_image_target
    )
    if not exact_system_install and not any(destination.is_relative_to(root) for root in roots):
        raise ValueError("Release CLI output must be inside the checkout or runner temp directory")
    if destination.is_symlink():
        raise ValueError("Release CLI output cannot replace a symlink")
    return destination


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
    if not result.stdout.startswith("go version go1.27.4 "):
        raise ValueError("Release CLI downloader requires exactly Go 1.27.4")
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
    member_path = PurePosixPath(name) if isinstance(name, str) else PurePosixPath(".")
    if (not isinstance(name, str) or "\\" in name or member_path.is_absolute()
            or not member_path.parts or any(part in {"", ".", ".."} for part in member_path.parts)
            or member_path.as_posix() != name):
        raise ValueError("Release CLI archive member path is not canonical and relative")
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


def verify(path: Path, artifact: dict, version: str, *, allow_temporary: bool = False) -> None:
    executable = _verified_executable_path(path, allow_temporary=allow_temporary)
    _verify_executable_bytes(executable, artifact, version)


def install(
    output: Path, manifest: dict, artifact: dict, archive: Path | None = None,
    *, allow_system_image: bool = False,
) -> None:
    output = _install_destination(output, allow_system_image=allow_system_image)
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
        # The private temporary path is created under the validated parent.
        verify(temporary_path, artifact, manifest["version"], allow_temporary=True)
        os.replace(temporary_path, output)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--output", type=Path)
    choice.add_argument("--check", type=Path)
    parser.add_argument("--system-image-install", action="store_true",
                        help="Allow only root-owned /usr/local/bin/gh image-layer installation")
    parser.add_argument("--archive", type=Path,
                        help="Install a previously downloaded archive without network access; requires --output")
    args = parser.parse_args()
    if args.archive and not args.output:
        parser.error("--archive requires --output")
    if args.system_image_install and (not args.archive or not args.output):
        parser.error("--system-image-install requires --archive and --output")
    manifest = json.loads(MANIFEST.read_text())
    target = native_target()
    artifact = manifest["artifacts"][target]
    if args.output:
        install(args.output, manifest, artifact, args.archive,
                allow_system_image=args.system_image_install)
    else:
        verify(args.check, artifact, manifest["version"])
    print(f'Qualified gh {manifest["version"]}, {manifest["go_version"]}, {target}, FIPS TLS enabled')


if __name__ == "__main__":
    main()
