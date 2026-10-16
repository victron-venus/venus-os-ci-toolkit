"""Private regular files and external process I/O; no secret diagnostics."""

import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time
from .catalog import require, MAX_CATALOG


def private_parent(path):
    """Pin private paths beneath trusted, non-renameable directory components."""
    path = Path(path).absolute()
    require(".." not in path.parts and bool(path.name), "invalid private path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            # Root-owned sticky temporary directories protect each owned child
            # from renaming by other users. Other writable ancestors do not.
            require(
                info.st_uid in (0, os.geteuid())
                and (
                    not info.st_mode & 0o022
                    or (info.st_uid == 0 and info.st_mode & stat.S_ISVTX)
                ),
                "unsafe private path ancestor",
            )
        info = os.fstat(fd)
        require(
            info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "private parent must be owned mode 0700",
        )
        return path, fd
    except BaseException:
        os.close(fd)
        raise


def open_private(path):
    path, parent = private_parent(path)
    try:
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "private file must be owned regular mode 0600",
        )
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_private(path, limit=MAX_CATALOG * 2):
    fd = open_private(path)
    try:
        before = os.fstat(fd)
        require(before.st_size <= limit, "private input oversized")
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(fd)

        def fields(st):
            return (
                st.st_dev,
                st.st_ino,
                st.st_size,
                st.st_mtime_ns,
                st.st_ctime_ns,
            )

        require(
            len(data) == before.st_size and fields(before) == fields(after),
            "private input changed",
        )
        return bytes(data)
    finally:
        os.close(fd)


def new_private(path, data):
    path, parent = private_parent(path)
    try:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent,
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(parent)
    finally:
        os.close(parent)


def trusted_tool(path):
    path = Path(path).absolute()
    resolved = path.resolve(strict=True)
    require(
        resolved.is_file() and os.access(resolved, os.X_OK), "tool is not executable"
    )
    for p in (resolved, *resolved.parents):
        info = p.stat()
        require(
            info.st_uid in (0, os.geteuid()) and not info.st_mode & 0o022,
            "unsafe tool ownership or permissions",
        )
    return str(resolved)


def clean_env():
    # Preserve HOME exactly; do not inherit runtime injection, proxy, TF_LOG or TF_CLI_ARGS.
    names = ("HOME", "SYSTEMROOT", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
    result = {k: os.environ[k] for k in names if k in os.environ}
    result["PATH"] = "/usr/bin:/bin"
    result["TF_IN_AUTOMATION"] = "1"
    result["CHECKPOINT_DISABLE"] = "1"
    return result


def process(
    argv,
    data=b"",
    *,
    env=None,
    cwd=None,
    pass_fds=(),
    timeout=120,
    limit=MAX_CATALOG * 2,
):
    """Bound all streams in memory and discard tool diagnostics, including on failure."""
    p = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=clean_env() if env is None else env,
        cwd=cwd,
        pass_fds=pass_fds,
        start_new_session=True,
    )
    sel = selectors.DefaultSelector()
    for stream in (p.stdin, p.stdout, p.stderr):
        os.set_blocking(stream.fileno(), False)
    sel.register(p.stdin, selectors.EVENT_WRITE, "in")
    sel.register(p.stdout, selectors.EVENT_READ, "out")
    sel.register(p.stderr, selectors.EVENT_READ, "err")
    output = bytearray()
    sent = 0
    received = 0
    start = time.monotonic()
    try:
        while sel.get_map():
            require(time.monotonic() - start < timeout, "external tool timeout")
            for key, _ in sel.select(0.1):
                stream = key.fileobj
                if key.data == "in":
                    if sent == len(data):
                        sel.unregister(stream)
                        stream.close()
                        continue
                    try:
                        sent += os.write(stream.fileno(), data[sent : sent + 65536])
                    except BrokenPipeError:
                        sel.unregister(stream)
                        stream.close()
                    except BlockingIOError:
                        continue
                else:
                    try:
                        chunk = os.read(stream.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        sel.unregister(stream)
                        stream.close()
                        continue
                    received += len(chunk)
                    require(received <= limit, "external tool output limit")
                    if key.data == "out":
                        output.extend(chunk)
        try:
            status = p.wait(timeout=max(0.1, timeout - (time.monotonic() - start)))
        except subprocess.TimeoutExpired:
            require(False, "external tool timeout")
        require(status == 0, "external tool failed; output withheld")
        require(sent == len(data), "external tool did not consume its input")
        return bytes(output)
    finally:
        # The leader may have exited while descendants retain its streams.
        # Bound the entire session even when the leader is already reaped.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
        sel.close()
        for stream in (p.stdin, p.stdout, p.stderr):
            if not stream.closed:
                stream.close()


def decrypt(path, identity, age):
    return decrypt_bytes(read_private(path), identity, age)


def decrypt_bytes(encrypted, identity, age):
    """Decrypt already-read ciphertext without reopening or racing its pathname."""
    require(
        type(encrypted) is bytes and len(encrypted) <= MAX_CATALOG * 2,
        "invalid or oversized encrypted input",
    )
    fd = open_private(identity)
    try:
        return process(
            [trusted_tool(age), "--decrypt", "--identity", "/dev/fd/" + str(fd)],
            encrypted,
            pass_fds=(fd,),
        )
    finally:
        os.close(fd)
