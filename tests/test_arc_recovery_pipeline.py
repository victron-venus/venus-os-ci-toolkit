import hashlib
import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import time
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
