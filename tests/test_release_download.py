"""Small offline streams prove download bounds and subprocess cleanup."""

from __future__ import annotations

import argparse
from pathlib import Path
import os
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

    def fixture_processes(self, *programs):
        """Substitute only gh with a fixed sequence of tiny offline children."""
        original = subprocess.Popen
        remaining = iter(programs)

        def start(command, **options):
            self.assertEqual(command[:3], ["gh", "api", "--hostname"])
            self.assertEqual(command[command.index("--method") + 1], "GET")
            self.assertEqual(command[-1], "repos/test/repo/releases/assets/1")
            self.assertTrue(all(child.poll() is not None for child in self.children))
            process = original([sys.executable, "-c", next(remaining)], **options)
            self.children.append(process)
            return process

        return patch.object(rc.subprocess, "Popen", side_effect=start)

    @staticmethod
    def failed_stream(stderr=b"gh: unexpected end of JSON input", code=1):
        return subprocess.CompletedProcess([], code, b"", stderr)

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

    def test_partial_eof_retry_restarts_bytes_and_keeps_failed_debit(self):
        budget = rc.DownloadBudget(10)
        with (
            self.fixture_processes(
                "import os;os.write(1,b'pay');os.write(2,b'gh: unexpected EOF');raise SystemExit(1)",
                "import os;os.write(1,b'payload')",
            ),
            patch.object(rc.time, "sleep"),
        ):
            result = self.download(rc.GitHub("test/repo"), budget=budget)
        self.assertEqual(result, self.identity(b"payload"))
        self.assertEqual(self.output.read_bytes(), b"payload")
        self.assertEqual(budget.written, 10)
        self.assertEqual(len(self.children), 2)
        self.assert_reaped()

    def test_zero_byte_json_eof_retry_returns_exact_payload(self):
        # A synthetic zero-byte case, not an assertion about the reported outage.
        budget = rc.DownloadBudget(7)
        with (
            self.fixture_processes(
                "import os;os.write(2,b'gh: unexpected end of JSON input\\n');raise SystemExit(1)",
                "import os;os.write(1,b'payload')",
            ),
            patch.object(rc.time, "sleep"),
        ):
            result = self.download(rc.GitHub("test/repo"), budget=budget)
        self.assertEqual(result, self.identity(b"payload"))
        self.assertEqual(self.output.read_bytes(), b"payload")
        self.assertEqual(budget.written, 7)
        self.assertEqual(len(self.children), 2)
        self.assert_reaped()

    def test_retry_exhaustion_cleans_only_its_output_and_keeps_all_debits(self):
        sibling = self.root / "unrelated"
        sibling.write_bytes(b"keep")

        def fail(_command, sink, **_options):
            sink.write(b"p")
            return self.failed_stream()

        with (
            patch.object(rc, "_stream_asset", side_effect=fail) as stream,
            patch.object(rc.time, "sleep") as sleep,
            self.assertRaises(rc.GitHubError),
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertEqual(stream.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.budget.written, 3)
        self.assertFalse(self.output.exists())
        self.assertEqual(sibling.read_bytes(), b"keep")

    def test_partial_retry_cannot_widen_operator_budget_or_start_second_request(self):
        budget = rc.DownloadBudget(9)

        def fail(_command, sink, **_options):
            sink.write(b"pay")
            return self.failed_stream()

        with (
            patch.object(rc, "_stream_asset", side_effect=fail) as stream,
            patch.object(rc.time, "sleep"),
            self.assertRaisesRegex(rc.ReleaseError, "byte budget"),
        ):
            self.download(rc.GitHub("test/repo"), budget=budget)
        self.assertEqual(stream.call_count, 1)
        self.assertEqual(budget.max_bytes, 9)
        self.assertEqual(budget.written, 3)
        self.assertFalse(self.output.exists())

    def test_asset_denials_veto_retry_even_with_eof_diagnostic(self):
        messages = [
            b"gh: HTTP 403\nunexpected EOF",
            b"gh: HTTP 404\nunexpected end of JSON input",
            b"gh: HTTP 429\nunexpected EOF",
            b"gh: HTTP 400\nunexpected EOF",
            b"gh: credentials unavailable\nunexpected EOF",
            b"gh: authentication required\nunexpected end of JSON input",
            b"unexpected EOF\n" + b"x" * 65522,  # Captured stderr hit its limit.
        ]
        for message in messages:
            with (
                self.subTest(message=message[:80]),
                patch.object(rc, "_stream_asset", return_value=self.failed_stream(message)) as stream,
                patch.object(rc.time, "sleep") as sleep,
                self.assertRaises(rc.GitHubError),
            ):
                self.download(rc.GitHub("test/repo"))
            self.assertEqual(stream.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_partial_asset_cli_timeouts_are_terminal_even_with_eof(self):
        messages = (
            b"gh: i/o timeout",
            b"gh: TLS handshake timeout",
            b"gh: context deadline exceeded",
            b"gh: Client.Timeout exceeded",
        )
        for message in messages:
            for suffix in (b"", b"\nunexpected EOF"):
                budget = rc.DownloadBudget(21)

                def fail(command, sink, **_options):
                    self.assertEqual(command[command.index("--method") + 1], "GET")
                    sink.write(b"pay")
                    return self.failed_stream(message + suffix)

                with (
                    self.subTest(message=message, suffix=suffix),
                    patch.object(rc, "_stream_asset", side_effect=fail) as stream,
                    patch.object(rc.time, "sleep") as sleep,
                    self.assertRaises(rc.GitHubError),
                ):
                    self.download(rc.GitHub("test/repo"), budget=budget)
                self.assertEqual(stream.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(budget.written, 3)
                self.assertEqual(budget.max_bytes, 21)
                self.assertFalse(self.output.exists())

    def test_unknown_exit_or_diagnostic_and_successful_short_body_are_not_retried(self):
        cases = [
            (2, b"gh: unexpected end of JSON input"),
            (-9, b"unexpected EOF"),
            (1, b"unrecognized failure"),
            (1, b"another error: unexpected end of JSON input"),
            (0, b"gh: unexpected end of JSON input"),
        ]
        for code, message in cases:
            with (
                self.subTest(code=code, message=message),
                patch.object(rc, "_stream_asset", return_value=self.failed_stream(message, code)) as stream,
                patch.object(rc.time, "sleep") as sleep,
                self.assertRaises(rc.ReleaseError),
            ):
                self.download(rc.GitHub("test/repo"))
            self.assertEqual(stream.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_retry_preserves_replaced_file_and_symlink_without_second_request(self):
        for symlink in (False, True):
            backup = self.root / "original-owned-output"
            target = self.root / "replacement-target"
            target.write_bytes(b"foreign bytes")

            def replace(_command, sink, **_options):
                sink.write(b"p")
                self.output.replace(backup)
                if symlink:
                    self.output.symlink_to(target)
                else:
                    self.output.write_bytes(b"foreign bytes")
                return self.failed_stream()

            with (
                self.subTest(symlink=symlink),
                patch.object(rc, "_stream_asset", side_effect=replace) as stream,
                patch.object(rc.time, "sleep"),
                self.assertRaisesRegex(rc.ReleaseError, "destination changed"),
            ):
                self.download(rc.GitHub("test/repo"))
            self.assertEqual(stream.call_count, 1)
            self.assertEqual(self.output.read_bytes(), b"foreign bytes")
            self.assertEqual(self.output.is_symlink(), symlink)
            self.assertEqual(target.read_bytes(), b"foreign bytes")
            self.assertEqual(backup.read_bytes(), b"p")
            self.output.unlink()
            backup.unlink()

    def test_all_stream_attempts_share_original_absolute_deadline(self):
        clock = [100.0]
        deadlines = []

        def stream(_command, sink, *, deadline=None):
            deadlines.append(deadline)
            clock[0] += 2
            if len(deadlines) == 1:
                sink.write(b"p")
                return self.failed_stream()
            sink.write(b"payload")
            return subprocess.CompletedProcess([], 0, b"", b"")

        def sleep(delay):
            clock[0] += delay

        with (
            patch.object(rc, "ASSET_DOWNLOAD_TIMEOUT", 10),
            patch.object(rc.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(rc.random, "uniform", return_value=1),
            patch.object(rc.time, "sleep", side_effect=sleep),
            patch.object(rc, "_stream_asset", side_effect=stream),
        ):
            self.assertEqual(self.download(rc.GitHub("test/repo")), self.identity(b"payload"))
        self.assertEqual(deadlines, [110.0, 110.0])
        self.assertEqual(clock[0], 105.0)
        self.assertEqual(self.budget.written, 8)

    def test_exhausted_deadline_and_stream_timeout_never_start_another_attempt(self):
        for timeout in (False, True):
            clock = [100.0]

            def fail(command, sink, *, deadline=None):
                self.assertEqual(deadline, 110.0)
                sink.write(b"p")
                if timeout:
                    raise subprocess.TimeoutExpired(command, 10)
                clock[0] = 109.5
                return self.failed_stream()

            with (
                self.subTest(timeout=timeout),
                patch.object(rc, "ASSET_DOWNLOAD_TIMEOUT", 10),
                patch.object(rc.time, "monotonic", side_effect=lambda: clock[0]),
                patch.object(rc.random, "uniform", return_value=1),
                patch.object(rc.time, "sleep") as sleep,
                patch.object(rc, "_stream_asset", side_effect=fail) as stream,
                self.assertRaisesRegex(rc.GitHubError, "asset download exceeded"),
            ):
                self.download(rc.GitHub("test/repo"))
            self.assertEqual(stream.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse(self.output.exists())

    def test_cancellation_during_retry_reaps_both_children_and_readers(self):
        original_write = rc._AssetWriter.write
        original_thread = rc.threading.Thread
        calls = 0
        readers = []

        def write(sink, chunk):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return original_write(sink, chunk)

        def thread(*args, **kwargs):
            reader = original_thread(*args, **kwargs)
            readers.append(reader)
            return reader

        with (
            self.fixture_processes(
                "import os;os.write(1,b'p');os.write(2,b'unexpected EOF');raise SystemExit(1)",
                "import os,time;os.write(1,b'payload');time.sleep(20)",
            ),
            patch.object(rc.time, "sleep"),
            patch.object(rc._AssetWriter, "write", write),
            patch.object(rc.threading, "Thread", side_effect=thread),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.download(rc.GitHub("test/repo"))
        self.assertFalse(self.output.exists())
        self.assertEqual(self.budget.written, 1)
        self.assertEqual(len(self.children), 2)
        self.assert_reaped()
        self.assertEqual(len(readers), 4)
        self.assertTrue(all(not reader.is_alive() for reader in readers))

    def test_json_eof_retry_does_not_extend_metadata_or_mutation_retry_policy(self):
        cases = [
            ("GET", "git/ref/tags/v1.2.3", None),
            ("POST", "releases", {"tag_name": "v1.2.3"}),
            ("PATCH", "releases/42", {"draft": False}),
            ("PUT", "contents/release-version-state.json", {"branch": "release-version-state"}),
        ]
        for method, path, body in cases:
            with (
                self.subTest(method=method),
                patch.object(rc.subprocess, "run", return_value=self.failed_stream()) as run,
                patch.object(rc.time, "sleep") as sleep,
                self.assertRaises(rc.GitHubError),
            ):
                rc.GitHub("test/repo").api(path, method, body)
            self.assertEqual(run.call_count, 1)
            command = run.call_args.args[0]
            self.assertEqual(command[command.index("--method") + 1], method)
            self.assertEqual(command[-1], "repos/test/repo/" + path)
            sleep.assert_not_called()


class PublisherStreamRetryTests(unittest.TestCase):
    """Real publisher/readback/retry with the existing offline control-plane fake."""

    def setUp(self):
        # The sibling deliberately loads a separate release_control instance.
        # Use it consistently for its exception classes, writers and GitHub fake.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        self.addCleanup(sys.path.pop, 0)
        import test_release_control as fixtures  # pylint: disable=import-outside-toplevel

        self.fixtures = fixtures
        self.rc = fixtures.rc
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.data = {"a.bin": b"first", "m.bin": b"middle", "z.bin": b"last"}
        for name, data in self.data.items():
            (self.directory / name).write_bytes(data)
        self.enterContext(patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}))

    def publisher(self):
        gh = self.fixtures.FakeGitHub()
        # Leave all metadata/upload operations on the fake, but do actual
        # streamed GET retry and actual publisher-owned budget validation.
        gh.download_asset = self.rc.GitHub(self.fixtures.REPO).download_asset
        return gh

    def publish(self, gh):
        return self.rc._publish_prepared(
            gh, "v2.0.0-beta.1", self.fixtures.SHA, self.directory, True,
            "Already prepared source-bound release notes",
        )

    def promotion(self):
        """Extend the existing qualified RC fixture to a three-file inventory."""
        gh = self.publisher()
        manifest = self.fixtures.manifest()
        manifest["assets"] = [
            {"name": name, "size": len(data), "sha256": self.rc.digest(data)}
            for name, data in self.data.items()
        ]
        gh.files = {20 + index: data for index, data in enumerate(self.data.values())}
        gh.assets[10] = [
            {"id": 20 + index, "name": name, "size": len(data), "state": "uploaded"}
            for index, (name, data) in enumerate(self.data.items())
        ]
        raw = self.rc.json_bytes(manifest)
        gh.files[23] = raw
        gh.assets[10].append({"id": 23, "name": self.rc.MANIFEST, "size": len(raw), "state": "uploaded"})
        gh.set_evidence(raw)
        event = self.directory / "event.json"
        event.write_bytes(self.rc.json_bytes({"inputs": {"channel": "stable"}}))
        self.enterContext(patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": self.fixtures.REPO,
            "GITHUB_RUN_ID": "99",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW_REF": f"{self.fixtures.REPO}/{self.rc.WORKFLOW}@refs/heads/main",
            "GITHUB_EVENT_PATH": str(event),
        }))
        self.enterContext(patch.object(self.rc, "checked_out_sha", return_value=self.fixtures.SHA))
        self.enterContext(patch.object(self.rc, "GitHub", return_value=gh))
        args = argparse.Namespace(repo=self.fixtures.REPO, rc=self.fixtures.RC_TAG, run_id="99")
        return gh, args

    def test_publisher_retries_first_and_last_assets_with_one_charged_inventory_budget(self):
        gh = self.publisher()
        attempts = {}
        budgets = []
        verified = {}

        def stream(command, sink, **_options):
            asset_id = int(command[-1].rsplit("/", 1)[1])
            name = next(item["name"] for item in gh.assets[11] if item["id"] == asset_id)
            attempts[name] = attempts.get(name, 0) + 1
            budgets.append(sink.budget)
            data = gh.files[asset_id]
            if name in ("a.bin", "z.bin") and attempts[name] == 1:
                sink.write(data[:1])
                return subprocess.CompletedProcess([], 1, b"", b"gh: unexpected end of JSON input")
            sink.write(data)
            verified[name] = (Path(sink.name).read_bytes(), sink.identity())
            return subprocess.CompletedProcess([], 0, b"", b"")

        with (
            patch.object(self.rc, "_stream_asset", side_effect=stream),
            patch.object(self.rc.time, "sleep"),
        ):
            result = self.publish(gh)
        self.assertFalse(result["draft"])
        self.assertEqual(attempts, {"a.bin": 2, "m.bin": 1, "z.bin": 2})
        self.assertEqual(verified, {
            name: (data, {"size": len(data), "sha256": self.rc.digest(data)})
            for name, data in self.data.items()
        })
        self.assertTrue(all(budget is budgets[0] for budget in budgets))
        total = sum(map(len, self.data.values()))
        self.assertEqual(budgets[0].max_bytes, total * 3)
        self.assertEqual(budgets[0].written, total + 2)
        self.assertEqual(sum(write[1] == "PATCH" for write in gh.writes), 1)
        self.assertEqual(gh.writes[-1][1], "PATCH")

    def test_publisher_exhaustion_or_denial_keeps_draft_without_publication_patch(self):
        for denied in (False, True):
            gh = self.publisher()
            budgets = []
            paths = []

            def fail(_command, sink, **_options):
                budgets.append(sink.budget)
                paths.append(Path(sink.name))
                sink.write(b"f")
                stderr = b"gh: HTTP 403\nunexpected EOF" if denied else b"unexpected EOF"
                return subprocess.CompletedProcess([], 1, b"", stderr)

            with (
                self.subTest(denied=denied),
                patch.object(self.rc, "_stream_asset", side_effect=fail) as stream,
                patch.object(self.rc.time, "sleep"),
                self.assertRaises(self.rc.GitHubError),
            ):
                self.publish(gh)
            expected_attempts = 1 if denied else 3
            self.assertEqual(stream.call_count, expected_attempts)
            self.assertEqual(budgets[0].written, expected_attempts)
            self.assertEqual(budgets[0].max_bytes, sum(map(len, self.data.values())) * 3)
            self.assertTrue(gh.releases[11]["draft"])
            self.assertFalse(any(write[1] == "PATCH" for write in gh.writes))
            self.assertTrue(all(not path.exists() for path in paths))
            for name, data in self.data.items():
                self.assertEqual((self.directory / name).read_bytes(), data)

    def test_publisher_full_failed_attempts_exhaust_exact_inventory_allowance(self):
        gh = self.publisher()
        attempts = {}
        budgets = []
        verified = {}

        def stream(command, sink, **_options):
            asset_id = int(command[-1].rsplit("/", 1)[1])
            name = next(item["name"] for item in gh.assets[11] if item["id"] == asset_id)
            attempts[name] = attempts.get(name, 0) + 1
            budgets.append(sink.budget)
            data = gh.files[asset_id]
            sink.write(data)
            # Every attempt starts at offset zero, including full failed bodies.
            self.assertEqual(Path(sink.name).read_bytes(), data)
            if attempts[name] < 3:
                return subprocess.CompletedProcess([], 1, b"", b"unexpected EOF")
            verified[name] = sink.identity()
            return subprocess.CompletedProcess([], 0, b"", b"")

        with (
            patch.object(self.rc, "_stream_asset", side_effect=stream),
            patch.object(self.rc.time, "sleep"),
        ):
            result = self.publish(gh)
        self.assertFalse(result["draft"])
        self.assertEqual(attempts, {name: 3 for name in self.data})
        self.assertEqual(verified, {
            name: {"size": len(data), "sha256": self.rc.digest(data)}
            for name, data in self.data.items()
        })
        self.assertTrue(all(budget is budgets[0] for budget in budgets))
        allowance = 3 * sum(map(len, self.data.values()))
        self.assertEqual(budgets[0].written, allowance)
        self.assertEqual(budgets[0].max_bytes, allowance)
        self.assertEqual(sum(write[1] == "PATCH" for write in gh.writes), 1)
        self.assertEqual(gh.writes[-1][1], "PATCH")

    def test_promotion_retries_inventory_then_publishes_identical_candidate_bytes(self):
        gh, args = self.promotion()
        source_ids = {item["id"] for item in gh.assets[10] if item["name"] != self.rc.MANIFEST}
        attempts = {}
        source_budgets = []
        readback_budgets = []

        def stream(command, sink, **_options):
            asset_id = int(command[-1].rsplit("/", 1)[1])
            data = gh.files[asset_id]
            if asset_id in source_ids:
                attempts[asset_id] = attempts.get(asset_id, 0) + 1
                source_budgets.append(sink.budget)
                if asset_id in (20, 22) and attempts[asset_id] == 1:
                    sink.write(data[:1])
                    return subprocess.CompletedProcess([], 1, b"", b"unexpected EOF")
            else:
                readback_budgets.append(sink.budget)
            sink.write(data)
            return subprocess.CompletedProcess([], 0, b"", b"")

        with (
            patch.object(self.rc, "_stream_asset", side_effect=stream),
            patch.object(self.rc.time, "sleep"),
        ):
            result = self.rc.promote(args)
        self.assertEqual(result["tag"], "v1.2.3")
        self.assertEqual(attempts, {20: 2, 21: 1, 22: 2})
        source = {item["name"]: gh.files[item["id"]] for item in gh.assets[10]}
        promoted = {item["name"]: gh.files[item["id"]] for item in gh.assets[11]}
        self.assertEqual(promoted, source)
        self.assertTrue(all(budget is source_budgets[0] for budget in source_budgets))
        self.assertTrue(all(budget is readback_budgets[0] for budget in readback_budgets))
        self.assertIsNot(source_budgets[0], readback_budgets[0])
        total = sum(map(len, self.data.values()))
        self.assertEqual(source_budgets[0].max_bytes, total * 3)
        self.assertEqual(source_budgets[0].written, total + 2)
        self.assertEqual(readback_budgets[0].written, sum(map(len, source.values())))
        self.assertFalse(gh.releases[11]["draft"])
        self.assertEqual(gh.writes[-1][1], "PATCH")

    def test_promotion_exhaustion_stops_before_any_publication_write(self):
        gh, args = self.promotion()
        budgets = []
        paths = []

        def fail(_command, sink, **_options):
            budgets.append(sink.budget)
            paths.append(Path(sink.name))
            sink.write(b"f")
            return subprocess.CompletedProcess([], 1, b"", b"unexpected EOF")

        with (
            patch.object(self.rc, "_stream_asset", side_effect=fail) as stream,
            patch.object(self.rc.time, "sleep"),
            self.assertRaises(self.rc.GitHubError),
        ):
            self.rc.promote(args)
        self.assertEqual(stream.call_count, 3)
        self.assertEqual(budgets[0].written, 3)
        self.assertEqual(budgets[0].max_bytes, sum(map(len, self.data.values())) * 3)
        self.assertEqual(gh.writes, [])
        self.assertTrue(all(not path.exists() for path in paths))


if __name__ == "__main__":
    unittest.main()
