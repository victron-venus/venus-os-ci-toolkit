"""Bounded shell-free asynchronous process execution for release tooling."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import shutil
import stat
from typing import Mapping, Sequence

MAX_OUTPUT_BYTES = 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


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


@dataclass(frozen=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes | str
    stderr: bytes | str


@dataclass
class _CapturedOutput:
    stdout: bytearray
    stderr: bytearray

    @classmethod
    def create(cls) -> _CapturedOutput:
        return cls(bytearray(), bytearray())


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


async def _read_bounded(
    stream: asyncio.StreamReader,
    output: bytearray,
    budget: list[int],
    budget_lock: asyncio.Lock,
    limit: int,
) -> bytes:
    while chunk := await stream.read(READ_CHUNK_BYTES):
        async with budget_lock:
            if budget[0] + len(chunk) > limit:
                raise ProcessOutputLimit("Release helper output exceeded its capture limit")
            budget[0] += len(chunk)
        output.extend(chunk)
    return bytes(output)


async def _reap_after_failure(
    process: asyncio.subprocess.Process,
    tasks: Sequence[asyncio.Task[object]],
) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError as error:
        raise ProcessCleanupError("Release helper process could not be reaped") from error
    try:
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
    except asyncio.TimeoutError as error:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise ProcessCleanupError("Release helper output pipes could not be closed") from error


def _validate_process_request(argv: Sequence[str], max_output_bytes: int) -> tuple[str, ...]:
    command = tuple(argv)
    if not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("Process argv must contain non-empty strings")
    if not Path(command[0]).is_absolute():
        raise ValueError("Process executable must be an absolute path")
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("Process output limit must be a positive integer")
    return command


def _process_options(
    env: Mapping[str, str] | None,
    cwd: str | Path | None,
    input_bytes: bytes | None,
) -> dict[str, object]:
    options: dict[str, object] = {
        "cwd": str(cwd) if cwd is not None else None,
        "env": dict(env) if env is not None else None,
        "stdin": asyncio.subprocess.PIPE if input_bytes is not None else None,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
    }
    if os.name == "posix":
        options["start_new_session"] = True
    return options


async def _write_stdin(process: asyncio.subprocess.Process, input_bytes: bytes) -> None:
    if process.stdin is None:
        raise RuntimeError("Process stdin pipe was not created")
    try:
        process.stdin.write(input_bytes)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()
    try:
        await process.stdin.wait_closed()
    except (BrokenPipeError, ConnectionResetError):
        pass


async def _capture_process(
    process: asyncio.subprocess.Process,
    command: tuple[str, ...],
    capture: _CapturedOutput,
    max_output_bytes: int,
    input_bytes: bytes | None,
    text: bool,
) -> ProcessResult:
    if process.stdout is None or process.stderr is None:
        await _reap_after_failure(process, ())
        raise RuntimeError("Process capture pipes were not created")

    output_budget = [0]
    budget_lock = asyncio.Lock()
    readers: tuple[asyncio.Task[bytes], ...] = (
        asyncio.create_task(_read_bounded(
            process.stdout, capture.stdout, output_budget, budget_lock, max_output_bytes,
        )),
        asyncio.create_task(_read_bounded(
            process.stderr, capture.stderr, output_budget, budget_lock, max_output_bytes,
        )),
    )
    tasks: tuple[asyncio.Task[object], ...] = tuple(readers) + (asyncio.create_task(process.wait()),)
    if input_bytes is not None:
        tasks += (asyncio.create_task(_write_stdin(process, input_bytes)),)
    try:
        stdout, stderr, returncode, *_unused = await asyncio.gather(*tasks)
    except BaseException:
        await _reap_after_failure(process, tasks)
        raise
    return ProcessResult(command, returncode, _format_output(stdout, text), _format_output(stderr, text))


async def run_process_async(
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    input_bytes: bytes | None = None,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
    text: bool = False,
    _capture: _CapturedOutput | None = None,
) -> ProcessResult:
    """Run argv directly with bounded output; callers own their async deadline."""
    command = _validate_process_request(argv, max_output_bytes)
    process = await asyncio.create_subprocess_exec(
        *command, **_process_options(env, cwd, input_bytes),
    )
    capture = _capture if _capture is not None else _CapturedOutput.create()
    result = await _capture_process(process, command, capture, max_output_bytes, input_bytes, text)
    return ProcessResult(command, result.returncode, result.stdout, result.stderr)


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
    """Synchronous entry point for CLI code using the bounded async runner."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Process timeout must be positive")
    capture = _CapturedOutput.create()

    try:
        result = asyncio.run(asyncio.wait_for(
            run_process_async(
                argv, env=env, cwd=cwd, input_bytes=input,
                max_output_bytes=max_output_bytes, text=text, _capture=capture,
            ),
            timeout=timeout,
        ))
    except asyncio.TimeoutError as error:
        raise ProcessTimeout(timeout, _format_output(bytes(capture.stdout), text),
                             _format_output(bytes(capture.stderr), text)) from error
    if check and result.returncode != 0:
        raise ProcessExecutionError(result.returncode)
    return result
