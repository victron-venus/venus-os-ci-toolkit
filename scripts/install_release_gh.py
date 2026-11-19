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
import tarfile
import tempfile
import zipfile
import zlib

from async_process import run_process, resolve_executable

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github/gh/release.json"
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_BINARY = 64 * 1024 * 1024
EXECUTABLE_NAME = "gh"
SYSTEM_IMAGE_EXECUTABLE = Path("/usr/local/bin/gh")


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


def _verified_executable_path(
    path: Path, *, allow_temporary: bool = False, allow_system_image_stage: bool = False,
) -> Path:
    """Resolve an explicit check target, requiring a real qualified gh file."""
    allowed_name = path.name == EXECUTABLE_NAME or (
        allow_temporary and path.name.startswith(".gh-")
    )
    if _path_has_parent_reference(path) or not allowed_name:
        raise ValueError("Release CLI check path must name gh without parent traversal")
    resolved = path.resolve(strict=True)
    roots = [root.resolve() for root in _trusted_path_roots(include_path=True)]
    trusted_root = any(resolved.is_relative_to(root) for root in roots)
    system_image_stage = (
        allow_system_image_stage
        and resolved.parent == SYSTEM_IMAGE_EXECUTABLE.parent.resolve()
        and os.name == "posix"
        and hasattr(os, "geteuid")
        and os.geteuid() == 0
    )
    if not trusted_root and not system_image_stage:
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
    # Execute a private copy of the bytes just hashed. Running `path` here would
    # permit another writer to replace it between the read above and execve.
    with tempfile.TemporaryDirectory(prefix="release-gh-verify-") as directory:
        executable = Path(directory) / EXECUTABLE_NAME
        descriptor = os.open(executable, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(binary)
        executable.chmod(0o700)
        result = run_process([str(executable), "--version"], timeout=15)
    if not result.stdout.startswith(f"gh version {version} ".encode()):
        raise ValueError("Release CLI version does not match the reviewed version")


def _install_destination(path: Path, *, allow_system_image: bool = False) -> Path:
    """Canonicalize a target under an allowed root or the exact image path."""
    if _path_has_parent_reference(path) or path.name != EXECUTABLE_NAME:
        raise ValueError("Release CLI output path must name gh without parent traversal")
    destination = path.parent.resolve() / EXECUTABLE_NAME
    roots = [root.resolve() for root in _trusted_path_roots()]
    exact_system_install = (
        allow_system_image and path == SYSTEM_IMAGE_EXECUTABLE and os.name == "posix"
        and hasattr(os, "geteuid") and os.geteuid() == 0
        and destination == SYSTEM_IMAGE_EXECUTABLE
    )
    if not exact_system_install and not any(destination.is_relative_to(root) for root in roots):
        raise ValueError("Release CLI output must be inside the checkout or runner temp directory")
    if destination.is_symlink():
        raise ValueError("Release CLI output cannot replace a symlink")
    return destination


def _read_local_archive(path: Path) -> bytes:
    """Read a bounded regular archive from a caller-owned trusted root."""
    if _path_has_parent_reference(path):
        raise ValueError("Release CLI archive path cannot contain parent traversal")
    candidate = path if path.is_absolute() else Path.cwd() / path
    if candidate.is_symlink():
        raise ValueError("Release CLI archive cannot be a symlink")
    resolved = candidate.resolve(strict=True)
    roots = [root.resolve() for root in _trusted_path_roots()]
    if not any(resolved.is_relative_to(root) for root in roots):
        raise ValueError("Release CLI archive must be inside the checkout or runner temp directory")
    descriptor = os.open(
        resolved,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise ValueError("Release CLI archive must be a regular file")
    with os.fdopen(descriptor, "rb") as source:
        data = source.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Release CLI archive exceeded its size limit")
    return data


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
    go = resolve_executable("go")
    result = run_process([go, "version"], env=env, timeout=15)
    if not result.stdout.startswith(b"go version go1.27.4 "):
        raise ValueError("Release CLI downloader requires exactly Go 1.27.4")
    with tempfile.TemporaryDirectory(prefix="release-gh-download-") as directory:
        output = Path(directory) / "artifact"
        run_process([go, "run", str(ROOT / "scripts/download_release_gh.go"), url, str(output)],
                    env=env, cwd=directory, timeout=180)
        data = output.read_bytes()
    if len(data) > MAX_ARCHIVE:
        raise ValueError("Release CLI archive exceeded its size limit")
    return data


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_archive_member(name: object) -> str:
    member_path = PurePosixPath(name) if isinstance(name, str) else PurePosixPath(".")
    if (not isinstance(name, str) or "\\" in name or member_path.is_absolute()
            or not member_path.parts or any(part in {"", ".", ".."} for part in member_path.parts)
            or member_path.as_posix() != name):
        raise ValueError("Release CLI archive member path is not canonical and relative")
    return name


def _binary_from_zip(data: bytes, name: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = [item for item in archive.infolist() if item.filename == name]
        if len(members) != 1 or members[0].is_dir() or members[0].file_size > MAX_BINARY:
            raise ValueError("Release CLI ZIP member is missing, ambiguous or oversized")
        mode = members[0].external_attr >> 16
        if mode and (mode & 0o170000) not in {0, 0o100000}:
            raise ValueError("Release CLI ZIP member is not a regular file")
        with archive.open(members[0]) as source:
            return source.read(MAX_BINARY + 1)


def _binary_from_tar(data: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = [item for item in archive.getmembers() if item.name == name]
        if len(members) != 1 or not members[0].isfile() or members[0].size > MAX_BINARY:
            raise ValueError("Release CLI TAR member is missing, ambiguous or oversized")
        source = archive.extractfile(members[0])
        if source is None:
            raise ValueError("Release CLI TAR member cannot be read")
        with source:
            return source.read(MAX_BINARY + 1)


def binary_from_archive(data: bytes, artifact: dict) -> bytes:
    if digest(data) != artifact["archive_sha256"]:
        raise ValueError("Release CLI archive checksum differs from the reviewed artifact")
    # Read exactly one regular member, never extract archive paths onto disk.
    name = _canonical_archive_member(artifact.get("member"))
    try:
        binary = (
            _binary_from_zip(data, name)
            if artifact["asset"].endswith(".zip")
            else _binary_from_tar(data, name)
        )
    except (OSError, EOFError, tarfile.TarError, zipfile.BadZipFile, zlib.error) as error:
        raise ValueError("Release CLI archive is malformed") from error
    if len(binary) > MAX_BINARY or digest(binary) != artifact["binary_sha256"]:
        raise ValueError("Release CLI executable differs from the reviewed Go build")
    return binary


def verify(
    path: Path, artifact: dict, version: str, *, allow_temporary: bool = False,
    allow_system_image_stage: bool = False,
) -> None:
    executable = _verified_executable_path(
        path, allow_temporary=allow_temporary, allow_system_image_stage=allow_system_image_stage,
    )
    _verify_executable_bytes(executable, artifact, version)


def install(
    output: Path, manifest: dict, artifact: dict, archive: Path | None = None,
    *, allow_system_image: bool = False,
) -> None:
    output = _install_destination(output, allow_system_image=allow_system_image)
    data = _release_archive(manifest, artifact, archive)
    binary = binary_from_archive(data, artifact)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".gh-", delete=False) as temporary:
            # Capture the name as soon as the file exists so write and close
            # failures are covered by the same cleanup path as verification.
            temporary_path = Path(temporary.name)
            temporary.write(binary)
        temporary_path.chmod(0o755)
        # The private temporary path is created under the validated parent.
        verify_options = {"allow_system_image_stage": True} if allow_system_image else {}
        verify(temporary_path, artifact, manifest["version"], allow_temporary=True, **verify_options)
        os.replace(temporary_path, output)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _release_archive(manifest: dict, artifact: dict, archive: Path | None) -> bytes:
    """Use the reviewed download path or a bounded explicit local archive."""
    if archive is not None:
        return _read_local_archive(archive)
    url = f'https://github.com/cli/cli/releases/download/v{manifest["version"]}/{artifact["asset"]}'
    return download(url)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--output", type=Path)
    choice.add_argument("--check", type=Path)
    parser.add_argument("--system-image-install", action="store_true",
                        help="Allow only root-owned /usr/local/bin/gh image-layer installation")
    parser.add_argument("--archive", type=Path,
                        help="Install a regular archive inside the checkout or runner temp without network access")
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
