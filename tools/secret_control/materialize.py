"""Materialize an independently verified bundle as an immutable private file."""

import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import stat


class MaterializationError(ValueError):
    """Fixed diagnostics safe to expose without secret content or file paths."""


def _parent_fd(parent):
    """Walk using directory descriptors: no symlink component can be followed."""
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parent.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise MaterializationError('output parent must be owned mode 0700')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _existing_matches(parent, name, payload):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except FileNotFoundError:
        return False
    try:
        before = os.fstat(fd)
        if not (stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1):
            raise MaterializationError('existing output is not a private regular file')
        if before.st_size != len(payload):
            raise MaterializationError('existing output differs; use a new destination')
        chunks = bytearray()
        while len(chunks) <= len(payload):
            chunk = os.read(fd, min(65536, len(payload) + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                or (named.st_dev, named.st_ino) != (after.st_dev, after.st_ino)
                or not hmac.compare_digest(chunks, payload)):
            raise MaterializationError('existing output differs or changed')
        return True
    finally:
        os.close(fd)


def materialize_bytes(payload: bytes, destination: Path, expected_sha256: str) -> dict:
    """Never overwrite an output; a byte-identical private output is a no-op.

    The digest must come from a trusted channel independent of the fetched bytes.
    Use a new filename for each version and switch the consumer only after success.
    This cannot protect secrets from other processes running as the same OS user.
    """
    if type(payload) is not bytes or len(payload) > 16 * 1024 * 1024:
        raise MaterializationError('invalid or oversized bundle')
    if not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-f]{64}', expected_sha256):
        raise MaterializationError('an independent SHA-256 digest is required')
    actual = hashlib.sha256(payload).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise MaterializationError('bundle digest mismatch')
    path = Path(destination).absolute()
    if '..' in path.parts or not path.name:
        raise MaterializationError('invalid output path')
    parent = None
    temporary = None
    try:
        parent = _parent_fd(path.parent)
        if _existing_matches(parent, path.name, payload):
            return {'status': 'unchanged', 'sha256': actual, 'bytes': len(payload)}
        temporary = '.secret-' + secrets.token_hex(16)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
        except FileExistsError:
            raise MaterializationError('output appeared during write; retry with verification') from None
        os.unlink(temporary, dir_fd=parent)
        temporary = None
        os.fsync(parent)
        return {'status': 'created', 'sha256': actual, 'bytes': len(payload)}
    except OSError:
        raise MaterializationError('private output access failed') from None
    finally:
        if parent is not None:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.close(parent)
