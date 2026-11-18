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
import threading
import time
from typing import Mapping, Sequence

MAX_OUTPUT_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
CLEANUP_TIMEOUT_SECONDS = 5
POSIX_PROCESS_RUNNER = os.name == "posix"
POSIX_ONLY_MESSAGE = "Release process runner supports POSIX systems (Linux/macOS) only"


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
    if not POSIX_PROCESS_RUNNER:
        raise NotImplementedError(POSIX_ONLY_MESSAGE)
    _require_nonreaping_wait_support()
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
    if process.stdout is None or process.stderr is None:
        _kill_owned_process(process)
        try:
            process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            raise ProcessCleanupError("Release helper process could not be reaped") from error
        raise ProcessCleanupError("Release helper capture pipes were not created")

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    output_bytes = 0
    selector = selectors.DefaultSelector()
    output_streams: dict[int, str] = {}
    opened_streams = [process.stdout, process.stderr]
    stdin_stream = process.stdin
    if stdin_stream is not None:
        opened_streams.append(stdin_stream)
    failure: ProcessTimeout | ProcessOutputLimit | _ProcessCancelled | None = None
    cleanup_deadline: float | None = None
    killed = False
    input_view = memoryview(input_bytes or b"")
    input_offset = 0
    try:
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
            output_streams[stream.fileno()] = name
        if stdin_stream is not None:
            os.set_blocking(stdin_stream.fileno(), False)
            if input_view:
                selector.register(stdin_stream, selectors.EVENT_WRITE, "stdin")
            else:
                _close_registered_stream(selector, stdin_stream)
                stdin_stream = None

        deadline = time.monotonic() + timeout if timeout is not None else None
        while selector.get_map() or not _process_has_exited(process):
            now = time.monotonic()
            if failure is None and cancel_event is not None and cancel_event.is_set():
                failure = _ProcessCancelled()
            if failure is None and deadline is not None and now >= deadline:
                failure = ProcessTimeout(
                    timeout or 0,
                    _format_output(bytes(captured["stdout"]), text),
                    _format_output(bytes(captured["stderr"]), text),
                )
            if failure is not None and not killed:
                _kill_owned_process(process)
                killed = True
                cleanup_deadline = now + CLEANUP_TIMEOUT_SECONDS
            if cleanup_deadline is not None and now >= cleanup_deadline:
                _kill_owned_process(process)
                for stream in opened_streams:
                    _close_registered_stream(selector, stream)
                _wait_owned_process(process)
                raise ProcessCleanupError("Release helper output pipes did not reach EOF")

            waits = [0.05]
            if failure is None and deadline is not None:
                waits.append(max(0.0, deadline - now))
            if cleanup_deadline is not None:
                waits.append(max(0.0, cleanup_deadline - now))
            events = selector.select(min(waits)) if selector.get_map() else []
            for key, _mask in events:
                stream = key.fileobj
                if key.data == "stdin":
                    try:
                        written = os.write(stream.fileno(), input_view[input_offset:])
                    except (BrokenPipeError, ConnectionResetError):
                        _close_registered_stream(selector, stream)
                        stdin_stream = None
                    else:
                        input_offset += written
                        if input_offset >= len(input_view):
                            _close_registered_stream(selector, stream)
                            stdin_stream = None
                    continue

                name = key.data
                try:
                    chunk = os.read(stream.fileno(), READ_CHUNK_BYTES)
                except BlockingIOError:
                    continue
                if not chunk:
                    output_streams.pop(stream.fileno(), None)
                    _close_registered_stream(selector, stream)
                    continue
                if failure is not None and not isinstance(failure, ProcessTimeout):
                    continue
                if output_bytes + len(chunk) > max_output_bytes:
                    failure = ProcessOutputLimit(
                        "Release helper output exceeded its capture limit"
                    )
                    continue
                output_bytes += len(chunk)
                captured[name].extend(chunk)

        returncode = _wait_owned_process(process)
        if isinstance(failure, ProcessTimeout):
            raise ProcessTimeout(
                failure.timeout,
                _format_output(bytes(captured["stdout"]), text),
                _format_output(bytes(captured["stderr"]), text),
            )
        if isinstance(failure, ProcessOutputLimit):
            raise failure
        if isinstance(failure, _ProcessCancelled):
            raise failure
        return ProcessResult(
            command,
            returncode,
            _format_output(bytes(captured["stdout"]), text),
            _format_output(bytes(captured["stderr"]), text),
        )
    except (ProcessTimeout, ProcessOutputLimit, _ProcessCancelled):
        raise
    except ProcessCleanupError:
        raise
    except BaseException:
        _kill_owned_process(process)
        try:
            process.wait(timeout=CLEANUP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as cleanup_error:
            raise ProcessCleanupError("Release helper process could not be reaped") from cleanup_error
        raise
    finally:
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
    if not POSIX_PROCESS_RUNNER:
        raise NotImplementedError(POSIX_ONLY_MESSAGE)
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
        # The worker owns the PID synchronously and doesn't return until cleanup is proven.
        try:
            await asyncio.shield(worker)
        except _ProcessCancelled:
            pass
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
    if not POSIX_PROCESS_RUNNER:
        raise NotImplementedError(POSIX_ONLY_MESSAGE)
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
