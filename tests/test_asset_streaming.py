"""Exercise bounded release payload I/O and fail-closed staging offline."""

import io
import os
# Imported only for subprocess result/exception fixtures; commands are mocked.
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_release_control import FakeGitHub, REPO, SHA, rc


class BoundedReader(io.BytesIO):
    """Reject unbounded reads even when a fixture fits in memory."""

    def read(self, size=-1):
        if not 0 < size <= rc.ASSET_CHUNK_SIZE:
            raise AssertionError(f"Unbounded payload read: {size}")
        return super().read(size)


class AssetStreamingTests(unittest.TestCase):
    """Preserve byte identity and publication boundaries with streamed assets."""

    def test_copy_and_hash_bounded_chunks_including_empty_and_partial_chunk(self):
        """All byte counts and hashes match the original payload exactly."""
        for payload in (b"", b"x", b"abc" * rc.ASSET_CHUNK_SIZE):
            with self.subTest(size=len(payload)):
                output = io.BytesIO()
                identity = rc.stream_identity(BoundedReader(payload), output)
                self.assertEqual(output.getvalue(), payload)
                self.assertEqual(
                    identity, {"size": len(payload), "sha256": rc.digest(payload)}
                )

    def test_transport_streams_to_output_with_media_type_and_timeout(self):
        """The API allowlist hands a bounded sink to the streaming pump."""
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(
                rc, "_stream_asset",
                return_value=subprocess.CompletedProcess([], 0, stderr=b""),
            ) as stream:
                rc.download_asset(
                    rc.GitHub(REPO), 21, Path(temporary) / "empty",
                    expected={"size": 0, "sha256": rc.digest(b"")},
                    budget=rc.DownloadBudget(0),
                )
            command, sink = stream.call_args.args
            self.assertIn("Accept: application/octet-stream", command)
            self.assertEqual(command[-2:], ["--", f"repos/{REPO}/releases/assets/21"])
            self.assertIsInstance(sink, rc._AssetWriter)
            self.assertEqual(rc.ASSET_DOWNLOAD_TIMEOUT, 900)

    def test_transport_rejects_non_asset_endpoints_before_launching_gh(self):
        """Streaming does not widen the repository API allowlist."""
        for path in (
            "actions/artifacts/40/zip",
            "releases/assets/0",
            "../secrets",
            "releases/assets/21?x=1",
        ):
            with self.subTest(path=path), patch.object(rc.subprocess, "Popen") as run:
                gh = rc.GitHub(REPO)
                output = io.BytesIO()
                with self.assertRaisesRegex(rc.ReleaseError, "Unsupported binary"):
                    gh.download_asset(path, output)
                run.assert_not_called()

    def test_transport_timeout_uses_the_release_error_contract(self):
        """CLI entry points retain the bounded transfer error contract."""
        with tempfile.TemporaryDirectory() as temporary, (
            patch.object(rc, "_stream_asset", side_effect=subprocess.TimeoutExpired("gh", 900))
        ), self.assertRaisesRegex(rc.GitHubError, "exceeded 900 seconds"):
            rc.download_asset(
                rc.GitHub(REPO), 21, Path(temporary) / "empty",
                expected={"size": 0, "sha256": rc.digest(b"")},
                budget=rc.DownloadBudget(0),
            )

    def test_successful_download_returns_identity_of_the_actual_staged_bytes(self):
        """Callers validate disk bytes, independently of GitHub metadata digests."""
        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "app.zip"
            result = rc.download_asset(
                gh, 21, destination,
                expected={"size": len(gh.files[21]), "sha256": rc.digest(gh.files[21])},
                budget=rc.DownloadBudget(len(gh.files[21])),
            )
            self.assertEqual(destination.read_bytes(), gh.files[21])
            self.assertEqual(
                result, {"size": len(gh.files[21]), "sha256": rc.digest(gh.files[21])}
            )

    @unittest.skipUnless(os.name == "posix", "the supervised asset downloader requires POSIX")
    def test_http_502_small_and_oversized_bodies_retry_before_asset_writes(self):
        """Retry explicit 502 responses without letting their bodies pollute the staged file."""
        payload = b"verified asset payload"
        for error_body in (b"upstream unavailable", b"gateway failure " * 10000):
            with self.subTest(error_body_size=len(error_body)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                gh_path, calls = self._fake_gh(
                    root,
                    [
                        (b"HTTP/2 502 Bad Gateway\r\n\r\n"
                         + error_body, 1),
                        (b"HTTP/2 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n"
                         + payload, 0),
                    ],
                )
                destination = root / "asset.bin"
                with (
                    patch.object(rc, "resolve_executable", lambda _name: str(gh_path)),
                    patch.object(rc.time, "sleep"),
                ):
                    identity = rc.download_asset(
                        rc.GitHub(REPO), 21, destination,
                        expected={"size": len(payload), "sha256": rc.digest(payload)},
                        budget=rc.DownloadBudget(len(payload)),
                    )
                self.assertEqual(calls.read_text(), "2")
                self.assertEqual(destination.read_bytes(), payload)
                self.assertEqual(identity, {"size": len(payload), "sha256": rc.digest(payload)})

    @unittest.skipUnless(os.name == "posix", "the supervised asset downloader requires POSIX")
    def test_http_4xx_is_terminal_but_success_body_identity_is_preserved(self):
        """A denial is not retried, and HTTP framing never enters the asset hash."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gh_path, calls = self._fake_gh(
                root, [(b"HTTP/2 403 Forbidden\r\n\r\nnot allowed", 1)],
            )
            destination = root / "asset.bin"
            with patch.object(rc, "resolve_executable", lambda _name: str(gh_path)):
                with self.assertRaises(rc.GitHubError):
                    rc.download_asset(
                        rc.GitHub(REPO), 21, destination,
                        expected={"size": 4, "sha256": rc.digest(b"data")},
                        budget=rc.DownloadBudget(4),
                    )
            self.assertEqual(calls.read_text(), "1")
            self.assertFalse(destination.exists())

    @unittest.skipUnless(os.name == "posix", "the supervised asset downloader requires POSIX")
    def test_http_stream_frames_the_final_redirect_response_only(self):
        """gh follows redirects; its included final 200 frame is separated from payload bytes."""
        payload = b"redirected release asset"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            gh_path, calls = self._fake_gh(
                root,
                [(b"HTTP/2 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n"
                  + payload, 0)],
            )
            destination = root / "asset.bin"
            with patch.object(rc, "resolve_executable", lambda _name: str(gh_path)):
                identity = rc.download_asset(
                    rc.GitHub(REPO), 21, destination,
                    expected={"size": len(payload), "sha256": rc.digest(payload)},
                    budget=rc.DownloadBudget(len(payload)),
                )
            self.assertEqual(calls.read_text(), "1")
            self.assertEqual(destination.read_bytes(), payload)
            self.assertEqual(identity, {"size": len(payload), "sha256": rc.digest(payload)})

    @unittest.skipUnless(os.name == "posix", "the supervised asset downloader requires POSIX")
    def test_oversized_and_truncated_http_headers_are_terminal_before_asset_writes(self):
        """Malformed framing cannot consume payload budget or be mistaken for asset bytes."""
        responses = (
            b"HTTP/2 200 OK\r\nX-Large: " + b"x" * rc.ASSET_HTTP_HEADER_LIMIT + b"\r\n\r\npayload",
            b"HTTP/2 200 OK\r\nContent-Type: application/octet-stream\r\n",
        )
        for response in responses:
            with self.subTest(response_size=len(response)), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                gh_path, calls = self._fake_gh(root, [(response, 0)])
                destination = root / "asset.bin"
                with patch.object(rc, "resolve_executable", lambda _name: str(gh_path)):
                    with self.assertRaises(rc.GitHubError):
                        rc.download_asset(
                            rc.GitHub(REPO), 21, destination,
                            expected={"size": 7, "sha256": rc.digest(b"payload")},
                            budget=rc.DownloadBudget(7),
                        )
                self.assertEqual(calls.read_text(), "1")
                self.assertFalse(destination.exists())

    @staticmethod
    def _fake_gh(root, responses):
        """Create a deterministic local gh executable and return its call ledger."""
        fake_bin = root / "bin"
        fake_bin.mkdir()
        gh_path = fake_bin / "gh"
        calls = root / "calls"
        gh_path.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys\n"
            f"counter=pathlib.Path({str(calls)!r})\n"
            "index=int(counter.read_text()) if counter.exists() else 0\n"
            "counter.write_text(str(index+1))\n"
            f"responses={responses!r}\n"
            "data,code=responses[min(index,len(responses)-1)]\n"
            "sys.stdout.buffer.write(data)\n"
            "sys.exit(code)\n"
        )
        gh_path.chmod(0o755)
        return gh_path, calls

    def test_partial_download_is_removed_on_transport_error_timeout_or_interrupt(self):
        """No failed payload remains eligible for a subsequent upload."""
        errors = (
            rc.GitHubError("HTTP 500"),
            subprocess.TimeoutExpired("gh", 900),
            KeyboardInterrupt(),
        )
        for error in errors:
            with (
                self.subTest(error=type(error).__name__),
                tempfile.TemporaryDirectory() as temp,
            ):
                destination = Path(temp) / "app.zip"

                def fail(_path, output, download_error=error):
                    output.write(b"partial payload")
                    raise download_error

                gh = Mock(download_asset=Mock(side_effect=fail))
                with self.assertRaises(type(error)):
                    rc.download_asset(
                        gh, 21, destination,
                        expected={"size": 15, "sha256": rc.digest(b"partial payload")},
                        budget=rc.DownloadBudget(15),
                    )
                self.assertFalse(destination.exists())

    def test_existing_file_and_symlink_are_never_overwritten_or_removed(self):
        """Exclusive creation preserves foreign files even when download fails."""
        with tempfile.TemporaryDirectory() as temp:
            existing = Path(temp) / "existing.zip"
            existing.write_bytes(b"keep")
            link = Path(temp) / "link.zip"
            link.symlink_to(existing)
            gh = Mock()
            for destination in (existing, link):
                with self.subTest(path=destination), self.assertRaises(FileExistsError):
                    rc.download_asset(
                        gh, 21, destination,
                        expected={"size": 15, "sha256": rc.digest(b"partial payload")},
                        budget=rc.DownloadBudget(15),
                    )
                self.assertEqual(existing.read_bytes(), b"keep")
                self.assertTrue(link.is_symlink())
            gh.download_asset.assert_not_called()

    def test_truncated_uploaded_bytes_leave_draft_unpublished(self):
        """A successful HTTP response still needs the complete expected payload."""
        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            (stage / "app.zip").write_bytes(b"verified payload")

            def truncate(path, output):
                output.write(gh.binary(path)[:-1])

            with patch.object(gh, "download_asset", side_effect=truncate):
                with self.assertRaisesRegex(rc.ReleaseError, "Release asset size or checksum mismatch"):
                    rc.publish(gh, "v9.9.9", SHA, stage, False, "")
        self.assertFalse(any(write[1] == "PATCH" for write in gh.writes))
        release = next(
            item for item in gh.releases.values() if item["tag_name"] == "v9.9.9"
        )
        self.assertTrue(release["draft"])


if __name__ == "__main__":
    unittest.main()
