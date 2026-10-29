"""Small offline streams prove download bounds and subprocess cleanup."""

from __future__ import annotations

from pathlib import Path
import shutil
# Only fixed, tiny local Python fixtures are launched; no network is used.
import subprocess  # nosec B404
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_control as rc


class ReleaseDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "payload"
        self.budget = rc.DownloadBudget(32, reserve_bytes=10)
        self.children = []

    @staticmethod
    def identity(data):
        return {"size": len(data), "sha256": rc.digest(data)}

    def download(self, gh, data=b"payload", **options):
        return rc.download_asset(
            gh,
            1,
            self.output,
            expected=self.identity(data),
            budget=options.pop("budget", self.budget),
            **options,
        )

    def transport(self, data):
        return Mock(download_asset=lambda _path, sink: sink.write(data))

    def fixture_process(self, code):
        original = subprocess.Popen

        def start(command, **options):
            self.assertEqual(command[:3], ["gh", "api", "--hostname"])
            process = original([sys.executable, "-c", code], **options)
            self.children.append(process)
            return process

        return patch.object(rc.subprocess, "Popen", side_effect=start)

    def assert_reaped(self):
        self.assertTrue(self.children)
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_exact_empty_and_multichunk_streams(self):
        for data in [b"", b"one", b"a" * 19]:
            with self.subTest(size=len(data)):
                with (
                    self.fixture_process("import os;os.write(1," + repr(data) + ")"),
                    patch.object(rc, "ASSET_CHUNK_SIZE", 4),
                ):
                    self.assertEqual(
                        self.download(rc.GitHub("test/repo"), data), self.identity(data)
                    )
                self.assertEqual(self.output.read_bytes(), data)
                self.output.unlink()
        self.assertEqual(self.budget.written, 22)
        self.assert_reaped()

    def test_short_or_wrong_digest_never_leaves_a_payload(self):
        for actual in [b"pay", b"wrong!!"]:
            with self.subTest(actual=actual), self.assertRaises(rc.ReleaseError):
                self.download(self.transport(actual))
            self.assertFalse(self.output.exists())

    def test_oversized_stream_rejects_before_excess_write_and_reaps(self):
        seen = []
        original = Path.unlink

        def removing(path, *args, **kwargs):
            if path == self.output:
                seen.append(path.stat().st_size)
            return original(path, *args, **kwargs)

        with (
            self.fixture_process(
                "import os,time;os.write(1,b'123456789');time.sleep(20)"
            ),
            patch.object(rc, "ASSET_CHUNK_SIZE", 4),
            patch.object(Path, "unlink", removing),
            self.assertRaisesRegex(rc.ReleaseError, "exceeds expected"),
        ):
            self.download(rc.GitHub("test/repo"), b"12345678")
        self.assertTrue(seen)
        self.assertLessEqual(max(seen), 8)
        self.assertFalse(self.output.exists())
        self.assert_reaped()

    def test_cumulative_budget_does_not_reset_when_verified_file_is_removed(self):
        budget = rc.DownloadBudget(8)
        self.download(self.transport(b"1234"), b"1234", budget=budget)
        self.output.unlink()
        self.download(self.transport(b"5678"), b"5678", budget=budget)
        self.output.unlink()
        gh = Mock()
        with self.assertRaisesRegex(rc.ReleaseError, "byte budget"):
            self.download(gh, b"9", budget=budget)
        gh.download_asset.assert_not_called()
        self.assertEqual(budget.written, 8)
        self.assertFalse(self.output.exists())

    def test_unknown_negative_bool_and_malformed_identity_fail_before_transfer(self):
        correct = self.identity(b"payload")
        variants = [
            None,
            {},
            {"size": 7},
            correct | {"size": None},
            correct | {"size": True},
            correct | {"size": -1},
            correct | {"sha256": "x"},
            correct | {"extra": 1},
        ]
        for expected in variants:
            gh = Mock()
            with self.subTest(expected=expected), self.assertRaises(rc.ReleaseError):
                rc.download_asset(
                    gh, 1, self.output, expected=expected, budget=self.budget
                )
            gh.download_asset.assert_not_called()
            self.assertFalse(self.output.exists())
        for maximum in [None, -1, True]:
            with self.subTest(maximum=maximum), self.assertRaises(rc.ReleaseError):
                rc.DownloadBudget(maximum)

    def test_existing_file_and_symlink_are_preserved(self):
        self.output.write_bytes(b"keep")
        gh = Mock()
        with self.assertRaises(FileExistsError):
            self.download(gh)
        self.assertEqual(self.output.read_bytes(), b"keep")
        gh.download_asset.assert_not_called()
        self.output.unlink()
        target = self.root / "target"
        target.write_bytes(b"target")
        self.output.symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.download(gh)
        self.assertEqual(target.read_bytes(), b"target")
        self.assertTrue(self.output.is_symlink())

    def test_replaced_destination_is_not_deleted_during_failure_cleanup(self):
        def replace(_path, sink):
            sink.write(b"pay")
            replacement = self.root / "replacement"
            replacement.write_bytes(b"keep replacement")
            replacement.replace(self.output)
            raise RuntimeError("synthetic interruption")

        with self.assertRaises(RuntimeError):
            self.download(Mock(download_asset=replace))
        self.assertEqual(self.output.read_bytes(), b"keep replacement")

    def test_disk_reserve_checked_before_open_and_between_writes(self):
        free = shutil._ntuple_diskusage(100, 84, 16)
        with (
            patch.object(rc.shutil, "disk_usage", return_value=free),
            self.assertRaisesRegex(rc.ReleaseError, "disk reserve"),
        ):
            self.download(Mock())
        self.assertFalse(self.output.exists())
        with patch.object(
            rc.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100, 0, 100)
        ) as disk:

            def stream(_path, sink):
                sink.write(b"pay")
                disk.return_value = shutil._ntuple_diskusage(100, 89, 11)
                with self.assertRaises(rc.ReleaseError):
                    sink.write(b"load")
                self.assertEqual(self.output.stat().st_size, 3)
                raise RuntimeError("abort incomplete stream")

            with self.assertRaises(RuntimeError):
                self.download(Mock(download_asset=stream))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.budget.written, 3)

    def test_silent_and_stderr_heavy_timeout_reap_and_clean(self):
        for prelude in ["", "os.write(2,b'x'*131072);"]:
            with (
                self.subTest(prelude=prelude),
                self.fixture_process("import os,time;" + prelude + "time.sleep(20)"),
                patch.object(rc, "ASSET_DOWNLOAD_TIMEOUT", 0.15),
            ):
                started = time.monotonic()
                with self.assertRaisesRegex(rc.GitHubError, "asset download exceeded"):
                    self.download(rc.GitHub("test/repo"))
                self.assertLess(time.monotonic() - started, 3)
            self.assertFalse(self.output.exists())
            self.assert_reaped()

    def test_expected_length_without_eof_still_times_out(self):
        with (
            self.fixture_process(
                "import os,time;os.write(1,b'payload');time.sleep(20)"
            ),
            patch.object(rc, "ASSET_DOWNLOAD_TIMEOUT", 0.15),
            self.assertRaises(rc.GitHubError),
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertFalse(self.output.exists())
        self.assert_reaped()

    def test_cancellation_reaps_transfer_and_removes_partial_output(self):
        with (
            self.fixture_process(
                "import os,time;os.write(1,b'payload');time.sleep(20)"
            ),
            patch.object(rc._AssetWriter, "write", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertFalse(self.output.exists())
        self.assert_reaped()

    def test_stderr_is_memory_bounded_and_nonzero_exit_rejects_payload(self):
        with (
            self.fixture_process(
                "import os;os.write(2,b'x'*131072);raise SystemExit(1)"
            ),
            self.assertRaises(rc.GitHubError) as error,
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertLess(len(str(error.exception)), 66000)
        self.assertFalse(self.output.exists())
        self.assert_reaped()

    def test_disk_drop_during_silent_transfer_stops_and_reaps(self):
        checks = 0

        def space(_directory):
            nonlocal checks
            checks += 1
            return shutil._ntuple_diskusage(100, 0, 100 if checks < 3 else 9)

        with (
            self.fixture_process("import time;time.sleep(20)"),
            patch.object(rc.shutil, "disk_usage", side_effect=space),
            self.assertRaisesRegex(rc.ReleaseError, "disk reserve"),
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertFalse(self.output.exists())
        self.assert_reaped()

    def test_closed_pipes_without_child_exit_still_check_disk_floor(self):
        calls = 0

        def disk(_path):
            nonlocal calls
            calls += 1
            return shutil._ntuple_diskusage(100, 0, 100 if calls < 8 else 9)

        with (
            self.fixture_process(
                "import os,time;os.close(1);os.close(2);time.sleep(20)"
            ),
            patch.object(rc.shutil, "disk_usage", side_effect=disk),
            self.assertRaisesRegex(rc.ReleaseError, "disk reserve"),
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertFalse(self.output.exists())
        self.assert_reaped()

    def test_upload_readbacks_share_inventory_budget(self):
        budget = rc.DownloadBudget(8)
        gh = self.transport(b"1234")
        first = self.root / "first"
        first.write_bytes(b"1234")
        item = {"id": 1, "state": "uploaded", "size": 4}
        rc.verify_uploaded_asset(gh, item, first, budget=budget)
        rc.verify_uploaded_asset(gh, item, first, budget=budget)
        self.assertEqual(budget.written, 8)
        with self.assertRaisesRegex(rc.ReleaseError, "byte budget"):
            rc.verify_uploaded_asset(gh, item, first, budget=budget)
        self.assertEqual(first.read_bytes(), b"1234")

    def test_truncated_real_child_and_cancelled_reader_threads_are_cleaned(self):
        original = rc.threading.Thread
        readers = []

        def thread(*args, **kwargs):
            reader = original(*args, **kwargs)
            readers.append(reader)
            return reader

        with (
            self.fixture_process("import os;os.write(1,b'pay')"),
            patch.object(rc.threading, "Thread", side_effect=thread),
            self.assertRaises(rc.ReleaseError),
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertFalse(self.output.exists())
        self.assert_reaped()
        self.assertEqual(len(readers), 2)
        self.assertTrue(all(not reader.is_alive() for reader in readers))

    def test_direct_transport_cannot_bypass_expected_identity_and_budget(self):
        with (
            self.output.open("wb") as output,
            patch.object(rc.subprocess, "Popen") as child,
            self.assertRaises(rc.ReleaseError),
        ):
            rc.GitHub("test/repo").download_asset("releases/assets/1", output)
        child.assert_not_called()


if __name__ == "__main__":
    unittest.main()
