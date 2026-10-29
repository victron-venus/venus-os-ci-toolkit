import hashlib
import json
import os
import signal
import socket
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import arc_recovery_pipeline as pipeline


def python(code):
    return [sys.executable, "-c", code]


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.destination = self.root / "payload.age"

    def tearDown(self):
        self.temporary.cleanup()

    def run_pipe(self, commands, **kwargs):
        settings = dict(maximum=1024, reserve=0, timeout=3, inputs={})
        settings.update(kwargs)
        result = pipeline.run(commands, self.destination, **settings)
        receipt = json.loads((self.root / "payload.age.receipt.json").read_text())
        self.assertEqual(result, receipt)
        return result

    def test_unsupported_platform_fails_before_any_output_or_process(self):
        with patch.object(pipeline.os, "name", "nt"), \
                patch.object(pipeline.subprocess, "Popen") as launch, \
                self.assertRaisesRegex(ValueError, "requires POSIX"):
            pipeline.run([python("print(1)")], self.destination,
                         maximum=10, reserve=0, timeout=1, inputs={})
        launch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_missing_waitid_fails_before_any_output_or_process(self):
        with patch.dict(pipeline.os.__dict__), \
                patch.object(pipeline.subprocess, "Popen") as launch:
            pipeline.os.__dict__.pop("waitid", None)
            with self.assertRaisesRegex(ValueError, r"waitid/WNOWAIT.*Python >= 3.13"):
                self.run_pipe([python("print(1)")])
        launch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_child_reaper_handler_fails_before_launch(self):
        with patch.object(pipeline.signal, "getsignal", return_value=signal.SIG_IGN), \
                patch.object(pipeline.subprocess, "Popen") as launch, \
                self.assertRaisesRegex(ValueError, "sole child reaper"):
            self.run_pipe([python("print(1)")])
        launch.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_repeated_status_peeks_leave_leader_waitable(self):
        with subprocess.Popen(python("raise SystemExit(7)"), start_new_session=True) as child:
            deadline = time.monotonic() + 2
            status = None
            while status is None and time.monotonic() < deadline:
                status = pipeline.child_status(child)
                time.sleep(0.005)
            self.assertEqual(status, 7)
            self.assertEqual(pipeline.child_status(child), 7)
            self.assertIsNone(child.returncode)
            self.assertEqual(child.wait(timeout=1), 7)

    def test_early_stage_stays_waitable_until_group_teardown(self):
        kill_group = os.killpg
        retained = []

        def check_then_kill(pid, sig):
            status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.assertIsNotNone(status)
            self.assertEqual(status.si_pid, pid)
            retained.append(pid)
            kill_group(pid, sig)

        with patch.object(pipeline.os, "killpg", side_effect=check_then_kill):
            result = self.run_pipe([python("print('a',end='')"),
                                    python("import sys,time;sys.stdout.write(sys.stdin.read());time.sleep(.1)")])
        self.assertTrue(result["passed"])
        self.assertEqual(result["returncodes"], [0, 0])
        self.assertEqual(len(retained), 2)

    def test_exited_leaders_pipe_holding_descendant_is_terminated(self):
        address = str(self.root / "owned.sock")
        code = ("import os,socket,time\n"
                "if os.fork(): os._exit(0)\n"
                "peer=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)\n"
                f"peer.connect({address!r})\n"
                "peer.sendall(b'ready')\n"
                "time.sleep(10)\n")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(address)
            server.listen(1)
            server.settimeout(2)
            start = time.monotonic()
            result = self.run_pipe([python(code)], timeout=1)
            self.assertEqual(result["failure"], "pipeline deadline")
            self.assertLess(time.monotonic() - start, 4)
            self.assertEqual(result["returncodes"], [0])
            peer, _ = server.accept()
            with peer:
                peer.settimeout(1)
                self.assertEqual(peer.recv(5), b"ready")
                # Only the owned descendant has this connection. EOF proves
                # it did not survive its already-exited leader's teardown.
                self.assertEqual(peer.recv(1), b"")

    def test_consumed_leader_identity_is_never_signaled_or_accepted(self):
        status = pipeline.child_status

        def consumed(child):
            child.wait(timeout=2)
            return status(child)

        with patch.object(pipeline, "child_status", side_effect=consumed), \
                patch.object(pipeline.os, "killpg") as kill_group:
            result = self.run_pipe([python("pass")])
        self.assertEqual(result["failure"], "pipeline child ownership lost")
        self.assertFalse(result["passed"])
        kill_group.assert_not_called()

    def test_signal_error_does_not_skip_other_groups_or_failure_receipt(self):
        kill_group = os.killpg
        signaled = []

        def signal_then_error(pid, sig):
            try:
                kill_group(pid, sig)
            except PermissionError:
                # Native Darwin reports EPERM for an all-zombie group.
                pass
            signaled.append(pid)
            if len(signaled) == 1:
                raise OSError("simulated signal failure")

        with patch.object(pipeline.os, "killpg", side_effect=signal_then_error):
            result = self.run_pipe([python("pass"), python("pass")])
        self.assertEqual(result["failure"], "pipeline group teardown failed")
        self.assertFalse(result["passed"])
        self.assertEqual(len(signaled), 2)
        self.assertEqual(result["returncodes"], [0, 0])

    def inspect_fixture(self, code, timeout=1):
        launch = subprocess.Popen
        inspectors = []

        def fixture(command, **kwargs):
            self.assertEqual(command, ["/bin/ps", "-g", "12345", "-o", "pid=,pgid=,stat="])
            child = launch(python(code), **kwargs)
            inspectors.append(child)
            return child

        try:
            with patch.object(pipeline, "child_status", return_value=0), \
                    patch.object(pipeline.subprocess, "Popen", side_effect=fixture), \
                    patch.object(pipeline.os, "killpg") as group_signal:
                pipeline.terminated_group_only(SimpleNamespace(pid=12345), time.monotonic() + timeout)
                group_signal.assert_not_called()
        finally:
            self.assertTrue(all(child.returncode is not None for child in inspectors))

    def test_darwin_zombie_only_projection_is_accepted(self):
        self.inspect_fixture("print('12345 12345 Z\\n12346 12345 Z')")

    def test_darwin_live_or_unknown_group_is_not_accepted(self):
        for projection in ["12345 12345 Z\n12346 12345 S", "12346 12345 Z",
                           "12345 99999 Z", "12345 12345 Z\n12345 12345 Z", "truncated"]:
            with self.subTest(projection=projection), self.assertRaises(ValueError):
                self.inspect_fixture(f"print({projection!r})")

    def test_darwin_projection_errors_and_timeouts_are_not_accepted(self):
        for code in ["raise SystemExit(1)", "import time;time.sleep(10)",
                     "print('x'*100000)"]:
            with self.subTest(code=code), self.assertRaises(ValueError):
                self.inspect_fixture(code, timeout=.15)

    def test_missing_waitable_child_is_ownership_failure(self):
        with patch.object(pipeline.os, "waitid", side_effect=ChildProcessError), \
                self.assertRaisesRegex(ValueError, "ownership lost"):
            pipeline.child_status(SimpleNamespace(pid=12345, returncode=None))

    def test_roundtrip_and_exact_hash(self):
        result = self.run_pipe(
            [python("print('hello', end='')"), python("import sys;sys.stdout.write(sys.stdin.read().upper())")],
            expected_bytes=5, expected_sha256=hashlib.sha256(b"HELLO").hexdigest())
        self.assertTrue(result["passed"])
        self.assertEqual(self.destination.read_bytes(), b"HELLO")
        self.assertEqual(self.destination.stat().st_ino,
                         (self.root / "payload.age.partial").stat().st_ino)
        self.assertFalse(result["cpu_memory_limits_verified"])

    def test_byte_cap_refuses_chunk_before_write(self):
        result = self.run_pipe([python("print('x' * 30,end='')")], maximum=5)
        self.assertFalse(result["passed"])
        self.assertEqual(result["failure"], "output byte limit")
        self.assertEqual((self.root / "payload.age.partial").stat().st_size, 0)
        self.assertFalse(self.destination.exists())

    def test_expected_cap_and_truncation(self):
        result = self.run_pipe([python("print('abc',end='')")], expected_bytes=4)
        self.assertEqual(result["failure"], "truncated output")
        self.assertEqual((self.root / "payload.age.partial").read_bytes(), b"abc")

    def test_bad_digest(self):
        result = self.run_pipe([python("print('abc',end='')")], expected_sha256="0" * 64)
        self.assertEqual(result["failure"], "output digest")
        self.assertFalse(self.destination.exists())

    def test_stderr_flood_is_bounded(self):
        result = self.run_pipe([python("import sys;sys.stderr.write('x'*1000000)")], stderr_maximum=10)
        self.assertEqual(result["failure"], "stderr limit")
        self.assertLessEqual(result["stderr_bytes"][0], pipeline.BLOCK)

    def test_silent_deadline(self):
        start = time.monotonic()
        result = self.run_pipe([python("import time;time.sleep(10)")], timeout=1)
        self.assertEqual(result["failure"], "pipeline deadline")
        self.assertLess(time.monotonic() - start, 4)
        self.assertTrue(all(code is not None for code in result["returncodes"]))

    def test_closed_pipes_alive_child_deadline(self):
        start = time.monotonic()
        result = self.run_pipe([python("import os,time;os.close(1);os.close(2);time.sleep(10)")], timeout=1)
        self.assertEqual(result["failure"], "pipeline deadline")
        self.assertLess(time.monotonic() - start, 4)

    def test_nonzero_pipeline_stage(self):
        result = self.run_pipe([python("raise SystemExit(7)"), python("import sys;sys.stdout.write(sys.stdin.read())")])
        self.assertFalse(result["passed"])
        self.assertIn(7, result["returncodes"])
        self.assertFalse(self.destination.exists())

    def test_retained_file_untouched(self):
        self.destination.write_bytes(b"retained")
        with self.assertRaises(ValueError), patch.object(pipeline.subprocess, "Popen") as launch:
            self.run_pipe([python("print('new')")])
        launch.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), b"retained")

    def test_fsync_intent_failure_prevents_launch(self):
        with patch.object(pipeline.os, "fsync", side_effect=OSError("test")), \
                patch.object(pipeline.subprocess, "Popen") as launch, self.assertRaises(OSError):
            self.run_pipe([python("print('new')")])
        launch.assert_not_called()
        self.assertFalse((self.root / "payload.age.partial").exists())

    def test_input_mismatch_prevents_launch(self):
        source = self.root / "source.py"
        source.write_bytes(b"unchanged")
        with patch.object(pipeline.subprocess, "Popen") as launch, self.assertRaises(ValueError):
            self.run_pipe([python("print('new')")], inputs={str(source): {"bytes": 9, "sha256": "0" * 64}})
        launch.assert_not_called()

    def test_live_and_final_disk_failure_retains_failure_receipt(self):
        def gate(descriptor, pending, reserve):
            info = os.fstat(descriptor)
            if pending == 0 and stat.S_ISREG(info.st_mode) and info.st_size:
                raise ValueError("disk reserve")
        with patch.object(pipeline, "disk_gate", side_effect=gate):
            result = self.run_pipe([python("print('small',end='')")])
        self.assertEqual(result["failure"], "disk reserve")
        self.assertFalse(self.destination.exists())

    def test_invalid_typed_bounds(self):
        for key, value in [("maximum", True), ("timeout", False), ("reserve", -1), ("expected_bytes", True)]:
            with self.subTest(key=key), patch.object(pipeline.subprocess, "Popen") as launch, self.assertRaises(ValueError):
                self.run_pipe([python("print('new')")], **{key: value})
            launch.assert_not_called()

    def test_metadata_floor_before_process_launch(self):
        with patch.object(pipeline, "disk_gate", side_effect=ValueError("disk reserve")), \
                patch.object(pipeline.subprocess, "Popen") as launch, self.assertRaises(ValueError):
            self.run_pipe([python("print('new')")])
        launch.assert_not_called()
        self.assertFalse((self.root / "payload.age.intent.json").exists())

    def test_metadata_limit_before_process_launch(self):
        with patch.object(pipeline, "MAX_METADATA", 1), \
                patch.object(pipeline.subprocess, "Popen") as launch, self.assertRaises(ValueError):
            self.run_pipe([python("print('new')")])
        launch.assert_not_called()
        self.assertFalse((self.root / "payload.age.intent.json").exists())

    def test_receipt_floor_failure_preserves_output_without_acceptance(self):
        original = pipeline.durable_json
        def save(path, value, reserve):
            if path.name.endswith(".receipt.json"):
                with patch.object(pipeline, "disk_gate", side_effect=ValueError("disk reserve")):
                    return original(path, value, reserve)
            return original(path, value, reserve)
        with patch.object(pipeline, "durable_json", side_effect=save), self.assertRaises(ValueError):
            self.run_pipe([python("print('small',end='')")])
        self.assertEqual(self.destination.read_bytes(), b"small")
        self.assertTrue((self.root / "payload.age.partial").exists())
        self.assertFalse((self.root / "payload.age.receipt.json").exists())

    def test_postwrite_receipt_gate_cannot_publish_success(self):
        path = self.root / "receipt.json"
        with patch.object(pipeline, "disk_gate", side_effect=[None, ValueError("disk reserve")]), \
                self.assertRaises(ValueError):
            pipeline.durable_json(path, {"passed": True}, 0)
        self.assertFalse(path.exists())
        self.assertTrue((self.root / "receipt.json.pending").exists())

    def test_postlink_receipt_gate_revokes_own_success_record(self):
        path = self.root / "receipt.json"
        with patch.object(pipeline, "disk_gate", side_effect=[None, None, ValueError("disk reserve")]), \
                self.assertRaises(ValueError):
            pipeline.durable_json(path, {"passed": True}, 0)
        self.assertFalse(path.exists())
        self.assertTrue((self.root / "receipt.json.pending").exists())


if __name__ == "__main__":
    unittest.main()
