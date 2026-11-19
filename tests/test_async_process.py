"""Bounded shell-free process execution and cleanup contracts."""

import asyncio
import contextvars
import gc
import os
from pathlib import Path
import signal
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import async_process


async def cancel_test_task(task, timeout=5):
    """Drain an owned test task without catching cancellation of its caller."""
    task.cancel()
    _done, pending = await asyncio.wait({task}, timeout=timeout)
    if pending:
        raise AssertionError("owned test task did not finish within cleanup timeout")
    if not task.cancelled():
        task.result()


class AsyncProcessTests(unittest.TestCase):
    def assert_process_stopped(self, pid_file: Path, timeout: float = 3.0) -> None:
        pid = int(pid_file.read_text())
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            # kill(pid, 0) also succeeds for zombies. They cannot run or hold
            # pipes, so distinguish them before declaring leaked live work.
            status = subprocess.run(
                ["/bin/ps", "-o", "stat=", "-p", str(pid)],
                check=False, capture_output=True, text=True,
            )
            if status.returncode != 0 or status.stdout.strip().startswith("Z"):
                return
            time.sleep(0.02)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.fail(f"descendant process {pid} remained after bounded cleanup")

    def test_captures_both_streams_and_preserves_nonzero_status(self):
        result = async_process.run_process(
            [sys.executable, "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"],
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, b"out\n")
        self.assertEqual(result.stderr, b"err\n")
        with self.assertRaisesRegex(async_process.ProcessExecutionError, "status 7"):
            async_process.run_process(
                [sys.executable, "-c", "raise SystemExit(7)"], timeout=5,
            )

    def test_text_mode_keeps_text_output(self):
        result = async_process.run_process(
            [sys.executable, "-c", "print('ok')"], timeout=5, text=True,
        )
        self.assertEqual(result.stdout, "ok\n")
        self.assertEqual(result.stderr, "")

    def test_early_child_exit_returns_process_result_with_large_stdin(self):
        result = async_process.run_process(
            [sys.executable, "-c", "pass"],
            timeout=5,
            check=False,
            input=b"x" * (4 * 1024 * 1024),
        )
        self.assertEqual(result.returncode, 0)

    def test_output_limit_is_shared_by_stdout_and_stderr(self):
        script = (
            "import sys; "
            "sys.stdout.buffer.write(b'o' * 700); sys.stdout.flush(); "
            "sys.stderr.buffer.write(b'e' * 700); sys.stderr.flush()"
        )
        with self.assertRaises(async_process.ProcessOutputLimit):
            async_process.run_process(
                [sys.executable, "-c", script], timeout=5, max_output_bytes=1000,
            )

    def test_timeout_preserves_partial_stdout_and_stderr(self):
        script = (
            "import sys,time; "
            "print('ready', flush=True); print('detail', file=sys.stderr, flush=True); "
            "time.sleep(60)"
        )
        with self.assertRaises(async_process.ProcessTimeout) as raised:
            async_process.run_process(
                [sys.executable, "-c", script], timeout=0.2, text=True,
            )
        self.assertEqual(raised.exception.stdout, "ready\n")
        self.assertEqual(raised.exception.stderr, "detail\n")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_late_output_limit_does_not_replace_timeout_result(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = Path(directory) / "write-after-timeout"
            ready = Path(directory) / "initial-output-written"
            overflowed = Path(directory) / "overflow-written"
            script = (
                "from pathlib import Path; import sys,time; "
                "sys.stdout.buffer.write(b'a' * 100); sys.stdout.flush(); "
                f"ready=Path({str(ready)!r}); gate=Path({str(gate)!r}); "
                f"overflowed=Path({str(overflowed)!r}); ready.write_text('ready'); "
                "exec('while not gate.exists(): time.sleep(.005)'); "
                "sys.stdout.buffer.write(b'b' * 2000); sys.stdout.flush(); "
                "overflowed.write_text('yes'); time.sleep(60)"
            )
            kill = async_process._kill_owned_process
            popen = async_process.subprocess.Popen

            def start_after_initial_output(*args, **kwargs):
                process = popen(*args, **kwargs)
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    if process.poll() is not None:
                        break
                    time.sleep(0.005)
                if not ready.exists():
                    process.kill()
                    process.wait(timeout=2)
                    raise AssertionError("child did not flush initial output before timeout starts")
                return process

            def delayed_kill(process):
                gate.write_text("release")
                deadline = time.monotonic() + 2
                while not overflowed.exists() and time.monotonic() < deadline:
                    time.sleep(0.005)
                if not overflowed.exists():
                    raise AssertionError("child did not write overflowing output after timeout")
                kill(process)

            with patch.object(async_process.subprocess, "Popen", side_effect=start_after_initial_output), \
                    patch.object(async_process, "_kill_owned_process", side_effect=delayed_kill):
                with self.assertRaises(async_process.ProcessTimeout) as raised:
                    async_process.run_process(
                        [sys.executable, "-c", script], timeout=1, max_output_bytes=1024,
                    )
            self.assertTrue(overflowed.exists(), "regression did not actually exceed output bound")
            self.assertEqual(raised.exception.stdout, b"a" * 100)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_timeout_kills_descendant_that_inherits_capture_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory) / "descendant-started"
            pid_file = Path(directory) / "descendant.pid"
            gate = Path(directory) / "release-descendant"
            survived = Path(directory) / "descendant-survived"
            descendant = (
                "from pathlib import Path; import os,time; "
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); Path({str(started)!r}).write_text('started'); "
                f"gate=Path({str(gate)!r}); "
                "exec('while not gate.exists(): time.sleep(.01)'); "
                f"Path({str(survived)!r}).write_text('alive')"
            )
            parent = (
                "from pathlib import Path; import os,subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                f"started=Path({str(started)!r}); "
                "exec('while not started.exists(): time.sleep(.01)'); time.sleep(60)"
            )
            start_time = time.monotonic()
            with self.assertRaises(async_process.ProcessTimeout):
                async_process.run_process([sys.executable, "-c", parent], timeout=0.5)
            self.assertLess(time.monotonic() - start_time, 5)
            self.assertTrue(started.exists(), "test descendant did not start before timeout")
            self.assert_process_stopped(pid_file)
            self.assertFalse(survived.exists(), "descendant kept running after parent timeout")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_timeout_kills_group_after_leader_exits_without_reaping_its_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory) / "leader-exit-descendant-started"
            gate = Path(directory) / "release-leader-exit-descendant"
            survived = Path(directory) / "leader-exit-descendant-survived"
            descendant = (
                "from pathlib import Path; import os,time; "
                f"Path({str(started)!r}).write_text('started'); gate=Path({str(gate)!r}); "
                "exec('while not gate.exists(): time.sleep(.01)'); "
                f"Path({str(survived)!r}).write_text('alive')"
            )
            leader = (
                "from pathlib import Path; import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                f"started=Path({str(started)!r}); "
                "exec('while not started.exists(): time.sleep(.01)')"
            )
            with self.assertRaises(async_process.ProcessTimeout):
                async_process.run_process([sys.executable, "-c", leader], timeout=0.3)
            self.assertTrue(started.exists(), "descendant did not start before leader exit")
            gate.write_text("release")
            time.sleep(0.2)
            self.assertFalse(survived.exists(), "same-group descendant survived leader timeout")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_expired_deadline_kills_descendant_before_reaping_exited_leader(self):
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory) / "detached-pipe-descendant-started"
            pid_file = Path(directory) / "detached-pipe-descendant.pid"
            gate = Path(directory) / "release-detached-pipe-descendant"
            survived = Path(directory) / "detached-pipe-descendant-survived"
            descendant = (
                "from pathlib import Path; import os,time; "
                f"Path({str(started)!r}).write_text('started'); "
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                f"gate=Path({str(gate)!r}); "
                "exec('while not gate.exists(): time.sleep(.005)'); "
                f"Path({str(survived)!r}).write_text('alive')"
            )
            leader = (
                "from pathlib import Path; import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                f"started=Path({str(started)!r}); "
                "exec('while not started.exists(): time.sleep(.005)')"
            )
            process = async_process.subprocess.Popen(
                [sys.executable, "-c", leader],
                stdout=async_process.subprocess.PIPE,
                stderr=async_process.subprocess.PIPE,
                start_new_session=True,
                bufsize=0,
            )
            selector = async_process.selectors.DefaultSelector()
            streams = [process.stdout, process.stderr]
            try:
                deadline = time.monotonic() + 3
                while (not started.exists() or not async_process._process_has_exited(process)):
                    if time.monotonic() >= deadline:
                        self.fail("leader/descendant fixture did not reach the completion boundary")
                    time.sleep(0.005)
                self.assertTrue(pid_file.exists(), "descendant did not publish its PID")
                state = async_process._OwnedProcessState(
                    process=process,
                    command=(sys.executable, "-c", leader),
                    selector=selector,
                    opened_streams=streams,
                    captured={"stdout": bytearray(), "stderr": bytearray()},
                    input_view=memoryview(b""),
                    max_output_bytes=1024,
                    text=False,
                    timeout=0.1,
                    deadline=time.monotonic() - 1,
                    cancel_event=None,
                )
                with self.assertRaises(async_process.ProcessTimeout):
                    async_process._complete_owned_process(state)
            finally:
                selector.close()
                for stream in streams:
                    stream.close()
            self.assert_process_stopped(pid_file)
            gate.write_text("release")
            self.assertFalse(survived.exists(), "descendant ran after completion cleanup")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_output_limit_kills_descendant_that_holds_capture_pipe_open(self):
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory) / "descendant-started"
            pid_file = Path(directory) / "descendant.pid"
            gate = Path(directory) / "release-descendant"
            survived = Path(directory) / "descendant-survived"
            descendant = (
                "from pathlib import Path; import os,time; "
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); Path({str(started)!r}).write_text('started'); "
                f"gate=Path({str(gate)!r}); "
                "exec('while not gate.exists(): time.sleep(.01)'); "
                f"Path({str(survived)!r}).write_text('alive')"
            )
            parent = (
                "from pathlib import Path; import os,subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                f"started=Path({str(started)!r}); "
                "exec('while not started.exists(): time.sleep(.01)'); "
                "sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush(); time.sleep(60)"
            )
            start_time = time.monotonic()
            with self.assertRaises(async_process.ProcessOutputLimit):
                async_process.run_process(
                    [sys.executable, "-c", parent], timeout=5, max_output_bytes=1024,
                )
            self.assertLess(time.monotonic() - start_time, 5)
            self.assertTrue(started.exists(), "test descendant did not start before output limit")
            self.assert_process_stopped(pid_file)
            self.assertFalse(survived.exists(), "descendant kept running after output limit")

    @unittest.skipUnless(os.name == "posix", "escaped-process pipe check is POSIX-specific")
    def test_leader_exit_with_escaped_pipe_holder_fails_cleanup_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "descendant.pid"
            child = (
                "import os,time; "
                f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}], start_new_session=True); "
                "time.sleep(.05)"
            )
            try:
                with self.assertRaises(async_process.ProcessCleanupError):
                    async_process.run_process([sys.executable, "-c", parent], timeout=0.2)
                self.assertTrue(pid_file.exists(), "escaped pipe holder did not start")
            finally:
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_requires_absolute_executable_and_positive_bounds(self):
        with self.assertRaisesRegex(ValueError, "absolute path"):
            async_process.run_process(["python", "-V"], timeout=5)
        with self.assertRaisesRegex(ValueError, "positive"):
            async_process.run_process([sys.executable, "-V"], timeout=0)
        with self.assertRaisesRegex(ValueError, "output limit"):
            async_process.run_process([sys.executable, "-V"], timeout=5, max_output_bytes=0)

    def test_supported_runtime_matrix_is_explicit(self):
        supported = async_process._runtime_supported
        self.assertTrue(supported("linux", (3, 12), "posix"))
        self.assertTrue(supported("linux", (3, 13), "posix"))
        self.assertFalse(supported("linux", (3, 11), "posix"))
        self.assertTrue(supported("darwin", (3, 13), "posix"))
        self.assertFalse(supported("darwin", (3, 12), "posix"))
        self.assertFalse(supported("win32", (3, 13), "nt"))

    def test_unsupported_macos_python_fails_before_spawning(self):
        with (
            patch.object(async_process, "POSIX_PROCESS_RUNNER", True),
            patch.object(async_process.sys, "platform", "darwin"),
            patch.object(async_process.sys, "version_info", (3, 12, 9)),
            patch.object(async_process.os, "name", "posix"),
            patch.object(async_process.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(NotImplementedError, "macOS Python 3.13"):
                async_process.run_process([sys.executable, "-c", "pass"], timeout=1)
        popen.assert_not_called()

    def test_prelaunch_cancellation_does_not_create_process(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch.object(async_process.subprocess, "Popen") as popen:
            with self.assertRaises(async_process._ProcessCancelled):
                async_process._run_owned_process(
                    (sys.executable, "-c", "pass"), env=None, cwd=None,
                    input_bytes=None, max_output_bytes=100, text=False,
                    timeout=1, cancel_event=cancelled,
                )
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_selector_creation_failure_reaps_owned_process(self):
        real_popen = async_process.subprocess.Popen
        processes = []

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch.object(async_process.subprocess, "Popen", side_effect=tracked_popen),
            patch.object(async_process.selectors, "DefaultSelector", side_effect=OSError("no selector")),
        ):
            with self.assertRaisesRegex(OSError, "no selector"):
                async_process.run_process(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=5, input=b"payload",
                )
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode, "selector failure leaked its child")
        self.assertTrue(all(stream is None or stream.closed for stream in (
            processes[0].stdin, processes[0].stdout, processes[0].stderr,
        )), "selector failure leaked an owned pipe")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_nonblocking_setup_failure_closes_all_owned_pipes(self):
        real_popen = async_process.subprocess.Popen
        processes = []

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            return process

        with (
            patch.object(async_process.subprocess, "Popen", side_effect=tracked_popen),
            patch.object(async_process.os, "set_blocking", side_effect=OSError("fd setup failed")),
        ):
            with self.assertRaisesRegex(OSError, "fd setup failed"):
                async_process.run_process(
                    [sys.executable, "-c", "import time; time.sleep(60)"],
                    timeout=5, input=b"payload",
                )
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode, "fd setup failure leaked its child")
        self.assertTrue(all(stream is None or stream.closed for stream in (
            processes[0].stdin, processes[0].stdout, processes[0].stderr,
        )), "fd setup failure leaked an owned pipe")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_terminal_wait_crossing_deadline_reports_timeout(self):
        real_wait = async_process._wait_owned_process

        def delayed_wait(process):
            status = real_wait(process)
            time.sleep(0.55)
            return status

        with patch.object(async_process, "_wait_owned_process", side_effect=delayed_wait):
            with self.assertRaises(async_process.ProcessTimeout):
                async_process.run_process(
                    [sys.executable, "-c", "pass"], timeout=0.5,
                )

    @unittest.skipUnless(os.name == "posix", "selector behavior is POSIX-specific")
    def test_empty_selector_waits_while_child_runs_without_pipes(self):
        real_sleep = time.sleep
        sleeps = []

        def observed_sleep(duration):
            sleeps.append(duration)
            return real_sleep(duration)

        with patch.object(async_process.time, "sleep", side_effect=observed_sleep):
            result = async_process.run_process(
                [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(.08)"],
                timeout=2,
            )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(sleeps, "empty selector loop spun without sleeping")


@unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
class AsyncCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.loop_exceptions = []
        self.previous_exception_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(
            lambda _loop, context: self.loop_exceptions.append(context),
        )

    async def asyncTearDown(self):
        # Flush callbacks and collect completed futures before checking that
        # cancellation did not orphan a shield wrapper's exception.
        await asyncio.sleep(0)
        gc.collect()
        await asyncio.sleep(0)
        self.loop.set_exception_handler(self.previous_exception_handler)
        self.assertEqual(
            self.loop_exceptions,
            [],
            "asyncio reported an unretrieved cancellation/worker exception",
        )

    async def test_cancellation_kills_owned_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory) / "descendant-started"
            gate = Path(directory) / "release-descendant"
            survived = Path(directory) / "descendant-survived"
            descendant = (
                "from pathlib import Path; import time; "
                f"Path({str(started)!r}).write_text('started'); gate=Path({str(gate)!r}); "
                "exec('while not gate.exists(): time.sleep(.01)'); "
                f"Path({str(survived)!r}).write_text('alive')"
            )
            parent = (
                "from pathlib import Path; import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                f"started=Path({str(started)!r}); "
                "exec('while not started.exists(): time.sleep(.01)'); time.sleep(60)"
            )
            task = asyncio.create_task(async_process.run_process_async(
                [sys.executable, "-c", parent],
            ))
            try:
                deadline = asyncio.get_running_loop().time() + 5
                while not started.exists() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(started.exists(), "test descendant did not start")
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
                gate.write_text("release")
                await asyncio.sleep(0.2)
                self.assertFalse(survived.exists(), "spawn cancellation left a descendant running")
            finally:
                if not task.done():
                    await cancel_test_task(task)

    async def test_test_task_cleanup_accepts_owned_task_cancellation(self):
        task = asyncio.create_task(asyncio.sleep(60))
        await cancel_test_task(task)
        self.assertTrue(task.cancelled())

    async def test_test_task_cleanup_propagates_caller_cancellation(self):
        started = asyncio.Event()
        cleanup_started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def owned_task():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await release_cleanup.wait()

        task = asyncio.create_task(owned_task())
        await asyncio.wait_for(started.wait(), timeout=1)
        cleanup = asyncio.create_task(cancel_test_task(task))
        try:
            await asyncio.wait_for(cleanup_started.wait(), timeout=1)
            cleanup.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(cleanup, timeout=1)
            self.assertFalse(task.done(), "caller cancellation interrupted owned task cleanup")
        finally:
            release_cleanup.set()
            _done, pending = await asyncio.wait({cleanup, task}, timeout=1)
            self.assertFalse(pending, "test cleanup left pending tasks")
        self.assertTrue(task.cancelled())

    async def test_test_task_cleanup_remains_bounded(self):
        started = asyncio.Event()
        release_cleanup = asyncio.Event()

        async def owned_task():
            try:
                started.set()
                await asyncio.Event().wait()
            finally:
                await release_cleanup.wait()

        task = asyncio.create_task(owned_task())
        await asyncio.wait_for(started.wait(), timeout=1)
        try:
            with self.assertRaisesRegex(AssertionError, "cleanup timeout"):
                await cancel_test_task(task, timeout=0.01)
            self.assertFalse(task.done())
        finally:
            release_cleanup.set()
            _done, pending = await asyncio.wait({task}, timeout=1)
            self.assertFalse(pending, "test cleanup left a pending task")
        self.assertTrue(task.cancelled())

    async def test_test_task_cleanup_preserves_owned_task_failure(self):
        failure = RuntimeError("owned test task cleanup failed")

        async def owned_task():
            raise failure

        task = asyncio.create_task(owned_task())
        _done, pending = await asyncio.wait({task}, timeout=1)
        self.assertFalse(pending, "owned task did not fail within the test timeout")
        with self.assertRaises(RuntimeError) as raised:
            await cancel_test_task(task)
        self.assertIs(raised.exception, failure)

    async def test_repeated_cancellation_waits_until_owned_process_cleanup_finishes(self):
        kill_started = threading.Event()
        process_started = threading.Event()
        release_kill = threading.Event()
        real_kill = async_process._kill_owned_process
        real_popen = async_process.subprocess.Popen

        def started_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            process_started.set()
            return process

        def held_kill(process):
            real_kill(process)
            kill_started.set()
            if not release_kill.wait(timeout=5):
                raise AssertionError("test did not release the process cleanup gate")

        with (
            patch.object(async_process.subprocess, "Popen", side_effect=started_popen),
            patch.object(async_process, "_kill_owned_process", side_effect=held_kill),
        ):
            task = asyncio.create_task(async_process.run_process_async(
                [sys.executable, "-c", "import time; time.sleep(60)"],
            ))
            try:
                self.assertTrue(await asyncio.to_thread(process_started.wait, 3))
                task.cancel()
                self.assertTrue(await asyncio.to_thread(kill_started.wait, 3))
                task.cancel()
                await asyncio.sleep(0.05)
                self.assertFalse(task.done(), "repeated cancellation abandoned the owned worker")
                release_kill.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            finally:
                release_kill.set()
                if not task.done():
                    task.cancel()
                    _done, pending = await asyncio.wait({task}, timeout=5)
                    self.assertFalse(pending, "owned worker did not finish after cleanup")
                    if not task.cancelled():
                        task.result()
                    self.assertTrue(task.cancelled(), "cleanup unexpectedly completed without cancellation")

    async def test_caller_cancellation_wins_over_output_limit_after_worker_cleanup(self):
        worker_started = threading.Event()

        def output_limit_after_cancel(*_args, cancel_event, **_kwargs):
            worker_started.set()
            if not cancel_event.wait(timeout=3):
                raise AssertionError("caller cancellation did not reach the process worker")
            raise async_process.ProcessOutputLimit("fixture output limit")

        with patch.object(async_process, "_run_owned_process", side_effect=output_limit_after_cancel):
            task = asyncio.create_task(async_process.run_process_async(
                [sys.executable, "-c", "pass"],
            ))
            self.assertTrue(await asyncio.to_thread(worker_started.wait, 3))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)


    async def test_escaped_pipe_holder_during_cancellation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            started = Path(directory) / "escaped-descendant-started"
            pid_file = Path(directory) / "escaped-descendant.pid"
            gate = Path(directory) / "release-descendant"
            survived = Path(directory) / "escaped-descendant-survived"
            descendant = (
                "from pathlib import Path; import os,time; "
                f"Path({str(started)!r}).write_text('started'); "
                f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                f"gate=Path({str(gate)!r}); "
                "exec('while not gate.exists(): time.sleep(.01)'); "
                f"Path({str(survived)!r}).write_text('alive')"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}], start_new_session=True); "
                "time.sleep(60)"
            )
            task = asyncio.create_task(async_process.run_process_async(
                [sys.executable, "-c", parent], max_output_bytes=1024,
            ))
            try:
                deadline = asyncio.get_running_loop().time() + 5
                while not started.exists() and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(started.exists(), "escaped test descendant did not start")
                task.cancel()
                with self.assertRaises(async_process.ProcessCleanupError):
                    await asyncio.wait_for(task, timeout=8)
                self.assertTrue(pid_file.exists())
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
                gate.write_text("release")
                await asyncio.sleep(0.2)
                self.assertFalse(survived.exists(), "escaped pipe holder survived cleanup")
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=8)
                    except (asyncio.CancelledError, async_process.ProcessCleanupError):
                        pass
                if pid_file.exists():
                    try:
                        os.kill(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    async def test_unsupported_platform_fails_before_spawning(self):
        with patch.object(async_process, "POSIX_PROCESS_RUNNER", False), \
                patch.object(async_process.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(NotImplementedError, "Linux Python 3.12"):
                await async_process.run_process_async([sys.executable, "-c", "pass"])
            with self.assertRaisesRegex(NotImplementedError, "Linux Python 3.12"):
                async_process.run_process([sys.executable, "-c", "pass"], timeout=1)
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "waitid capability test is POSIX-specific")
    def test_missing_nonreaping_wait_capability_fails_before_spawning(self):
        with patch.object(async_process.os, "waitid", None), \
                patch.object(async_process.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(NotImplementedError, r"waitid\(WNOWAIT\)"):
                async_process.run_process([sys.executable, "-c", "pass"], timeout=1)
        popen.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_missing_process_group_falls_back_to_killing_owned_leader(self):
        real_popen = async_process.subprocess.Popen
        processes = []
        direct_kills = []

        def tracked_popen(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            original_kill = process.kill

            def tracked_kill():
                direct_kills.append(process.pid)
                return original_kill()

            process.kill = tracked_kill
            processes.append(process)
            return process

        with patch.object(async_process.subprocess, "Popen", side_effect=tracked_popen), \
                patch.object(async_process.os, "killpg", side_effect=ProcessLookupError):
            with self.assertRaises(async_process.ProcessTimeout):
                async_process.run_process(
                    [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1,
                )
        self.assertEqual(len(processes), 1)
        self.assertEqual(direct_kills, [processes[0].pid])
        self.assertIsNotNone(processes[0].returncode, "owned leader was not reaped")


class AsyncRunnerShutdownTests(unittest.TestCase):
    def test_asyncio_run_shutdown_waits_for_executor_before_caller_finalizes(self):
        worker_started = threading.Event()
        worker_finished = threading.Event()
        outcomes = []

        def blocked_worker(*_args, cancel_event, **_kwargs):
            worker_started.set()
            if not cancel_event.wait(timeout=3):
                raise AssertionError("caller shutdown did not signal worker cancellation")
            worker_finished.set()
            return async_process.ProcessResult((sys.executable, "-c", "pass"), 0, b"", b"")

        def record_outcome(task):
            outcomes.append((worker_finished.is_set(), task.cancelled()))

        async def start_worker_task():
            task = asyncio.create_task(async_process.run_process_async([sys.executable, "-c", "pass"]))
            task.add_done_callback(record_outcome)
            if not await asyncio.to_thread(worker_started.wait, 3):
                self.fail("executor worker did not start")

        with patch.object(async_process, "_run_owned_process", side_effect=blocked_worker):
            asyncio.run(start_worker_task())

        self.assertEqual(outcomes, [(True, True)], "caller finalized before worker cleanup")

    def test_run_process_async_preserves_contextvars(self):
        request_id = contextvars.ContextVar("request_id", default=None)
        request_id.set("release-test")
        observed = []

        def read_context(*_args, **_kwargs):
            observed.append(request_id.get())
            return async_process.ProcessResult((sys.executable, "-c", "pass"), 0, b"", b"")

        async def run():
            return await async_process.run_process_async([sys.executable, "-c", "pass"])

        with patch.object(async_process, "_run_owned_process", side_effect=read_context):
            result = asyncio.run(run())

        self.assertEqual(result.returncode, 0)
        self.assertEqual(observed, ["release-test"])


if __name__ == "__main__":
    unittest.main()
