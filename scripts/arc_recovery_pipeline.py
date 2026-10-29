"""Bounded POSIX stream sink for a reviewed, ordinary-tools recovery pipeline.

This module supports a separately reviewed site operator. It does not select inputs, credentials, commands,
or resource limits. A separate approved systemd unit supplies CPU/memory limits.
No real archive pipeline is started by importing this module.
"""

import hashlib
import json
import os
import re
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time

BLOCK = 64 * 1024
MAX_METADATA = 256 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def positive(value):
    return type(value) is int and value > 0


def child_status(process):
    """Observe, but do not reap, the leader that reserves our process-group ID."""
    require(process.returncode is None, "pipeline child ownership lost")
    try:
        status = os.waitid(os.P_PID, process.pid,
                           os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as error:
        raise ValueError("pipeline child ownership lost") from error
    if status is None:
        return None
    require(status.si_pid == process.pid, "pipeline child identity")
    if status.si_code == os.CLD_EXITED:
        return status.si_status
    require(status.si_code in (os.CLD_KILLED, os.CLD_DUMPED), "pipeline child status")
    return -status.si_status


def terminated_group_only(process, deadline):
    """Darwin can return EPERM for a group containing only zombies."""
    require(child_status(process) is not None, "pipeline group leader still running")
    inspection_deadline = deadline - .1  # Reserve time to reap the inspector itself.
    require(time.monotonic() < inspection_deadline, "pipeline group inspection deadline")
    command = ["/bin/ps", "-g", str(process.pid), "-o", "pid=,pgid=,stat="]
    inspector = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env={"PATH": "/usr/bin:/bin", "LC_ALL": "C",
                                      "COMMAND_MODE": "unix2003"})
    output = [bytearray(), bytearray()]
    try:
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((inspector.stdout, inspector.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            while selector.get_map() or inspector.poll() is None:
                require(time.monotonic() < inspection_deadline, "pipeline group inspection deadline")
                for key, _ in selector.select(min(.05, max(0, inspection_deadline - time.monotonic()))):
                    try:
                        chunk = os.read(key.fd, 4096)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output[key.data].extend(chunk)
                        require(sum(map(len, output)) <= BLOCK, "pipeline group inspection limit")
        require(inspector.returncode == 0 and not output[1], "pipeline group inspection failed")
        members = set()
        for line in output[0].splitlines():
            fields = line.split()
            require(len(fields) == 3 and re.fullmatch(rb"[1-9][0-9]*", fields[0])
                    and fields[1] == str(process.pid).encode()
                    and re.fullmatch(rb"Z[+<>NAELsVWXS]*", fields[2]),
                    "pipeline group has live or unknown members")
            member = int(fields[0])
            require(member not in members, "pipeline group inspection duplicate")
            members.add(member)
        require(process.pid in members, "pipeline group inspection incomplete")
        require(child_status(process) is not None, "pipeline group identity changed")
    finally:
        # ps does not fork; never signal a group or a reaped inspector PID.
        if inspector.poll() is None:
            inspector.kill()
        inspector.wait(timeout=max(.001, deadline - time.monotonic()))
        inspector.stdout.close()
        inspector.stderr.close()


def durable_json(path, value, reserve):
    raw = (json.dumps(value, sort_keys=True) + "\n").encode()
    require(len(raw) <= MAX_METADATA, "metadata byte limit")
    pending = path.with_name(path.name + ".pending")
    require(not path.exists() and not path.is_symlink(), "metadata already exists")
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    linked = False
    try:
        disk_gate(descriptor, len(raw), reserve)
        with pending.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(descriptor)
        disk_gate(descriptor, 0, reserve)
        # A .pending record is never acceptance. Publish only after the write
        # and its post-write checks passed; never replace another final record.
        os.link(pending, path)
        linked = True
        os.fsync(descriptor)
        disk_gate(descriptor, 0, reserve)
    except BaseException:
        if linked and path.exists() and os.path.samestat(path.stat(), pending.stat()):
            path.unlink()
            os.fsync(descriptor)
        raise
    finally:
        os.close(descriptor)


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def disk_gate(descriptor, pending, reserve):
    current = os.fstatvfs(descriptor)
    allocation = ((pending + 4095) // 4096) * 4096 + 8192
    require(current.f_bavail * current.f_frsize >= reserve + allocation, "disk reserve")


def input_proof(inputs, deadline):
    require(type(inputs) is dict and len(inputs) <= 128, "input inventory")
    result = {}
    for name, expected in inputs.items():
        require(type(name) is str and type(expected) is dict
                and set(expected) == {"bytes", "sha256"}, "input metadata")
        require(type(expected["bytes"]) is int and expected["bytes"] >= 0
                and type(expected["sha256"]) is str
                and re.fullmatch("[0-9a-f]{64}", expected["sha256"]), "input identity")
        path = Path(name)
        before = path.lstat()
        require(stat.S_ISREG(before.st_mode), "input is not regular")
        digest = hashlib.sha256()
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            require(identity(os.fstat(stream.fileno())) == identity(before), "input changed")
            require(before.st_size == expected["bytes"], "input size")
            while chunk := stream.read(BLOCK):
                require(time.monotonic() < deadline, "deadline during input verification")
                digest.update(chunk)
            require(identity(os.fstat(stream.fileno())) == identity(before), "input changed")
        require(identity(path.lstat()) == identity(before), "input replaced")
        require(digest.hexdigest() == expected["sha256"], "input digest")
        result[name] = list(identity(before))
    return result


def run(commands, destination, *, maximum, reserve, timeout, inputs,
        expected_bytes=None, expected_sha256=None, stderr_maximum=1024 * 1024):
    """One pipeline, one exclusive output; preserve partial bytes on any failure.

    Callers supply reviewed argv, never shell strings. No retry, source cleanup,
    networking, credential discovery, or implicit overwrite is implemented.
    A final filename alone is not acceptance: a durable passed receipt is also
    required. Promotion can precede a later teardown or receipt-write failure.
    Only the final receipt name and successful overall unit exit count;
    metadata .pending hardlinks are retained diagnostics, never acceptance.
    """
    require(os.name == "posix", "Recovery pipeline requires POSIX")
    require(all(hasattr(os, name) for name in
                ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT",
                 "CLD_EXITED", "CLD_KILLED", "CLD_DUMPED")),
            "Recovery pipeline requires waitid/WNOWAIT (macOS: Python >= 3.13)")
    require(signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL,
            "Recovery pipeline requires the default SIGCHLD handler and sole child reaper")
    require(type(commands) is list and 1 <= len(commands) <= 5, "pipeline length")
    require(all(type(command) is list and command
                and all(type(value) is str and value and "\0" not in value
                        for value in command) for command in commands), "argv")
    require(positive(maximum) and type(reserve) is int and reserve >= 0
            and positive(timeout) and positive(stderr_maximum), "bounds")
    require(expected_bytes is None or (type(expected_bytes) is int
            and 0 <= expected_bytes <= maximum), "expected size")
    require(expected_sha256 is None or (type(expected_sha256) is str
            and re.fullmatch("[0-9a-f]{64}", expected_sha256)), "expected hash")
    destination = Path(destination)
    partial = destination.with_name(destination.name + ".partial")
    intent = destination.with_name(destination.name + ".intent.json")
    receipt = destination.with_name(destination.name + ".receipt.json")
    require(destination.parent.is_dir() and not destination.parent.is_symlink(), "output parent")
    require(all(not path.exists() and not path.is_symlink()
                for path in (destination, partial, intent, receipt)), "output exists")
    start = time.monotonic()
    deadline = start + timeout
    verified = input_proof(inputs, deadline)
    durable_json(intent, {"commands": commands, "inputs": inputs,
                          "maximum": maximum, "reserve": reserve, "timeout": timeout,
                          "cpu_memory_limits": "external approved unit required",
                          "automatic_retry": False}, reserve)
    processes = []
    source = None
    count = 0
    digest = hashlib.sha256()
    stderr_sizes = [0] * len(commands)
    stderr_hashes = [hashlib.sha256() for _ in commands]
    failure = None
    promoted = False
    try:
        with partial.open("xb", buffering=0) as output, selectors.DefaultSelector() as selector:
            os.fchmod(output.fileno(), 0o600)
            disk_gate(output.fileno(), 0, reserve)
            for index, command in enumerate(commands):
                require(time.monotonic() < deadline, "deadline before process launch")
                process = subprocess.Popen(command, stdin=source or subprocess.DEVNULL,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                           start_new_session=True)
                processes.append(process)
                if source is not None:
                    source.close()
                source = process.stdout
                os.set_blocking(process.stderr.fileno(), False)
                selector.register(process.stderr, selectors.EVENT_READ, ("stderr", index))
            os.set_blocking(source.fileno(), False)
            selector.register(source, selectors.EVENT_READ, ("output", -1))
            while selector.get_map() or any(child_status(process) is None for process in processes):
                require(time.monotonic() < deadline, "pipeline deadline")
                disk_gate(output.fileno(), 0, reserve)
                require(not any(child_status(process) not in (None, 0) for process in processes),
                        "pipeline nonzero exit")
                for key, _ in selector.select(min(0.1, max(0, deadline - time.monotonic()))):
                    try:
                        chunk = os.read(key.fd, BLOCK)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    kind, index = key.data
                    if kind == "stderr":
                        stderr_sizes[index] += len(chunk)
                        stderr_hashes[index].update(chunk)
                        require(stderr_sizes[index] <= stderr_maximum, "stderr limit")
                    else:
                        require(count + len(chunk) <= maximum, "output byte limit")
                        if expected_bytes is not None:
                            require(count + len(chunk) <= expected_bytes, "expected byte limit")
                        disk_gate(output.fileno(), len(chunk), reserve)
                        # A file write can be short; account only bytes actually retained.
                        view = memoryview(chunk)
                        while view:
                            written = output.write(view)
                            require(written is not None and written > 0, "short output write")
                            digest.update(view[:written])
                            count += written
                            view = view[written:]
                require(time.monotonic() < deadline, "pipeline deadline")
            require(all(child_status(process) == 0 for process in processes), "pipeline exit")
            require(expected_bytes is None or count == expected_bytes, "truncated output")
            require(expected_sha256 is None or digest.hexdigest() == expected_sha256, "output digest")
            require(all(list(identity(Path(name).lstat())) == value
                        for name, value in verified.items()), "verified input changed")
            disk_gate(output.fileno(), 0, reserve)
            os.fsync(output.fileno())
        os.link(partial, destination)
        promoted = True
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        # The partial hardlink remains until an explicit later cleanup decision.
    except BaseException as error:
        failure = str(error) if type(error) is ValueError else type(error).__name__
    finally:
        # Do not reap any leader until all owned groups have been signaled.
        # Even an exited, waitable leader reserves its PID, so killpg cannot
        # address a reused ID. Descendants may still hold inherited pipes.
        teardown_deadline = time.monotonic() + 2
        for process in processes:
            try:
                child_status(process)
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except PermissionError:
                    if sys.platform != "darwin":
                        raise
                    terminated_group_only(process, teardown_deadline)
            except ProcessLookupError:
                pass
            except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                failure = failure or (str(error) if type(error) is ValueError
                                      else "pipeline group teardown failed")
        for process in processes:
            try:
                process.wait(timeout=max(0.001, teardown_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = failure or "pipeline teardown incomplete"
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        result = {"passed": failure is None, "failure": failure,
                  "promoted": promoted, "bytes": count, "sha256": digest.hexdigest(),
                  "elapsed_seconds": time.monotonic() - start,
                  "returncodes": [process.returncode for process in processes],
                  "stderr_bytes": stderr_sizes,
                  "stderr_sha256": [value.hexdigest() for value in stderr_hashes],
                  "input_identities": verified, "automatic_retry": False,
                  "cpu_memory_limits_verified": False}
        durable_json(receipt, result, reserve)
    return result
