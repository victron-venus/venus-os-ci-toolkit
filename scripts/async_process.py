"""Bounded shell-free POSIX process execution for release tooling."""

from __future__ import annotations

import asyncio
import contextvars
from contextlib import suppress
from dataclasses import dataclass, field as dataclass_field
from functools import partial
import json
import math
import os
from pathlib import Path
import selectors
import signal
import shutil
import struct
import stat
import subprocess  # nosec B404: fixed absolute argv, shell-free bounded runner only
import sys
import threading
import time
from typing import BinaryIO, Mapping, Sequence

MAX_OUTPUT_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
CLEANUP_TIMEOUT_SECONDS = 5
POSIX_PROCESS_RUNNER = os.name == "posix"
POSIX_ONLY_MESSAGE = (
    "Release process runner supports Linux Python 3.12+ and macOS Python 3.13+ only"
)
SUPERVISOR_STATUS_SIZE = 9
SUPERVISOR_MALFORMED_STATUS_MESSAGE = "Release helper supervisor status frame was malformed"
SUPERVISOR_CONFIG_LIMIT = 1024 * 1024

_SUPERVISOR_SOURCE = r'''import json, os, select, signal, struct, subprocess, sys

config_fd, status_fd, control_fd = map(int, sys.argv[1:4])
status_size = 9

def read_exact(fd, size):
    data = bytearray()
    while len(data) < size:
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise ValueError("incomplete supervisor configuration")
        data.extend(chunk)
    return bytes(data)

def write_exact(fd, data):
    offset = 0
    while offset < len(data):
        offset += os.write(fd, data[offset:])

def publish(magic, value):
    write_exact(status_fd, struct.pack("!4sBi", magic, 1, value))

def wait_for_child(child):
    while True:
        try:
            return child.wait(timeout=0.05)
        except subprocess.TimeoutExpired:
            readable, _, _ = select.select([control_fd], [], [], 0)
            if readable and not os.read(control_fd, 1):
                try:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
                except OSError:
                    os._exit(125)
                os._exit(125)

try:
    header = read_exact(config_fd, 4)
    size = struct.unpack("!I", header)[0]
    if size <= 0 or size > 1048576:
        raise ValueError("invalid supervisor configuration size")
    config = json.loads(read_exact(config_fd, size))
    if type(config) is not dict or set(config) != {"argv", "env", "cwd"}:
        raise ValueError("invalid supervisor configuration")
    argv = config["argv"]
    if type(argv) is not list or not argv or not all(type(arg) is str and arg for arg in argv):
        raise ValueError("invalid supervisor command")
    if not os.path.isabs(argv[0]):
        raise ValueError("invalid supervisor executable")
    target_env = config["env"]
    if type(target_env) is not dict or not all(
        type(key) is str and type(value) is str and key and "=" not in key
        and "\x00" not in key and "\x00" not in value
        for key, value in target_env.items()
    ):
        raise ValueError("invalid supervisor environment")
    target_cwd = config["cwd"]
    if type(target_cwd) is not str or not os.path.isabs(target_cwd):
        raise ValueError("invalid supervisor working directory")
    child = subprocess.Popen(
        argv, env=target_env, cwd=target_cwd, close_fds=True, shell=False,
    )
    for descriptor in (0, 1, 2):
        try:
            os.close(descriptor)
        except OSError:
            pass
    publish(b"IVSP", wait_for_child(child))
except OSError as error:
    try:
        publish(b"IVSE", error.errno if type(error.errno) is int and error.errno > 0 else 5)
    except OSError:
        pass
except BaseException:
    try:
        publish(b"IVSE", 22)
    except OSError:
        pass
finally:
    try:
        os.close(status_fd)
    except OSError:
        pass
    try:
        os.close(config_fd)
    except OSError:
        pass

try:
    # The parent SIGKILLs this still-live group as soon as it validates status.
    # If the parent disappears first, EOF triggers cleanup while the wrapper
    # still pins the process-group identity.
    os.read(control_fd, 1)
except OSError:
    pass
try:
    os.killpg(os.getpgrp(), signal.SIGKILL)
except OSError:
    os._exit(125)
os._exit(125)
'''


class ProcessTimeout(TimeoutError):
    """A child process exceeded its caller-supplied deadline."""

    def __init__(self, timeout: float, stdout: bytes | str, stderr: bytes | str):
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr
        super().__init__("Release helper timed out")


class ProcessOutputLimit(ValueError):
    """A child process exceeded the bounded captured-output limit."""


class ProcessExecutionError(RuntimeError):
    """A checked child process exited unsuccessfully."""

    def __init__(self, returncode: int):
        self.returncode = returncode
        super().__init__(f"Release helper exited with status {returncode}")


class ProcessCleanupError(RuntimeError):
    """A terminated process or its captured pipes could not be reaped promptly."""


class _ProcessCancelled(Exception):
    """Internal cooperative cancellation after the owned process was reaped."""


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes | str
    stderr: bytes | str


@dataclass
class _OwnedProcessState:
    """Mutable I/O and termination state for one already-owned process."""

    process: subprocess.Popen[bytes]
    command: tuple[str, ...]
    selector: selectors.BaseSelector
    opened_streams: list[object]
    captured: dict[str, bytearray]
    input_view: memoryview
    max_output_bytes: int
    text: bool
    timeout: float | None
    deadline: float | None
    cancel_event: threading.Event | None
    status_stream: BinaryIO | None = None
    control_fd: int = -1
    status_buffer: bytearray = dataclass_field(default_factory=bytearray)
    input_offset: int = 0
    output_bytes: int = 0
    target_returncode: int | None = None
    supervisor_signaled: bool = False
    failure: ProcessTimeout | ProcessOutputLimit | _ProcessCancelled | ProcessCleanupError | OSError | None = None
    cleanup_deadline: float | None = None
    killed: bool = False


@dataclass
class _SupervisorLaunch:
    """Parent-side pipes for one live group leader supervising a target argv."""

    process: subprocess.Popen[bytes]
    status_stream: BinaryIO
    control_fd: int


def resolve_executable(name: str) -> str:
    """Resolve a PATH command once and return its absolute regular executable."""
    located = shutil.which(name)
    if located is None:
        raise FileNotFoundError(f"Required executable is unavailable: {name}")
    resolved = Path(located).resolve(strict=True)
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise ValueError("Resolved command is not a regular executable")
    return str(resolved)


def _validate_process_request(argv: Sequence[str], max_output_bytes: int) -> tuple[str, ...]:
    command = tuple(argv)
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("Process argv must contain non-empty strings")
    if not Path(command[0]).is_absolute():
        raise ValueError("Process executable must be an absolute path")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("Process output limit must be a positive integer")
    return command


def _write_supervisor_config(
    stream_fd: int, command: tuple[str, ...], target_env: Mapping[str, str], target_cwd: str,
) -> None:
    """Send bounded target argv/env/cwd over a private pipe, not wrapper metadata."""
    payload = json.dumps(
        {"argv": command, "env": dict(target_env), "cwd": target_cwd},
        separators=(",", ":"),
    ).encode("utf-8")
    if not payload or len(payload) > SUPERVISOR_CONFIG_LIMIT:
        raise ValueError("Release helper command metadata exceeds its private frame limit")
    frame = struct.pack("!I", len(payload)) + payload
    os.set_blocking(stream_fd, False)
    selector = selectors.DefaultSelector()
    try:
        selector.register(stream_fd, selectors.EVENT_WRITE)
        offset = 0
        deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
        while offset < len(frame):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ProcessCleanupError("Release helper supervisor did not accept command metadata")
            try:
                offset += os.write(stream_fd, frame[offset:])
            except BlockingIOError:
                continue
    finally:
        selector.close()


def _decode_supervisor_status(frame: bytes) -> tuple[str, int]:
    """Validate one private fixed-width target completion or spawn-error frame."""
    if len(frame) != SUPERVISOR_STATUS_SIZE:
        raise ProcessCleanupError(SUPERVISOR_MALFORMED_STATUS_MESSAGE)
    try:
        magic, version, value = struct.unpack("!4sBi", frame)
    except struct.error as error:
        raise ProcessCleanupError(SUPERVISOR_MALFORMED_STATUS_MESSAGE) from error
    if version != 1 or magic not in {b"IVSP", b"IVSE"}:
        raise ProcessCleanupError(SUPERVISOR_MALFORMED_STATUS_MESSAGE)
    if magic == b"IVSE":
        return "error", value if value > 0 else 5
    return "exit", value


def _close_supervisor_fd(descriptor: int) -> None:
    if descriptor >= 0:
        with suppress(OSError):
            os.close(descriptor)


def _close_supervisor_stream(stream: object | None) -> None:
    if stream is not None:
        with suppress(OSError):
            stream.close()  # type: ignore[attr-defined]


def _cleanup_failed_supervisor_launch(
    process: subprocess.Popen[bytes] | None,
    status_stream: BinaryIO | None,
    descriptors: Sequence[int],
) -> Exception | None:
    """Close partial launch state and reap a wrapper if Popen succeeded."""
    _close_supervisor_stream(status_stream)
    for descriptor in descriptors:
        _close_supervisor_fd(descriptor)
    if process is None:
        return None
    cleanup_error = None
    try:
        _reap_owned_process_after_error(process, control_fd=-1)
    except Exception as error:
        cleanup_error = error
    for stream in (process.stdin, process.stdout, process.stderr):
        _close_supervisor_stream(stream)
    return cleanup_error


def _launch_supervised_process(
    command: tuple[str, ...], *, env: Mapping[str, str] | None,
    cwd: str | Path | None, input_enabled: bool,
) -> _SupervisorLaunch:
    """Start a sanitized live wrapper; pass request metadata only over its private pipe."""
    target_environment = dict(os.environ if env is None else env)
    if not all(isinstance(key, str) and isinstance(value, str)
               for key, value in target_environment.items()):
        raise ValueError("Release helper environment must contain strings")
    target_directory = os.path.realpath(os.path.abspath(os.fsdecode(cwd))) if cwd is not None else os.getcwd()
    config_read, config_write = os.pipe()
    status_read, status_write = os.pipe()
    control_read, control_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    status_stream: BinaryIO | None = None
    try:
        for descriptor in (config_read, status_write, control_read):
            os.set_inheritable(descriptor, True)
        supervisor_command = (
            sys.executable, "-I", "-c", _SUPERVISOR_SOURCE,
            str(config_read), str(status_write), str(control_read),
        )
        wrapper_environment = {"PATH": os.defpath, "LANG": "C", "LC_ALL": "C"}
        process = subprocess.Popen(  # nosec B603: fixed internal wrapper, request data stays on private pipe
            supervisor_command,
            stdin=subprocess.PIPE if input_enabled else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            env=wrapper_environment,
            shell=False,
            bufsize=0,
            close_fds=True,
            pass_fds=(config_read, status_write, control_read),
            start_new_session=True,
        )
        os.close(config_read)
        config_read = -1
        os.close(status_write)
        status_write = -1
        os.close(control_read)
        control_read = -1
        status_stream = os.fdopen(status_read, "rb", buffering=0)
        status_read = -1
        _write_supervisor_config(
            config_write, command, target_environment, target_directory,
        )
        os.close(config_write)
        config_write = -1
        return _SupervisorLaunch(process, status_stream, control_write)
    except BaseException as launch_error:
        cleanup_error = _cleanup_failed_supervisor_launch(
            process, status_stream,
            (config_read, config_write, status_read, status_write, control_read, control_write),
        )
        if cleanup_error is not None:
            raise cleanup_error from launch_error
        raise


def _kill_live_owned_leader(
    process: subprocess.Popen[bytes], leader_exited: bool,
) -> None:
    """Best-effort direct signal only while WNOWAIT still proves ownership."""
    if leader_exited or _process_has_exited(process):
        return
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        # The caller still performs bounded reap and reports any group failure.
        pass


def _kill_owned_process(process: subprocess.Popen[bytes]) -> OSError | None:
    """Signal an owned group without using a possibly recycled leader PID."""
    # WNOWAIT keeps an exited leader's PID reserved until the process group is gone.
    leader_exited = _process_has_exited(process)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        # An absent group is complete; direct fallback is safe only for a live leader.
        _kill_live_owned_leader(process, leader_exited)
        return None
    except OSError as error:
        # Failed group signaling is unverified; a PGID-only listing cannot prove
        # that same-session descendants are absent.
        _kill_live_owned_leader(process, leader_exited)
        return error
    return None


def _reap_after_group_signal_error(
    process: subprocess.Popen[bytes], error: OSError,
) -> None:
    """Reap the leader, then fail closed because group cleanup was not confirmed."""
    try:
        process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        raise ProcessCleanupError(
            "Release helper process-group termination failed and leader could not be reaped"
        ) from error
    raise ProcessCleanupError(
        "Release helper process-group termination failed; descendants may still be running"
    ) from error


def _require_nonreaping_wait_support() -> None:
    """Require waitid(WNOWAIT) before launch so the process-group ID stays owned."""
    if not callable(getattr(os, "waitid", None)) or not all(
        hasattr(os, name) for name in ("P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
    ):
        raise NotImplementedError("Release process cleanup requires POSIX waitid(WNOWAIT)")


def _runtime_supported(platform: str, version: tuple[int, ...], os_name: str) -> bool:
    if os_name != "posix":
        return False
    if platform.startswith("linux"):
        return version >= (3, 12)
    if platform == "darwin":
        return version >= (3, 13)
    return False


def _require_supported_runtime() -> None:
    if not POSIX_PROCESS_RUNNER or not _runtime_supported(
        sys.platform, sys.version_info[:2], os.name,
    ):
        raise NotImplementedError(POSIX_ONLY_MESSAGE)
    _require_nonreaping_wait_support()


def _process_has_exited(process: subprocess.Popen[bytes]) -> bool:
    """Observe leader exit without reaping it or making its process-group ID reusable."""
    try:
        observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError as error:
        raise ProcessCleanupError("Release helper leader was reaped outside its owner") from error
    return observed is not None


def _close_registered_stream(
    selector: selectors.BaseSelector,
    stream: object,
) -> None:
    try:
        selector.unregister(stream)
    except (KeyError, ValueError):
        # EOF/error paths can close a stream after it was already unregistered.
        pass
    try:
        stream.close()  # type: ignore[attr-defined]
    except OSError:
        # Cleanup is best-effort when the stream was closed by an earlier path.
        pass


def _wait_owned_process(process: subprocess.Popen[bytes]) -> int:
    try:
        return process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise ProcessCleanupError("Release helper process could not be reaped promptly") from error


def _register_owned_process_streams(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    input_bytes: bytes | None,
    status_stream: BinaryIO,
) -> None:
    """Register nonblocking capture pipes and optional input under the selector."""
    if process.stdout is None or process.stderr is None:
        group_error = _kill_owned_process(process)
        if group_error is not None:
            _reap_after_group_signal_error(process, group_error)
        try:
            process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise ProcessCleanupError("Release helper process could not be reaped") from error
        raise ProcessCleanupError("Release helper capture pipes were not created")

    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    os.set_blocking(status_stream.fileno(), False)
    selector.register(status_stream, selectors.EVENT_READ, "status")

    stdin_stream = process.stdin
    input_view = memoryview(input_bytes or b"")
    if stdin_stream is not None:
        os.set_blocking(stdin_stream.fileno(), False)
        if input_view:
            selector.register(stdin_stream, selectors.EVENT_WRITE, "stdin")
        else:
            _close_registered_stream(selector, stdin_stream)


def _record_owned_process_failure(state: _OwnedProcessState, now: float) -> None:
    if state.failure is not None:
        return
    if state.cancel_event is not None and state.cancel_event.is_set():
        state.failure = _ProcessCancelled()
    elif state.deadline is not None and now >= state.deadline:
        state.failure = ProcessTimeout(
            state.timeout or 0,
            _format_output(bytes(state.captured["stdout"]), state.text),
            _format_output(bytes(state.captured["stderr"]), state.text),
        )


def _start_owned_process_cleanup(state: _OwnedProcessState, now: float) -> None:
    group_error = _kill_owned_process(state.process)
    if group_error is not None:
        _close_supervisor_control(state)
        _reap_after_group_signal_error(state.process, group_error)
    _close_supervisor_control(state)
    state.killed = True
    state.cleanup_deadline = now + CLEANUP_TIMEOUT_SECONDS


def _expire_owned_process_cleanup(state: _OwnedProcessState) -> None:
    if not state.killed:
        _start_owned_process_cleanup(state, time.monotonic())
    for stream in state.opened_streams:
        _close_registered_stream(state.selector, stream)
    _wait_owned_process(state.process)
    raise ProcessCleanupError("Release helper output pipes did not reach EOF")


def _advance_owned_process_failure(state: _OwnedProcessState, now: float) -> None:
    """Record cancellation/deadline and begin bounded process-group cleanup."""
    _record_owned_process_failure(state, now)
    if state.failure is not None and not state.killed:
        _start_owned_process_cleanup(state, now)
    if state.cleanup_deadline is not None and now >= state.cleanup_deadline:
        _expire_owned_process_cleanup(state)


def _owned_process_wait_time(state: _OwnedProcessState, now: float) -> float:
    waits = [0.05]
    if state.failure is None and state.deadline is not None:
        waits.append(max(0.0, state.deadline - now))
    if state.cleanup_deadline is not None:
        waits.append(max(0.0, state.cleanup_deadline - now))
    return min(waits)


def _write_owned_process_input(state: _OwnedProcessState, stream: object) -> None:
    try:
        written = os.write(stream.fileno(), state.input_view[state.input_offset:])  # type: ignore[attr-defined]
    except (BrokenPipeError, ConnectionResetError):
        _close_registered_stream(state.selector, stream)
        return
    state.input_offset += written
    if state.input_offset >= len(state.input_view):
        _close_registered_stream(state.selector, stream)


def _read_owned_process_output(state: _OwnedProcessState, stream: object, name: str) -> None:
    try:
        chunk = os.read(stream.fileno(), READ_CHUNK_BYTES)  # type: ignore[attr-defined]
    except BlockingIOError:
        return
    if not chunk:
        _close_registered_stream(state.selector, stream)
        return
    if state.output_bytes + len(chunk) > state.max_output_bytes:
        if state.failure is None:
            state.failure = ProcessOutputLimit(
                "Release helper output exceeded its capture limit"
            )
        return
    if state.failure is not None and not isinstance(state.failure, ProcessTimeout):
        return
    state.output_bytes += len(chunk)
    state.captured[name].extend(chunk)


def _close_supervisor_control(state: _OwnedProcessState) -> None:
    if state.control_fd >= 0:
        with suppress(OSError):
            os.close(state.control_fd)
        state.control_fd = -1


def _accept_owned_process_status(state: _OwnedProcessState, stream: BinaryIO) -> None:
    try:
        status, value = _decode_supervisor_status(bytes(state.status_buffer))
    except ProcessCleanupError as error:
        if state.failure is None:
            state.failure = error
        _close_registered_stream(state.selector, stream)
        return
    if status == "error":
        if state.failure is None:
            state.failure = OSError(value, "Release helper target could not be started")
    else:
        state.target_returncode = value
        # Status is the cleanup boundary: kill the live wrapper group before
        # draining output, since descendants may keep the capture pipes open.
        group_error = _kill_owned_process(state.process)
        if group_error is not None:
            _close_supervisor_control(state)
            _reap_after_group_signal_error(state.process, group_error)
        _close_supervisor_control(state)
        state.supervisor_signaled = True
        state.killed = True
        state.cleanup_deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
    _close_registered_stream(state.selector, stream)


def _read_owned_process_status(state: _OwnedProcessState, stream: BinaryIO) -> None:
    try:
        chunk = os.read(stream.fileno(), SUPERVISOR_STATUS_SIZE + 1)
    except BlockingIOError:
        return
    if not chunk:
        if state.failure is None:
            message = "Release helper supervisor exited without a complete status frame"
            state.failure = ProcessCleanupError(message)
        _close_registered_stream(state.selector, stream)
        return
    state.status_buffer.extend(chunk)
    if len(state.status_buffer) > SUPERVISOR_STATUS_SIZE:
        if state.failure is None:
            state.failure = ProcessCleanupError("Release helper supervisor status frame was oversized")
        _close_registered_stream(state.selector, stream)
        return
    if len(state.status_buffer) < SUPERVISOR_STATUS_SIZE:
        return
    _accept_owned_process_status(state, stream)


def _handle_owned_process_events(
    state: _OwnedProcessState,
    events: Sequence[tuple[selectors.SelectorKey, int]],
) -> None:
    for key, _mask in events:
        stream = key.fileobj
        if key.data == "stdin":
            _write_owned_process_input(state, stream)
            continue
        if key.data == "status":
            _read_owned_process_status(state, stream)
            continue
        _read_owned_process_output(state, stream, key.data)


def _record_completion_failure(state: _OwnedProcessState) -> None:
    """Remember cancellation or deadline observed around process-group cleanup."""
    if state.failure is not None:
        return
    if state.cancel_event is not None and state.cancel_event.is_set():
        state.failure = _ProcessCancelled()
    elif state.deadline is not None and time.monotonic() >= state.deadline:
        state.failure = ProcessTimeout(
            state.timeout or 0,
            _format_output(bytes(state.captured["stdout"]), state.text),
            _format_output(bytes(state.captured["stderr"]), state.text),
        )


def _raise_owned_process_failure(state: _OwnedProcessState) -> None:
    """Raise a captured process failure after its leader and group are reaped."""
    if isinstance(state.failure, ProcessTimeout):
        raise ProcessTimeout(
            state.failure.timeout,
            _format_output(bytes(state.captured["stdout"]), state.text),
            _format_output(bytes(state.captured["stderr"]), state.text),
        )
    if isinstance(state.failure, ProcessOutputLimit):
        raise state.failure
    if isinstance(state.failure, _ProcessCancelled):
        raise state.failure
    if isinstance(state.failure, ProcessCleanupError):
        raise state.failure
    if isinstance(state.failure, OSError):
        raise state.failure
    if state.deadline is not None and time.monotonic() >= state.deadline:
        raise ProcessTimeout(
            state.timeout or 0,
            _format_output(bytes(state.captured["stdout"]), state.text),
            _format_output(bytes(state.captured["stderr"]), state.text),
        )


def _complete_owned_process(state: _OwnedProcessState) -> ProcessResult:
    # The loop reaches completion only after the supervisor signaled its group,
    # all captured pipes reached EOF, and the supervisor leader exited.
    _record_completion_failure(state)
    returncode = _wait_owned_process(state.process)
    _raise_owned_process_failure(state)
    if state.target_returncode is None or not state.supervisor_signaled:
        raise ProcessCleanupError("Release helper supervisor completion was not confirmed")
    if returncode != -signal.SIGKILL:
        raise ProcessCleanupError("Release helper supervisor did not exit from confirmed group cleanup")
    return ProcessResult(
        state.command,
        state.target_returncode,
        _format_output(bytes(state.captured["stdout"]), state.text),
        _format_output(bytes(state.captured["stderr"]), state.text),
    )


def _drive_owned_process(state: _OwnedProcessState) -> ProcessResult:
    while True:
        now = time.monotonic()
        _advance_owned_process_failure(state, now)
        if not state.selector.get_map():
            if _process_has_exited(state.process):
                return _complete_owned_process(state)
            if state.cleanup_deadline is not None:
                _advance_owned_process_failure(state, now)
            time.sleep(_owned_process_wait_time(state, now))
            continue
        wait_time = _owned_process_wait_time(state, now)
        if state.selector.get_map():
            events = state.selector.select(wait_time)
        else:
            time.sleep(wait_time)
            events = []
        _handle_owned_process_events(state, events)


def _reap_owned_process_after_error(
    process: subprocess.Popen[bytes], *, control_fd: int,
) -> None:
    group_error = _kill_owned_process(process)
    if group_error is not None:
        if control_fd >= 0:
            with suppress(OSError):
                os.close(control_fd)
        _reap_after_group_signal_error(process, group_error)
    if control_fd >= 0:
        with suppress(OSError):
            os.close(control_fd)
    try:
        process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as cleanup_error:
        raise ProcessCleanupError("Release helper process could not be reaped") from cleanup_error


def _abort_owned_process_run(
    process: subprocess.Popen[bytes],
    state: _OwnedProcessState | None,
    control_fd: int,
) -> None:
    if process.returncode is not None:
        return
    if state is None:
        _reap_owned_process_after_error(process, control_fd=control_fd)
        return
    _close_supervisor_control(state)
    _reap_owned_process_after_error(process, control_fd=-1)


def _close_owned_process_run(
    selector: selectors.BaseSelector | None,
    opened_streams: Sequence[BinaryIO],
    state: _OwnedProcessState | None,
) -> None:
    if selector is not None:
        selector.close()
    for stream in opened_streams:
        _close_supervisor_stream(stream)
    if state is not None:
        _close_supervisor_control(state)


def _execute_owned_process_launch(
    launch: _SupervisorLaunch,
    command: tuple[str, ...],
    *,
    input_bytes: bytes | None,
    max_output_bytes: int,
    text: bool,
    timeout: float | None,
    cancel_event: threading.Event | None,
) -> ProcessResult:
    process = launch.process
    opened_streams = [stream for stream in (
        process.stdin, process.stdout, process.stderr, launch.status_stream,
    ) if stream is not None]
    selector: selectors.BaseSelector | None = None
    state: _OwnedProcessState | None = None
    try:
        selector = selectors.DefaultSelector()
        _register_owned_process_streams(process, selector, input_bytes, launch.status_stream)
        state = _OwnedProcessState(
            process=process,
            command=command,
            selector=selector,
            opened_streams=opened_streams,
            captured={"stdout": bytearray(), "stderr": bytearray()},
            input_view=memoryview(input_bytes or b""),
            max_output_bytes=max_output_bytes,
            text=text,
            timeout=timeout,
            deadline=time.monotonic() + timeout if timeout is not None else None,
            cancel_event=cancel_event,
            status_stream=launch.status_stream,
            control_fd=launch.control_fd,
            status_buffer=bytearray(),
        )
        return _drive_owned_process(state)
    except (ProcessTimeout, ProcessOutputLimit, _ProcessCancelled, ProcessCleanupError):
        raise
    except BaseException:
        _abort_owned_process_run(process, state, launch.control_fd)
        raise
    finally:
        _close_owned_process_run(selector, opened_streams, state)


def _run_owned_process(
    command: tuple[str, ...],
    *,
    env: Mapping[str, str] | None,
    cwd: str | Path | None,
    input_bytes: bytes | None,
    max_output_bytes: int,
    text: bool,
    timeout: float | None,
    cancel_event: threading.Event | None = None,
) -> ProcessResult:
    """Run a POSIX child whose PID is owned before any timeout can arrive."""
    _require_supported_runtime()
    if cancel_event is not None and cancel_event.is_set():
        raise _ProcessCancelled()
    launch = _launch_supervised_process(
        command, env=env, cwd=cwd, input_enabled=input_bytes is not None,
    )
    # Own every pipe before selector setup, then cleanup always sees even streams
    # that registration never reached.
    return _execute_owned_process_launch(
        launch, command, input_bytes=input_bytes, max_output_bytes=max_output_bytes,
        text=text, timeout=timeout, cancel_event=cancel_event,
    )


async def run_process_async(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    input_bytes: bytes | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    text: bool = False,
) -> ProcessResult:
    """Run argv with bounded capture; cancellation reaps the owned POSIX process."""
    _require_supported_runtime()
    command = _validate_process_request(argv, max_output_bytes)
    cancel_event = threading.Event()
    # Keep the executor Future itself, rather than a Task wrapping to_thread.
    # asyncio.run cancels every Task during shutdown; cancellation of that wrapper
    # would otherwise look complete before its thread has reaped the child.
    loop = asyncio.get_running_loop()
    context = contextvars.copy_context()
    operation = partial(
        _run_owned_process,
        command,
        env=env,
        cwd=cwd,
        input_bytes=input_bytes,
        max_output_bytes=max_output_bytes,
        text=text,
        timeout=None,
        cancel_event=cancel_event,
    )
    worker = loop.run_in_executor(None, context.run, operation)
    try:
        # `wait()` does not propagate caller cancellation to the executor
        # Future. Unlike `shield()`, it also does not attach a logging callback
        # that treats a worker exception after cancellation as unobserved.
        await asyncio.wait({worker})
    except asyncio.CancelledError:
        cancel_event.set()
        await _drain_cancelled_worker(worker)
        # Re-raise the caller's cancellation only after cleanup is confirmed. A
        # cleanup failure from worker.result() intentionally takes precedence.
        raise
    return worker.result()


async def _drain_cancelled_worker(worker: asyncio.Future[ProcessResult]) -> None:
    """Wait through repeated cancellation and propagate any real cleanup failure."""
    while not worker.done():
        with suppress(asyncio.CancelledError):
            await asyncio.wait({worker})
    try:
        worker.result()
    except (ProcessOutputLimit, _ProcessCancelled):
        # Both are expected worker outcomes after caller cancellation completed cleanup.
        pass


def _format_output(output: bytes, text: bool) -> bytes | str:
    return output.decode(errors="replace") if text else output


def run_process(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    timeout: float,
    check: bool = True,
    input: bytes | None = None,
    text: bool = False,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> ProcessResult:
    """Synchronous entry point for release helpers on Linux/macOS."""
    _require_supported_runtime()
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Process timeout must be positive")
    command = _validate_process_request(argv, max_output_bytes)
    result = _run_owned_process(
        command,
        env=env,
        cwd=cwd,
        input_bytes=input,
        max_output_bytes=max_output_bytes,
        text=text,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise ProcessExecutionError(result.returncode)
    return result
