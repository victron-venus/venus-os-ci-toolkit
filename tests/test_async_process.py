"""Bounded shell-free process execution and cleanup contracts."""

import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import async_process


class AsyncProcessTests(unittest.TestCase):
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

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_timeout_kills_descendant_that_inherits_capture_pipes(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-survived"
            descendant = f"import time; time.sleep(.6); open({str(marker)!r}, 'w').write('alive')"
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                "time.sleep(60)"
            )
            started = time.monotonic()
            with self.assertRaises(async_process.ProcessTimeout):
                async_process.run_process([sys.executable, "-c", parent], timeout=0.1)
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(0.8)
            self.assertFalse(marker.exists(), "descendant kept running after parent timeout")

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-specific")
    def test_output_limit_kills_descendant_that_holds_capture_pipe_open(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "descendant-survived"
            descendant = f"import time; time.sleep(.6); open({str(marker)!r}, 'w').write('alive')"
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable, '-c', {descendant!r}]); "
                "sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush(); time.sleep(60)"
            )
            started = time.monotonic()
            with self.assertRaises(async_process.ProcessOutputLimit):
                async_process.run_process(
                    [sys.executable, "-c", parent], timeout=5, max_output_bytes=1024,
                )
            self.assertLess(time.monotonic() - started, 5)
            time.sleep(0.8)
            self.assertFalse(marker.exists(), "descendant kept running after output limit")

    def test_requires_absolute_executable_and_positive_bounds(self):
        with self.assertRaisesRegex(ValueError, "absolute path"):
            async_process.run_process(["python", "-V"], timeout=5)
        with self.assertRaisesRegex(ValueError, "positive"):
            async_process.run_process([sys.executable, "-V"], timeout=0)
        with self.assertRaisesRegex(ValueError, "output limit"):
            async_process.run_process([sys.executable, "-V"], timeout=5, max_output_bytes=0)


if __name__ == "__main__":
    unittest.main()
