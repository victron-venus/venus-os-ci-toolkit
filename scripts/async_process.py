"""Bounded shell-free POSIX process execution for release tooling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
from pathlib import Path
import selectors
import signal
import shutil
import stat
import subprocess  # nosec B404: fixed absolute argv, shell-free bounded runner only
import sys
import threading
import time
from typing import Mapping, Sequence

MAX_OUTPUT_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
CLEANUP_TIMEOUT_SECONDS = 5
POSIX_PROCESS_RUNNER = os.name == "posix"
POSIX_ONLY_MESSAGE = (
    "Release process runner supports Linux Python 3.12+ and macOS Python 3.13+ only"
)


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
    input_offset: int = 0
    output_bytes: int = 0
    failure: ProcessTimeout | ProcessOutputLimit | _ProcessCancelled | None = None
    cleanup_deadline: float | None = None
    killed: bool = False


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


def _kill_owned_process(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        if not _process_has_exited(process):
            try:
                process.kill()
            except ProcessLookupError:
                pass
    except PermissionError:
        # Fail closed below if descendants still hold capture pipes; always kill
        # the owned leader. It remains unreaped here, so its PID cannot be reused.
        try:
            process.kill()
        except ProcessLookupError:
            pass


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
        pass
    try:
        stream.close()  # type: ignore[attr-defined]
    except OSError:
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
) -> None:
    """Register nonblocking capture pipes and optional input under the selector."""
    if process.stdout is None or process.stderr is None:
        _kill_owned_process(process)
        try:
            process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise ProcessCleanupError("Release helper process could not be reaped") from error
        raise ProcessCleanupError("Release helper capture pipes were not created")

    for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)

    stdin_stream = process.stdin
    input_view = memoryview(input_bytes or b"")
    if stdin_stream is not None:
        os.set_blocking(stdin_stream.fileno(), False)
        if input_view:
            selector.register(stdin_stream, selectors.EVENT_WRITE, "stdin")
        else:
            _close_registered_stream(selector, stdin_stream)


def _advance_owned_process_failure(state: _OwnedProcessState, now: float) -> None:
    """Record cancellation/deadline and begin bounded process-group cleanup."""
    if state.failure is None and state.cancel_event is not None and state.cancel_event.is_set():
        state.failure = _ProcessCancelled()
    if state.failure is None and state.deadline is not None and now >= state.deadline:
        state.failure = ProcessTimeout(
            state.timeout or 0,
            _format_output(bytes(state.captured["stdout"]), state.text),
            _format_output(bytes(state.captured["stderr"]), state.text),
        )
    if state.failure is not None and not state.killed:
        _kill_owned_process(state.process)
        state.killed = True
        state.cleanup_deadline = now + CLEANUP_TIMEOUT_SECONDS
    if state.cleanup_deadline is not None and now >= state.cleanup_deadline:
        _kill_owned_process(state.process)
        for stream in state.opened_streams:
            _close_registered_stream(state.selector, stream)
        _wait_owned_process(state.process)
        raise ProcessCleanupError("Release helper output pipes did not reach EOF")


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


def _handle_owned_process_events(
    state: _OwnedProcessState,
    events: Sequence[tuple[selectors.SelectorKey, int]],
) -> None:
    for key, _mask in events:
        stream = key.fileobj
        if key.data == "stdin":
            _write_owned_process_input(state, stream)
            continue
        _read_owned_process_output(state, stream, key.data)


def _complete_owned_process(state: _OwnedProcessState) -> ProcessResult:
    returncode = _wait_owned_process(state.process)
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
    if state.deadline is not None and time.monotonic() >= state.deadline:
        raise ProcessTimeout(
            state.timeout or 0,
            _format_output(bytes(state.captured["stdout"]), state.text),
            _format_output(bytes(state.captured["stderr"]), state.text),
        )
    return ProcessResult(
        state.command,
        returncode,
        _format_output(bytes(state.captured["stdout"]), state.text),
        _format_output(bytes(state.captured["stderr"]), state.text),
    )


def _drive_owned_process(state: _OwnedProcessState) -> ProcessResult:
    while state.selector.get_map() or not _process_has_exited(state.process):
        now = time.monotonic()
        _advance_owned_process_failure(state, now)
        wait_time = _owned_process_wait_time(state, now)
        if state.selector.get_map():
            events = state.selector.select(wait_time)
        else:
            time.sleep(wait_time)
            events = []
        _handle_owned_process_events(state, events)
    return _complete_owned_process(state)


def _reap_owned_process_after_error(process: subprocess.Popen[bytes]) -> None:
    _kill_owned_process(process)
    try:
        process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as cleanup_error:
        raise ProcessCleanupError("Release helper process could not be reaped") from cleanup_error


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
    process = subprocess.Popen(  # nosec B603: validated absolute argv, shell=False, bounded pipes
        command,
        stdin=subprocess.PIPE if input_bytes is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=dict(env) if env is not None else None,
        shell=False,
        bufsize=0,
        close_fds=True,
        start_new_session=True,
    )
    # Own every pipe immediately after Popen, before selector or fd setup can
    # fail. Cleanup must close even streams that were never registered.
    opened_streams = [
        stream for stream in (process.stdin, process.stdout, process.stderr)
        if stream is not None
    ]
    selector: selectors.BaseSelector | None = None
    try:
        selector = selectors.DefaultSelector()
        _register_owned_process_streams(process, selector, input_bytes)
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
        )
        return _drive_owned_process(state)
    except (ProcessTimeout, ProcessOutputLimit, _ProcessCancelled):
        raise
    except ProcessCleanupError:
        raise
    except BaseException:
        _reap_owned_process_after_error(process)
        raise
    finally:
        if selector is not None:
            selector.close()
        for stream in opened_streams:
            try:
                stream.close()
            except OSError:
                pass


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
    # Keep a strong reference to the worker: cancellation must not abandon a child.
    worker = asyncio.create_task(asyncio.to_thread(
        _run_owned_process,
        command,
        env=env,
        cwd=cwd,
        input_bytes=input_bytes,
        max_output_bytes=max_output_bytes,
        text=text,
        timeout=None,
        cancel_event=cancel_event,
    ))
    try:
        result = await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        # Repeated cancellation must not abandon the worker while it still owns a PID.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except _ProcessCancelled:
                break
        try:
            worker.result()
        except _ProcessCancelled:
            pass
        # Re-raise the caller's cancellation only after cleanup is confirmed. A
        # cleanup failure from worker.result() intentionally takes precedence.
        raise
    return result


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
