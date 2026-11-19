"""Offline release-contract tests. No test invokes GitHub or mutates a remote."""

# Keep the shared GitHub fake and its vendored release-contract regressions together.
# pylint: disable=too-many-lines

import argparse
import base64
import gc
import hashlib
import importlib.util
import io
import json
import os
import sys
import struct

# Subprocess calls below use argument vectors with shell=False.
import subprocess  # nosec B404
import tempfile
import types
import unittest
import weakref
import zipfile
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from itertools import product
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("release_control", ROOT / "scripts/release_control.py")
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)


def setUpModule():
    """Keep command fixtures concise while process-runner tests verify absolutes."""
    global _resolver_patch
    _resolver_patch = patch.object(rc, "resolve_executable", side_effect=lambda name: name)
    _resolver_patch.start()


def tearDownModule():
    _resolver_patch.stop()


# These fixtures model the original unsigned publication contract. Consumers may
# opt into signatures; that boundary is exercised with enforcement enabled in
# test_release_signing.py, rather than requiring Cosign/network access here.
rc.RELEASE_SIGNATURES_REQUIRED = False

REPO = "example/project"
SHA = "a" * 40
RC_TAG = "v1.2.3-rc.2"


def http_response(status=0, stdout=b"{}", error_format="gh: API failed (HTTP {status})", link=None):
    """Return a complete gh --include fixture independent of retry support."""
    headers = b"Content-Type: application/json\r\n"
    if link is not None:
        headers += b"Link: " + link + b"\r\n"
    return subprocess.CompletedProcess(
        [], bool(status), f"HTTP/2.0 {status or 200} Test\n".encode() + headers + b"\r\n" + stdout,
        error_format.format(status=status).encode() if status else b"",
    )


class MetadataReadRetryTests(unittest.TestCase):
    """Transient metadata failures cannot replay publication or change policy."""

    ERROR_FORMATS = (
        "gh: Server Error (HTTP {status})",
        "gh: Server Error (HTTP {status}) (HTTP {status})",
        "gh: HTTP {status}",
        "gh: HTTP {status}\n",
        "gh: HTTP {status}: Gateway response",
        "gh: HTTP {status} \t\r\n",
        "gh: HTTP {status}: Gateway response\r\n",
    )

    response = staticmethod(http_response)

    def failed_request(self, message, method="GET", path="git/ref/tags/v1.2.3", status=403):
        """A lookup or write failure must come from the expected API request."""
        def run(command, **_kwargs):
            self.assertEqual(command[:2], ["gh", "api"])
            self.assertEqual(command[command.index("--method") + 1], method)
            self.assertEqual(command[-2:], ["--", f"repos/{REPO}/{path}"])
            return subprocess.CompletedProcess(command, 1, self.response(status).stdout, message)
        return run

    def assert_read_output_limit_is_redacted_and_not_retried(self, callback):
        gh = rc.GitHub(REPO)
        with (
            patch.object(rc, "run_process", side_effect=rc.ProcessOutputLimit("private captured bytes")) as run,
            patch.object(rc.time, "sleep") as sleep,
            self.assertRaisesRegex(rc.GitHubError, "response exceeded output limit") as caught,
        ):
            callback(gh)
        run.assert_called_once()
        sleep.assert_not_called()
        self.assertNotIn("private captured bytes", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_output_limited_api_read_becomes_redacted_nonretryable_github_error(self):
        self.assert_read_output_limit_is_redacted_and_not_retried(
            lambda gh: gh.api("releases/7")
        )

    def test_output_limited_pages_read_becomes_redacted_nonretryable_github_error(self):
        self.assert_read_output_limit_is_redacted_and_not_retried(
            lambda gh: gh.pages("releases")
        )

    def test_output_limited_binary_read_becomes_redacted_nonretryable_github_error(self):
        self.assert_read_output_limit_is_redacted_and_not_retried(
            lambda gh: gh.binary("releases/assets/7")
        )

    def test_cli_reports_redacted_bounded_read_failure(self):
        stderr = io.StringIO()
        with (
            patch.object(rc, "run_process", side_effect=rc.ProcessOutputLimit("private captured bytes")) as run,
            patch.object(rc.sys, "stderr", stderr),
        ):
            result = rc.main(["next", "--repo", REPO, "--channel", "beta", "--version", "1.2.3"])
        self.assertEqual(result, 1)
        run.assert_called_once()
        self.assertIn("response exceeded output limit", stderr.getvalue())
        self.assertNotIn("private captured bytes", stderr.getvalue())

    def test_transient_read_retries_and_discards_partial_pages(self):
        for status, error_format in product((500, 502, 503, 504), self.ERROR_FORMATS):
            partial = self.response(
                stdout=b'[{"partial": true}]',
                link=b'<https://api.github.com/repos/example/project/releases?per_page=100&page=2>; rel="next"',
            ).stdout
            failed = self.response(status, b'{"errors":"HTTP 404"}', error_format)
            failed.stdout = partial + b"\n" + failed.stdout
            with (
                self.subTest(status=status, error_format=error_format),
                patch.object(rc, "run_process", side_effect=[
                    failed,
                    self.response(stdout=b'[{"id": 42}]'),
                ]) as run,
                patch.object(rc.time, "sleep") as sleep,
                patch.object(rc.random, "uniform", return_value=1.5),
            ):
                self.assertEqual(rc.GitHub(REPO).pages("releases"), [{"id": 42}])
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args_list[0], run.call_args_list[1])
                self.assertEqual(run.call_args.kwargs["timeout"], 30)
                sleep.assert_called_once_with(1.5)

    def test_exhausted_reads_fail_without_becoming_missing(self):
        gh = rc.GitHub(REPO)
        with (
            patch.object(rc, "run_process", return_value=self.response(502)) as run,
            patch.object(rc.time, "sleep") as sleep,
            self.assertRaises(rc.GitHubError) as error,
        ):
            gh.optional("actions/runs/42")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(error.exception.not_found)
        self.assertIn("HTTP 502", str(error.exception))

    def test_only_exact_missing_status_allows_an_optional_resource(self):
        gh = rc.GitHub(REPO)
        for error_format in self.ERROR_FORMATS:
            with (
                self.subTest(error_format=error_format),
                patch.object(rc, "run_process", side_effect=self.failed_request(
                    self.response(404, error_format=error_format).stderr, status=404,
                )) as run,
                patch.object(rc.time, "sleep") as sleep,
            ):
                self.assertIsNone(gh.optional("git/ref/tags/v1.2.3"))
            run.assert_called_once()
            sleep.assert_not_called()

    def test_embedded_missing_status_cannot_authorize_an_absent_release(self):
        gh = rc.GitHub(REPO)
        cases = (
            (b"gh: Request failed: HTTP 404 from upstream (HTTP 403)", 1),
            (b"gh: HTTP 4040: invalid status", 1),
            (b"gh: HTTP 404suffix", 1),
            (b"other tool: HTTP 404", 1),
            (b"gh: HTTP 404: Gateway reported (HTTP 502)", 1),
            (b"gh: HTTP 502: Gateway reported (HTTP 404)", 1),
            (b"gh: Forbidden (HTTP 403) (HTTP 404)", 1),
            (b"gh: Not Found (HTTP 404) (HTTP 403)", 1),
            (b"gh: Untrusted message (HTTP 404) with more text", 1),
            (b"other tool: Not Found (HTTP 404)", 1),
            (b"gh: HTTP 404\ngh: HTTP 403", 1),
            (b"gh: HTTP 502: upstream returned HTTP 404", 3),
        )
        for message, attempts in cases:
            with (
                self.subTest(message=message),
                patch.object(rc, "run_process", side_effect=self.failed_request(
                    message, status=502 if attempts == 3 else 403,
                )) as run,
                patch.object(rc.time, "sleep"),
                self.assertRaises(rc.GitHubError) as error,
            ):
                rc.ensure_absent(gh, "v1.2.3")
            self.assertFalse(error.exception.not_found)
            self.assertEqual(run.call_count, attempts)

    def test_failed_write_uses_the_same_exact_missing_status(self):
        gh = rc.GitHub(REPO)
        with (
            patch.object(rc, "run_process", side_effect=self.failed_request(
                b"gh: Authorization error mentions HTTP 404 (HTTP 403)",
                method="POST", path="releases",
            )) as run,
            self.assertRaises(rc.GitHubError) as error,
        ):
            gh.api("releases", "POST", {"tag_name": RC_TAG})
        self.assertFalse(error.exception.not_found)
        run.assert_called_once()

    def test_multiline_diagnostics_cannot_override_forbidden_http_status(self):
        gh = rc.GitHub(REPO)
        for message in (
            b"gh: HTTP 404: upstream\nfailure (HTTP 502)\n",
            b"gh: HTTP 404: upstream\r\nfailure (HTTP 403)\r\n",
            b"gh: HTTP 404: upstream\rfailure (HTTP 502)\r",
            b"gh: HTTP 502: upstream\nfailure (HTTP 403)\n",
            b"gh: HTTP 404\ngh: HTTP 404\n",
            b"gh: HTTP 404\n\n",
            b"\ngh: HTTP 404\n",
            b"gh: HTTP 502\nadditional diagnostic\n",
            b"gh: Not Found\nadditional detail (HTTP 404)\n",
        ):
            for operation in (
                lambda: gh.optional("git/ref/tags/v1.2.3"),
                lambda: rc.ensure_absent(gh, "v1.2.3"),
            ):
                with self.subTest(message=message, operation=operation):
                    with (
                        patch.object(rc, "run_process", side_effect=self.failed_request(message)) as run,
                        patch.object(rc.time, "sleep") as sleep,
                        self.assertRaises(rc.GitHubError) as error,
                    ):
                        operation()
                    self.assertFalse(error.exception.not_found)
                    run.assert_called_once()
                    sleep.assert_not_called()

    def test_timeouts_are_bounded_and_do_not_expose_partial_output(self):
        gh = rc.GitHub(REPO)
        with (
            patch.object(rc, "run_process", side_effect=subprocess.TimeoutExpired(
                ["gh"], 30, output=b"private payload", stderr=b"private diagnostics"
            )) as run,
            patch.object(rc.time, "sleep") as sleep,
            patch.object(rc.random, "uniform", side_effect=lambda low, high: high),
            self.assertRaises(rc.GitHubError) as error,
        ):
            gh.api("actions/runs/42")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sum(call.args[0] for call in sleep.call_args_list), 6)
        self.assertNotIn("private", str(error.exception))
        self.assertIn("timed out after 3 attempts", str(error.exception))

    def test_policy_and_rate_limit_errors_are_not_retried(self):
        gh = rc.GitHub(REPO)
        for status, error_format in product(
            (400, 401, 403, 404, 409, 422, 429, 501), self.ERROR_FORMATS
        ):
            with (
                self.subTest(status=status, error_format=error_format),
                patch.object(rc, "run_process", return_value=self.response(
                    status, error_format=error_format
                )) as run,
                patch.object(rc.time, "sleep") as sleep,
                self.assertRaises(rc.GitHubError),
            ):
                gh.api("actions/runs/42")
            run.assert_called_once()
            sleep.assert_not_called()

    def test_unrelated_details_and_longer_status_numbers_do_not_trigger_retry(self):
        gh = rc.GitHub(REPO)
        for message in (
            b"gh: HTTP 403: Gateway reported (HTTP 502)",
            b"gh: HTTP 502: Gateway reported (HTTP 403)",
            b"gh: HTTP 502: Gateway reported (HTTP 503)",
            b"gh: Forbidden (HTTP 403) (HTTP 502)",
            b"gh: Bad Gateway (HTTP 502) (HTTP 403)",
            b"gh: Untrusted message (HTTP 502) with more text",
            b"gh: HTTP 502-not-status",
            b"gh: HTTP 502.5",
            b"gh: HTTP 5020: invalid status",
            b"gh: HTTP 502suffix",
            b"other tool: HTTP 502",
        ):
            with (
                self.subTest(message=message),
                patch.object(rc, "run_process", return_value=subprocess.CompletedProcess(
                    [], 1, self.response(403).stdout, message,
                )) as run,
                patch.object(rc.time, "sleep") as sleep,
                self.assertRaises(rc.GitHubError),
            ):
                gh.api("actions/runs/42")
            run.assert_called_once()
            sleep.assert_not_called()

    def test_writes_are_not_replayed(self):
        gh = rc.GitHub(REPO)
        cases = [
            ("releases", "POST", {"tag_name": RC_TAG}, "json"),
            ("releases/42", "PATCH", {"draft": False}, "json"),
            ("contents/release-version-state.json", "PUT", {"branch": "release-version-state"}, "json"),
        ]
        for (path, method, body, mode), error_format in product(cases, self.ERROR_FORMATS):
            with (
                self.subTest(method=method, path=path, error_format=error_format),
                patch.object(rc, "run_process", return_value=self.response(
                    502, error_format=error_format
                )) as run,
                patch.object(rc.time, "sleep") as sleep,
                self.assertRaises(rc.GitHubError),
            ):
                gh.request(path, method, body, mode)
            run.assert_called_once()
            sleep.assert_not_called()
            self.assertEqual(run.call_args.kwargs["timeout"], rc.API_READ_TIMEOUT)


class WholeReadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}))
        self.gh = rc.GitHub(REPO)

    @staticmethod
    def failed(stderr=b"unexpected EOF", stdout=b"", code=1):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    def test_retry_releases_failed_response_before_waiting_or_recapturing(self):
        def check_case(path, status, framed):
            class CapturedBytes(bytes):
                """Keep a weak-referenceable marker alive with the captured buffer."""

            class BufferLifetime:
                """Marker owned only by the captured stdout byte string."""

            previous = []

            def assert_released(*_args):
                gc.collect()
                self.assertTrue(previous[0]() is None, "failed stdout is still retained")

            def capture(*_args, **_kwargs):
                if previous:
                    assert_released()
                    return http_response(stdout=b"COMPLETE")
                payload = b"x" * (1024 * 1024)
                if framed:
                    payload = f"HTTP/2.0 {status} Test\r\n\r\n".encode() + payload
                captured = CapturedBytes(payload)
                captured.lifetime = BufferLifetime()
                previous.append(weakref.ref(captured.lifetime))
                return self.failed(stdout=captured)

            with (
                patch.object(rc, "run_process", side_effect=capture) as run,
                patch.object(rc.time, "sleep", side_effect=assert_released) as sleep,
            ):
                self.assertEqual(self.gh.binary(path), b"COMPLETE")
                self.assertEqual(run.call_count, 2)
                sleep.assert_called_once()

        for path, status, framed in (
            ("releases/assets/7", 200, True),
            ("actions/artifacts/7/zip", 503, True),
            ("releases/assets/7", 200, False),
        ):
            with self.subTest(path=path, status=status, framed=framed):
                check_case(path, status, framed)

    def test_known_transport_losses_retry_whole_metadata_response(self):
        for message in (b"unexpected EOF", b"EOF", b"connection reset by peer", b"i/o timeout",
                        b"TLS handshake timeout", b"context deadline exceeded", b"Client.Timeout exceeded",
                        b"server closed idle connection", b"use of closed network connection"):
            with self.subTest(message=message), patch.object(rc, "run_process", side_effect=[
                    self.failed(message), http_response(stdout=b'{"id":7}')]) as run, patch.object(rc.time, "sleep"):
                self.assertEqual(self.gh.api("releases/7"), {"id": 7})
                self.assertEqual(run.call_count, 2)
                self.assertTrue(all(x.kwargs["timeout"] == 30 for x in run.call_args_list))

    def test_whole_asset_and_zip_retry_discard_failed_binary_prefix(self):
        partial = self.failed(stdout=http_response(stdout=b"PARTIAL").stdout)
        payload = bytes(range(256)) + b"HTTP/2.0 404 body data"
        for path in ("releases/assets/7", "actions/artifacts/7/zip"):
            with self.subTest(path=path), patch.object(rc, "run_process", side_effect=[
                    partial, http_response(stdout=payload)]) as run, patch.object(rc.time, "sleep"):
                self.assertEqual(self.gh.binary(path), payload)
                self.assertEqual(run.call_count, 2)
                self.assertTrue(all(x.kwargs["timeout"] == 120 for x in run.call_args_list))
                self.assertEqual(run.call_args_list[0], run.call_args_list[1])
                self.assertEqual("Accept: application/octet-stream" in run.call_args.args[0], path.startswith("releases/"))

    def test_partial_binary_body_status_lines_do_not_veto_whole_retry(self):
        for path, newline, status, failure in product(
            ("releases/assets/7", "actions/artifacts/7/zip"),
            (b"\n", b"\r\n"), (401, 403, 404, 429), ("timeout", "eof"),
        ):
            with self.subTest(path=path, newline=newline, status=status, failure=failure):
                header = b"HTTP/2.0 200 OK" + newline
                partial = header + newline + b"partial binary" + newline
                partial += f"HTTP/2.0 {status} body data".encode() + newline
                result = (rc.ProcessTimeout(120, partial, b"")
                          if failure == "timeout" else self.failed(stdout=partial))
                with (patch.object(rc, "run_process", side_effect=[
                    result, http_response(stdout=b"COMPLETE")]) as run,
                    patch.object(rc.time, "sleep") as sleep,
                    patch.object(rc, "_github_http_head", wraps=rc._github_http_head) as parse_header):
                    self.assertEqual(self.gh.binary(path), b"COMPLETE")
                    if failure == "timeout":
                        self.assertEqual(parse_header.call_args_list[0].args[0], header + newline)
                    self.assertEqual(run.call_count, 2)
                    self.assertEqual(sleep.call_count, 1)
                    self.assertEqual(run.call_args_list[0], run.call_args_list[1])


    def test_timed_out_first_header_denials_still_veto_retry(self):
        for newline, status, complete_header in product(
            (b"\n", b"\r\n"), (401, 403, 404, 429), (False, True),
        ):
            with self.subTest(newline=newline, status=status, complete_header=complete_header):
                partial = f"HTTP/2.0 {status} Denied".encode() + newline
                if complete_header:
                    partial += newline + b"partial response body"
                result = subprocess.TimeoutExpired(["gh"], 30, output=partial)
                with (patch.object(rc, "run_process", side_effect=[result]) as run,
                      patch.object(rc.time, "sleep") as sleep):
                    with self.assertRaises(rc.GitHubError) as error:
                        self.gh.optional("releases/7")
                    self.assertFalse(error.exception.not_found)
                    run.assert_called_once()
                    sleep.assert_not_called()

    def test_later_partial_page_denials_still_veto_retry(self):
        for status, failure in product((401, 403, 404, 429), ("timeout", "eof")):
            with self.subTest(status=status, failure=failure):
                partial = http_response(
                    stdout=b'[{"id":1}]',
                    link=b'<https://api.github.com/repos/example/project/releases?per_page=100&page=2>; rel="next"',
                ).stdout
                partial += b"\n" + f"HTTP/2.0 {status} Denied\r\n".encode()
                result = (subprocess.TimeoutExpired(["gh"], 30, output=partial)
                          if failure == "timeout" else self.failed(stdout=partial))
                with (patch.object(rc, "run_process", side_effect=[result]) as run,
                      patch.object(rc.time, "sleep") as sleep):
                    with self.assertRaises(rc.GitHubError) as error:
                        self.gh.pages("releases")
                    self.assertFalse(error.exception.not_found)
                    run.assert_called_once()
                    sleep.assert_not_called()

    def test_escaped_paginated_json_status_text_does_not_veto_retry(self):
        for status in (401, 403, 404, 429):
            with self.subTest(status=status):
                body = json.dumps([{"text": f"body\nHTTP/2.0 {status} data"}]).encode()
                result = subprocess.TimeoutExpired(["gh"], 30, output=http_response(stdout=body).stdout)
                with (patch.object(rc, "run_process", side_effect=[
                    result, http_response(stdout=b'[{"id":2}]')]) as run,
                    patch.object(rc.time, "sleep")):
                    self.assertEqual(self.gh.pages("releases"), [{"id": 2}])
                    self.assertEqual(run.call_count, 2)

    def test_timed_out_body_status_cannot_establish_optional_absence(self):
        partial = http_response(stdout=b'partial\nHTTP/2.0 404 body data').stdout
        result = subprocess.TimeoutExpired(["gh"], 30, output=partial)
        with (patch.object(rc, "run_process", side_effect=[
            result, http_response(stdout=b'{"id":7}')]) as run,
            patch.object(rc.time, "sleep")):
            self.assertEqual(self.gh.optional("releases/7"), {"id": 7})
            self.assertEqual(run.call_count, 2)

    def test_unproven_header_boundary_keeps_conservative_denial_veto(self):
        for partial in (
            b"unknown\n\nHTTP/2.0 403 body data",
            b"HTTP/2.0 200 OK\nInvalid header\n\nHTTP/2.0 403 body data",
            b"HTTP/2.0 200 OK\nHTTP/2.0 403 partial header",
        ):
            for failure in ("timeout", "eof"):
                with self.subTest(partial=partial, failure=failure):
                    result = (subprocess.TimeoutExpired(["gh"], 120, output=partial)
                              if failure == "timeout" else self.failed(stdout=partial))
                    with (patch.object(rc, "run_process", side_effect=[result]) as run,
                          patch.object(rc.time, "sleep") as sleep):
                        with self.assertRaises(rc.GitHubError) as error:
                            self.gh.binary("releases/assets/7")
                        self.assertFalse(error.exception.not_found)
                        run.assert_called_once()
                        sleep.assert_not_called()

    def test_empty_successful_binary_is_not_a_retry_signal(self):
        for replies, expected_calls in (([http_response(stdout=b"")], 1),
                                        ([self.failed(), http_response(stdout=b"")], 2)):
            with self.subTest(expected_calls=expected_calls), patch.object(
                    rc, "run_process", side_effect=replies) as run, patch.object(rc.time, "sleep"):
                self.assertEqual(self.gh.binary("releases/assets/7"), b"")
                self.assertEqual(run.call_count, expected_calls)

    def test_repeated_asset_timeouts_are_bounded(self):
        with patch.object(rc, "run_process", side_effect=subprocess.TimeoutExpired(
                ["gh"], 120, output=b"partial")) as run, patch.object(rc.time, "sleep") as sleep:
            with self.assertRaises(rc.GitHubError):
                self.gh.binary("releases/assets/7")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(all(x.kwargs["timeout"] == 120 for x in run.call_args_list))

    def test_binary_5xx_retries_but_other_statuses_never_do(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status), patch.object(rc, "run_process", side_effect=[
                    http_response(status), http_response(stdout=b"good")]) as run, patch.object(rc.time, "sleep"):
                self.assertEqual(self.gh.binary("releases/assets/7"), b"good")
                self.assertEqual(run.call_count, 2)
        for status in (400, 401, 403, 404, 409, 422, 429, 501):
            with self.subTest(status=status), patch.object(rc, "run_process", return_value=http_response(status)) as run:
                with self.assertRaises(rc.GitHubError):
                    self.gh.binary("releases/assets/7")
                run.assert_called_once()

    def test_setup_isolates_and_restores_publication_probe_environment(self):
        """An inherited publication setting must never trigger a live probe."""
        case = type(self)()
        with (
            patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": "true"}),
            patch.object(rc.GitHub, "verify_publication_permissions", side_effect=AssertionError("live probe")) as probe,
        ):
            try:
                case.setUp()
                probe.assert_not_called()
                self.assertEqual(os.environ["RELEASE_REQUIRE_WORKFLOW_SCOPE"], "")
            finally:
                case.doCleanups()
            self.assertEqual(os.environ["RELEASE_REQUIRE_WORKFLOW_SCOPE"], "true")

    def test_auth_certificate_and_partial_denials_veto_transport_retry(self):
        for message in (b"Unauthorized; unexpected EOF", b"unauthorised; i/o timeout", b"authorization failed; EOF", b"UNAUTHORIZED; unexpected EOF", b"requires authentication; unexpected EOF", b"bad credentials; i/o timeout",
                        b"x509 certificate error; unexpected EOF", b"permission denied; EOF", b"rate limit; EOF"):
            for result in (self.failed(message), subprocess.TimeoutExpired(["gh"], 30, stderr=message)):
                with self.subTest(message=message, result=type(result).__name__), patch.object(rc, "run_process", side_effect=[result]) as run:
                    with self.assertRaises(rc.GitHubError):
                        self.gh.api("releases")
                    run.assert_called_once()
        for status in (401, 403, 404, 429):
            result = self.failed(stdout=f"HTTP/2.0 {status} Denied\r\n".encode())
            with self.subTest(status=status), patch.object(rc, "run_process", return_value=result) as run:
                with self.assertRaises(rc.GitHubError) as error:
                    self.gh.optional("releases/7")
                self.assertFalse(error.exception.not_found)
                run.assert_called_once()

    def test_successful_exit_with_malformed_content_never_retries(self):
        for result in (self.failed(code=0), http_response(stdout=b"{broken")):
            with self.subTest(result=result), patch.object(rc, "run_process", return_value=result) as run:
                with self.assertRaises(rc.ReleaseError):
                    self.gh.api("releases")
                run.assert_called_once()
        malformed = http_response(stdout=b'[]\nHTTP/2.0 200 OK\r\n\r\n{broken')
        with patch.object(rc, "run_process", return_value=malformed) as run:
            with self.assertRaises(rc.GitHubError):
                self.gh.pages("releases")
            run.assert_called_once()

    def test_unknown_cli_failures_and_success_code_5xx_never_retry(self):
        for result in (self.failed(b"unknown CLI failure"), self.failed(b"unexpected EOF", code=2),
                       self.failed(stdout=http_response(503).stdout, code=0)):
            with self.subTest(result=result), patch.object(rc, "run_process", return_value=result) as run:
                with self.assertRaises(rc.GitHubError):
                    self.gh.api("releases")
                run.assert_called_once()

    def test_transport_retry_discards_all_paginated_prefixes(self):
        partial = self.failed(stdout=http_response(stdout=b'[{"id":1}]\nHTTP/2.0 200 OK\r\n').stdout)
        with patch.object(rc, "run_process", side_effect=[partial, http_response(stdout=b'[{"id":2}]')]) as run, patch.object(rc.time, "sleep"):
            self.assertEqual(self.gh.pages("releases"), [{"id": 2}])
            self.assertEqual(run.call_count, 2)

    def test_malformed_link_on_later_503_page_does_not_hide_retryable_status(self):
        first = http_response(
            stdout=b'[{"id":1}]',
            link=b'<https://api.github.com/repos/example/project/releases?per_page=100&page=2>; rel="next"',
        )
        failed_page = http_response(503, stdout=b'{"message":"try again"}', link=b"not a link")
        partial = subprocess.CompletedProcess(
            [], 1, first.stdout + b"\n" + failed_page.stdout,
            b"gh: Server Error (HTTP 503)",
        )
        with (
            patch.object(rc, "run_process", side_effect=[partial, http_response(stdout=b'[{"id":2}]')]) as run,
            patch.object(rc.time, "sleep"),
        ):
            self.assertEqual(self.gh.pages("releases"), [{"id": 2}])
        self.assertEqual(run.call_count, 2)

    def test_execution_deadline_caps_each_metadata_get_timeout(self):
        gh = rc.GitHub(REPO)
        now = rc.time.monotonic()
        token = rc._API_READ_DEADLINE.set(now + 4)
        try:
            with (
                patch.object(rc.time, "monotonic", return_value=now),
                patch.object(rc, "run_process", side_effect=[
                    http_response(503), http_response(stdout=b'{"id":7}'),
                ]) as run,
                patch.object(rc.time, "sleep"),
            ):
                self.assertEqual(gh.api("releases/7"), {"id": 7})
            timeouts = [call.kwargs["timeout"] for call in run.call_args_list]
            self.assertEqual(len(timeouts), 2)
            for timeout in timeouts:
                self.assertAlmostEqual(timeout, 4)
        finally:
            rc._API_READ_DEADLINE.reset(token)

    def test_retry_jitter_is_capped_by_remaining_execution_deadline(self):
        gh = rc.GitHub(REPO)
        now = rc.time.monotonic()
        token = rc._API_READ_DEADLINE.set(now + 1.5)
        try:
            with (
                patch.object(rc.time, "monotonic", return_value=now),
                patch.object(rc, "run_process", side_effect=[
                    http_response(503), http_response(stdout=b'{"id":7}'),
                ]) as run,
                patch.object(rc.random, "uniform", return_value=2),
                patch.object(rc.time, "sleep") as sleep,
            ):
                self.assertEqual(gh.api("releases/7"), {"id": 7})
            timeouts = [call.kwargs["timeout"] for call in run.call_args_list]
            self.assertEqual(len(timeouts), 2)
            for timeout in timeouts:
                self.assertAlmostEqual(timeout, 1.5)
            sleep.assert_called_once()
            self.assertAlmostEqual(sleep.call_args.args[0], 1.5)
        finally:
            rc._API_READ_DEADLINE.reset(token)

    def test_expired_execution_deadline_prevents_another_get_or_retry(self):
        gh = rc.GitHub(REPO)
        token = rc._API_READ_DEADLINE.set(rc.time.monotonic() - 1)
        try:
            with patch.object(rc, "run_process") as run, self.assertRaisesRegex(
                rc.ReleaseError, "metadata deadline exceeded",
            ):
                gh.api("releases/7")
            run.assert_not_called()
        finally:
            rc._API_READ_DEADLINE.reset(token)

    def test_create_response_output_limit_is_reconciliable_ambiguity(self):
        gh = rc.GitHub(REPO)
        with patch.object(rc, "run_process", side_effect=rc.ProcessOutputLimit("too large")) as post:
            with self.assertRaises(rc.AmbiguousCreate):
                gh.create_release_once({"tag_name": RC_TAG})
        post.assert_called_once()

    def test_paginated_response_must_reach_advertised_terminal_page(self):
        first = http_response(
            stdout=b'[{"id":1}]',
            link=b'<https://api.github.com/repos/example/project/releases?per_page=100&page=2>; rel=next',
        )
        with patch.object(rc, "run_process", return_value=first):
            with self.assertRaisesRegex(rc.GitHubError, "before the advertised terminal page"):
                rc.GitHub(REPO).pages("releases")

    def test_paginated_response_accepts_unquoted_next_when_followed(self):
        first = http_response(
            stdout=b'[{"id":1}]',
            link=b'<https://api.github.com/repos/example/project/releases?per_page=100&page=2>; rel=next',
        ).stdout
        second = http_response(stdout=b'[{"id":2}]').stdout
        with patch.object(rc, "run_process", return_value=subprocess.CompletedProcess(
            [], 0, first + b"\n" + second, b"",
        )):
            self.assertEqual(rc.GitHub(REPO).pages("releases"), [{"id": 1}, {"id": 2}])

    def test_paginated_response_rejects_unadvertised_later_page(self):
        first = http_response(stdout=b'[{"id":1}]').stdout
        second = http_response(stdout=b'[{"id":2}]').stdout
        with patch.object(rc, "run_process", return_value=subprocess.CompletedProcess(
            [], 0, first + b"\n" + second, b"",
        )):
            with self.assertRaisesRegex(rc.GitHubError, "unadvertised response page"):
                rc.GitHub(REPO).pages("releases")



class AssetStreamCleanupTests(unittest.TestCase):
    """Ensure a timed-out streaming helper cannot leave pipe-holding descendants."""

    def drain_status_events(self, statuses, eof=True):
        events = rc.queue.Queue()
        for frame in statuses:
            events.put(("status", frame))
        events.put(("stdout", None))
        events.put(("stderr", None))
        if eof:
            events.put(("status", None))

        class Output:
            def __init__(self):
                self.data = bytearray()

            def check(self):
                return None

            def write(self, value):
                self.data.extend(value)

        output = Output()
        launch = types.SimpleNamespace(process=object(), control_fd=-1)
        with (
            patch.object(rc, "_kill_owned_process", return_value=None),
            patch.object(rc, "_close_asset_supervisor_control"),
        ):
            result = rc._drain_asset_stream(
                events, output, ["gh"], rc.time.monotonic() + 5, launch,
            )
        return result, output.data

    def test_asset_status_frame_accumulates_fragmented_reads(self):
        frame = struct.pack("!4sBi", b"IVSP", 1, 7)
        result, _output = self.drain_status_events((frame[:3], frame[3:]))
        self.assertEqual(result, (b"", 7))

    def test_asset_status_frame_rejects_oversized_duplicate_and_truncated(self):
        frame = struct.pack("!4sBi", b"IVSP", 1, 0)
        for label, chunks, expected in (
            ("oversized", (frame + b"x",), "oversized status frame"),
            ("duplicate", (frame, frame), "duplicate status"),
            ("truncated", (frame[:4],), "without a complete status frame"),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(rc.GitHubError, expected):
                self.drain_status_events(chunks)

    def test_asset_deadline_is_decided_before_reaping_observed_leader(self):
        class Output:
            def check(self):
                return None

        class Process:
            wait_called = False

            def wait(self, timeout=None):
                self.wait_called = True
                return 0

        process = Process()
        output = Output()
        command = ["gh"]
        with (
            patch.object(rc, "_process_has_exited", return_value=True) as observe,
            patch.object(rc.time, "monotonic", side_effect=(0, 2)),
        ):
            with self.assertRaises(subprocess.TimeoutExpired):
                rc._wait_for_asset_process(process, output, command, deadline=1)
        observe.assert_called_once_with(process)
        self.assertFalse(process.wait_called, "deadline-crossing decision reaped the leader")

    @unittest.skipUnless(os.name == "posix", "process stream groups require POSIX")
    def test_waitid_capability_is_checked_before_asset_process_launch(self):
        with tempfile.TemporaryDirectory(prefix="release-stream-capability-") as temp:
            destination = Path(temp) / "asset.bin"
            with destination.open("w+b") as output_file:
                writer = rc._AssetWriter(
                    output_file, destination,
                    {"size": 0, "sha256": hashlib.sha256(b"").hexdigest()},
                    rc.DownloadBudget(0),
                )
                with (
                    patch.object(rc, "_require_nonreaping_wait_support", side_effect=NotImplementedError),
                    patch.object(rc.subprocess, "Popen") as popen,
                ):
                    with self.assertRaises(NotImplementedError):
                        rc._stream_asset([sys.executable, "-c", "pass"], writer)
                popen.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "process stream groups require POSIX")
    def test_asset_supervisor_kills_group_before_reaping_wrapper(self):
        with tempfile.TemporaryDirectory(prefix="release-stream-order-") as temp:
            destination = Path(temp) / "asset.bin"
            events = []
            real_popen = rc.subprocess.Popen
            real_waitid = rc.os.waitid
            real_killpg = rc.os.killpg

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                wait = process.wait

                def recording_wait(*wait_args, **wait_kwargs):
                    events.append("reap")
                    return wait(*wait_args, **wait_kwargs)

                process.wait = recording_wait
                return process

            def observing_waitid(*args):
                events.append("observe")
                return real_waitid(*args)

            def recording_killpg(*args):
                events.append("killpg")
                return real_killpg(*args)

            with destination.open("w+b") as output_file:
                writer = rc._AssetWriter(
                    output_file, destination,
                    {"size": 7, "sha256": hashlib.sha256(b"payload").hexdigest()},
                    rc.DownloadBudget(7),
                )
                with (
                    patch.object(rc.subprocess, "Popen", side_effect=recording_popen),
                    patch.object(rc.os, "waitid", side_effect=observing_waitid),
                    patch.object(rc.os, "killpg", side_effect=recording_killpg),
                ):
                    result = rc._stream_asset(
                        [sys.executable, "-c", "import sys;sys.stdout.buffer.write(b'payload')"],
                        writer, deadline=rc.time.monotonic() + 5,
                    )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(destination.read_bytes(), b"payload")
            self.assertEqual(events, ["observe", "killpg", "reap"])

    def test_exited_owned_leader_is_not_killed_after_group_permission_error(self):
        class Process:
            pid = 12345

            def kill(self):
                raise AssertionError("an exited leader must not be signaled by PID")

        process = Process()
        denied = PermissionError("test permission denial")
        with (
            patch.object(rc.os, "waitid", return_value=object()),
            patch.object(rc.os, "killpg", side_effect=denied),
        ):
            self.assertIs(rc._terminate_asset_process_group(process), denied)

    @unittest.skipUnless(os.name == "posix", "process stream groups require POSIX")
    def test_group_signal_permission_error_kills_owned_leader_then_reaps(self):
        with tempfile.TemporaryDirectory(prefix="release-stream-permission-") as temp:
            destination = Path(temp) / "asset.bin"
            command = [sys.executable, "-c", "import time; time.sleep(30)"]
            events = []
            processes = []
            real_popen = rc.subprocess.Popen
            real_waitid = rc.os.waitid

            def recording_popen(*args, **kwargs):
                process = real_popen(*args, **kwargs)
                processes.append(process)
                kill = process.kill
                wait = process.wait

                def recording_kill():
                    events.append("leader-kill")
                    return kill()

                def recording_wait(*wait_args, **wait_kwargs):
                    events.append("reap")
                    return wait(*wait_args, **wait_kwargs)

                process.kill = recording_kill
                process.wait = recording_wait
                return process

            def observing_waitid(*args):
                events.append("observe")
                return real_waitid(*args)

            def denied_killpg(*_args):
                events.append("killpg-denied")
                raise PermissionError("test permission denial")

            with destination.open("w+b") as output_file:
                writer = rc._AssetWriter(
                    output_file, destination,
                    {"size": 100, "sha256": "0" * 64}, rc.DownloadBudget(100),
                )
                with (
                    patch.object(rc.subprocess, "Popen", side_effect=recording_popen),
                    patch.object(rc.os, "waitid", side_effect=observing_waitid),
                    patch.object(rc.os, "killpg", side_effect=denied_killpg),
                    self.assertRaisesRegex(rc.GitHubError, "descendants may still be running"),
                ):
                    rc._stream_asset(command, writer, deadline=rc.time.monotonic() + 0.1)

            self.assertEqual(events, [
                "observe", "killpg-denied", "observe", "leader-kill", "reap",
            ])
            self.assertEqual(len(processes), 1)
            self.assertIsNotNone(processes[0].returncode, "leader was not reaped")

    def test_group_signal_permission_error_for_exited_leader_still_reaps(self):
        class Pipe:
            def close(self):
                return None

        class Process:
            pid = 12345
            stdout = Pipe()
            stderr = Pipe()
            returncode = None

            def __init__(self):
                self.wait_timeouts = []

            def kill(self):
                raise AssertionError("an exited leader must not be signaled by PID")

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                self.returncode = 0
                return self.returncode

        class Thread:
            def __init__(self, *args, **kwargs):
                self.ident = None

            def start(self):
                self.ident = 1

            def join(self, timeout=None):
                return None

        class Output:
            def check(self):
                return None

        process = Process()
        launch = types.SimpleNamespace(
            process=process, status_stream=Pipe(), control_fd=-1,
        )
        denied = PermissionError("test permission denial")
        with (
            patch.object(rc, "_require_nonreaping_wait_support"),
            patch.object(rc, "_launch_supervised_process", return_value=launch),
            patch.object(rc.threading, "Thread", Thread),
            patch.object(rc, "_drain_asset_stream", side_effect=RuntimeError("stream failed")),
            patch.object(rc, "_process_has_exited", return_value=True),
            patch.object(rc.os, "killpg", side_effect=denied),
            self.assertRaisesRegex(rc.GitHubError, "descendants may still be running") as raised,
        ):
            rc._stream_asset(["gh"], Output(), deadline=rc.time.monotonic() + 1)

        self.assertEqual(process.wait_timeouts, [rc.CLEANUP_TIMEOUT_SECONDS])
        self.assertEqual(process.returncode, 0, "the leader was not reaped")
        self.assertIs(raised.exception.__cause__, denied, "original signal failure was obscured")

    @unittest.skipUnless(os.name == "posix", "stream process groups require POSIX")
    def test_timeout_kills_process_group_when_descendants_hold_or_close_pipes(self):
        cases = (
            (
                "descendant inherits pipes",
                "import subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
                "time.sleep(30)",
            ),
            (
                "descendant closes parent pipes",
                "import os,subprocess,sys,time; "
                "subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'], "
                "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
                "os.close(1); os.close(2); time.sleep(30)",
            ),
        )
        for name, script in cases:
            with self.subTest(scenario=name), tempfile.TemporaryDirectory(prefix="release-stream-") as temp:
                destination = Path(temp) / "asset.bin"
                command = [sys.executable, "-c", script]
                events = []
                real_popen = rc.subprocess.Popen
                real_waitid = rc.os.waitid
                real_killpg = rc.os.killpg

                def recording_popen(*args, **kwargs):
                    process = real_popen(*args, **kwargs)
                    wait = process.wait

                    def recording_wait(*wait_args, **wait_kwargs):
                        events.append("reap")
                        return wait(*wait_args, **wait_kwargs)

                    process.wait = recording_wait
                    return process

                def observing_waitid(*args):
                    events.append("observe")
                    return real_waitid(*args)

                def recording_killpg(*args):
                    events.append("killpg")
                    return real_killpg(*args)

                with destination.open("w+b") as output_file:
                    writer = rc._AssetWriter(
                        output_file, destination, {"size": 100, "sha256": "0" * 64},
                        rc.DownloadBudget(100),
                    )
                    deadline = rc.time.monotonic() + 0.3
                    with (
                        patch.object(rc.subprocess, "Popen", side_effect=recording_popen),
                        patch.object(rc.os, "waitid", side_effect=observing_waitid),
                        patch.object(rc.os, "killpg", side_effect=recording_killpg) as kill_group,
                        self.assertRaises(subprocess.TimeoutExpired),
                    ):
                        rc._stream_asset(command, writer, deadline=deadline)
                kill_group.assert_called_once()
                self.assertEqual(kill_group.call_args.args[1], rc.signal.SIGKILL)
                kill_index = events.index("killpg")
                self.assertIn("observe", events[:kill_index])
                self.assertEqual(events[kill_index + 1:], ["reap"])


class JobPaginationTests(unittest.TestCase):
    """Small job pages retain complete, fail-closed release evidence."""

    @staticmethod
    def jobs():
        jobs = [
            {"id": index, "name": f"check-{index}", "head_sha": SHA,
             "status": "completed", "conclusion": "success"}
            for index in range(1, 95)
        ]
        jobs[-1]["name"] = "Release gate"
        return jobs

    @staticmethod
    def response(jobs):
        pages = [
            http_response(
                stdout=json.dumps({"total_count": len(jobs), "jobs": jobs[start:start + 20]}).encode(),
                link=(f'<https://api.github.com/repos/{REPO}/actions/runs/17/attempts/1/jobs?per_page=20&page={index + 2}>; rel="next"'.encode()
                      if start + 20 < len(jobs) else None),
            ).stdout
            for index, start in enumerate(range(0, len(jobs), 20))
        ]
        return subprocess.CompletedProcess([], 0, b"\n".join(pages), b"")

    def test_small_pages_keep_all_jobs_and_the_last_page_gate(self):
        jobs = self.jobs()
        gh = rc.GitHub(REPO)

        def service(command, **_kwargs):
            if command[-1].endswith("per_page=100"):
                return http_response(502)
            self.assertEqual(command[-1], f"repos/{REPO}/actions/runs/17/attempts/1/jobs?per_page=20")
            self.assertIn("--paginate", command)
            return self.response(jobs)

        with patch.object(rc, "run_process", side_effect=service), patch.object(rc.time, "sleep"):
            self.assertEqual(gh.pages("actions/runs/17/attempts/1/jobs", "jobs"), jobs)
            rc.validate_run(gh, run(), {"default_branch": "main"}, SHA, 1, completed=True)

    def test_failed_last_page_gate_still_blocks_publication(self):
        jobs = self.jobs()
        jobs[-1]["conclusion"] = "failure"
        with patch.object(rc, "run_process", return_value=self.response(jobs)):
            with self.assertRaisesRegex(rc.ReleaseError, "successful, completed Release gate"):
                rc.validate_run(rc.GitHub(REPO), run(), {"default_branch": "main"}, SHA, 1, completed=True)

    def test_later_page_failure_discards_partial_jobs_before_retry(self):
        failed = http_response(502)
        failed.stdout = http_response(
            stdout=b'{"total_count":94,"jobs":[{"id":999}]}',
            link=b'<https://api.github.com/repos/example/project/actions/runs/17/attempts/1/jobs?per_page=20&page=2>; rel="next"',
        ).stdout + b"\n" + failed.stdout
        for recover in (False, True):
            responses = [failed, self.response(self.jobs())] if recover else [failed] * 3
            with self.subTest(recover=recover), patch.object(rc, "run_process", side_effect=responses) as command, patch.object(rc.time, "sleep"):
                if recover:
                    self.assertEqual(rc.GitHub(REPO).pages("actions/runs/17/attempts/1/jobs", "jobs"), self.jobs())
                else:
                    with self.assertRaises(rc.GitHubError):
                        rc.GitHub(REPO).pages("actions/runs/17/attempts/1/jobs", "jobs")
                self.assertEqual(command.call_count, len(responses))
                self.assertTrue(all(call.args[0][-1].endswith("/jobs?per_page=20") for call in command.call_args_list))

    def test_other_paginated_metadata_keeps_its_page_size(self):
        with patch.object(rc, "run_process", return_value=http_response(stdout=b"[]")) as command:
            self.assertEqual(rc.GitHub(REPO).pages("releases"), [])
        self.assertEqual(command.call_args.args[0][-1], f"repos/{REPO}/releases?per_page=100")


class AutomaticBetaPreparationTests(unittest.TestCase):
    """An occupied base skips only automatic builds, never explicit requests."""

    def test_existing_stable_tag_closes_only_push_cycle(self):
        gh = Mock()
        gh.api.return_value = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": SHA},
        }
        result = rc.closed_push_cycle(gh, "1.2.3", "push")
        self.assertEqual(result["status"], "version-required")
        self.assertEqual(result["build"], "false")
        self.assertEqual(result["version"], "1.2.3")
        gh.api.assert_called_once_with("git/ref/tags/v1.2.3")
        for kind in ("workflow_dispatch", "schedule"):
            gh.reset_mock()
            self.assertIsNone(rc.closed_push_cycle(gh, "1.2.3", kind))
            gh.api.assert_not_called()

    def test_missing_tag_continues_but_api_errors_cannot_authorize_skip(self):
        gh = rc.GitHub(REPO)
        with patch.object(gh, "api", side_effect=rc.GitHubError("HTTP 404", True)):
            self.assertIsNone(rc.closed_push_cycle(gh, "1.2.3", "push"))
        for message in ("HTTP 401", "HTTP 403", "HTTP 429", "HTTP 500"):
            with (
                self.subTest(message=message),
                patch.object(gh, "api", side_effect=rc.GitHubError(message)),
                self.assertRaisesRegex(rc.GitHubError, message),
            ):
                rc.closed_push_cycle(gh, "1.2.3", "push")

    def test_malformed_or_wrong_tag_response_fails_closed(self):
        gh = Mock()
        for ref in (
            None,
            {},
            [],
            {"ref": "refs/tags/v1.2.4", "object": {"type": "commit", "sha": SHA}},
        ):
            gh.api.return_value = ref
            with (
                self.subTest(ref=ref),
                self.assertRaisesRegex(rc.ReleaseError, "Invalid stable tag"),
            ):
                rc.closed_push_cycle(gh, "1.2.3", "push")


def policy():
    """Return a minimal release-eligible source policy."""
    return {
        "repository": REPO,
        "mode": "release",
        "default_branch": "main",
        "validation_workflows": ["ci.yml"],
    }


def policy_response(data):
    """Represent policy bytes as a hash-verified GitHub contents response."""
    raw = rc.json_bytes(data)
    blob_sha = rc.hashlib.sha1(
        b"blob " + str(len(raw)).encode() + b"\0" + raw, usedforsecurity=False
    ).hexdigest()
    return {
        "type": "file",
        "path": rc.POLICY,
        "encoding": "base64",
        "size": len(raw),
        "sha": blob_sha,
        "content": base64.b64encode(raw).decode(),
    }


def policy_snapshot(data=None):
    """Build the immutable policy snapshot stored in a release manifest."""
    data = policy() if data is None else data
    raw = rc.json_bytes(data)
    return {
        "schema": 1,
        "path": rc.POLICY,
        "git_blob_sha": policy_response(data)["sha"],
        "sha256": rc.digest(raw),
        "data": deepcopy(data),
    }


def manifest():
    """Return a valid RC manifest with one deterministic binary payload."""
    data = b"built-once-package\x00\xff"
    return {
        "schema": 1,
        "repository": REPO,
        "version": "1.2.3",
        "channel": "rc",
        "tag": RC_TAG,
        "source_sha": SHA,
        "source_policy": policy_snapshot(),
        "workflow_path": rc.WORKFLOW,
        "run_id": 17,
        "run_attempt": 1,
        "created_at": "2026-09-12T00:00:00+00:00",
        "assets": [
            {"name": "package.tar.gz", "size": len(data), "sha256": rc.digest(data)}
        ],
    }


def run(run_id=17, completed=True):
    """Return a trusted default-branch Actions run with configurable completion."""
    return {
        "id": run_id,
        "run_number": run_id,
        "repository": {"full_name": REPO},
        "head_repository": {"full_name": REPO},
        "head_sha": SHA,
        "head_branch": "main",
        "path": rc.WORKFLOW,
        "event": "workflow_dispatch",
        "run_attempt": 1,
        "status": "completed" if completed else "in_progress",
        "conclusion": "success" if completed else None,
    }


# Separate fields intentionally model independently mutable GitHub resources.
# pylint: disable-next=too-many-instance-attributes
class FakeGitHub:
    """Stateful fake with draft uploads, publication history and mutable API data."""

    repo = REPO

    def __init__(self):
        self.info = {"full_name": REPO, "default_branch": "main"}
        self.runs = {17: run(), 99: run(99, False)}
        self.default_head = SHA
        self.github_directories = {
            sha: [
                {
                    "name": "workflows",
                    "path": ".github/workflows",
                    "type": "dir",
                    "sha": "c" * 40,
                }
            ]
            for sha in (SHA, "b" * 40)
        }
        self.successors = []
        self.source_policies = {SHA: policy()}
        self.policy_reads = []
        self.jobs = [
            {
                "name": "Release gate",
                "head_sha": SHA,
                "status": "completed",
                "conclusion": "success",
            }
        ]
        self.environment = {
            "name": "release",
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "reviewers": [{"type": "User", "reviewer": {"id": 1}}],
                }
            ],
        }
        self.refs = {
            RC_TAG: {
                "ref": f"refs/tags/{RC_TAG}",
                "object": {"type": "commit", "sha": SHA},
            }
        }
        self.releases = {
            10: {
                "id": 10,
                "tag_name": RC_TAG,
                "target_commitish": SHA,
                "draft": False,
                "prerelease": True,
                "html_url": f"https://github.com/{REPO}/releases/tag/{RC_TAG}",
                "updated_at": "2026-09-12T01:00:00Z",
            }
        }
        self.files = {20: b"built-once-package\x00\xff", 21: rc.json_bytes(manifest())}
        self.assets = {
            10: [
                {
                    "id": asset_id,
                    "name": name,
                    "size": len(self.files[asset_id]),
                    "state": "uploaded",
                }
                for asset_id, name in [(20, "package.tar.gz"), (21, rc.MANIFEST)]
            ]
        }
        self.artifacts = []
        self.set_evidence(self.files[21])
        self.writes = []
        self.corrupt_upload = False
        self.mutate_snapshot = False
        self.snapshot_reads = 0

    def set_evidence(self, raw):
        """Package manifest bytes into an immutable artifact with matching metadata."""
        zipped = io.BytesIO()
        with zipfile.ZipFile(zipped, "w") as archive:
            archive.writestr(rc.MANIFEST, raw)
        self.archive = zipped.getvalue()
        self.artifacts = [
            {
                "id": 40,
                "name": "release-evidence",
                "expired": False,
                "digest": f"sha256:{rc.digest(self.archive)}",
                "workflow_run": {"id": 17, "head_sha": SHA},
            }
        ]

    # Endpoint branches mirror the REST API and make unexpected calls fail closed.
    # pylint: disable-next=too-many-return-statements,too-many-branches
    def api(self, path, method="GET", body=None):
        """Model repository reads and release writes while recording every mutation."""
        if method != "GET":
            self.writes.append((path, method, deepcopy(body)))
        if path == "":
            return deepcopy(self.info)
        if path == "git/ref/heads/main":
            return {
                "ref": "refs/heads/main",
                "object": {"type": "commit", "sha": self.default_head},
            }
        if path.startswith("actions/workflows/release-pipeline.yml/runs?"):
            return {
                "total_count": len(self.successors),
                "workflow_runs": deepcopy(self.successors),
            }
        if path.startswith("contents/.github?ref="):
            sha = path.split("?ref=", 1)[1]
            if sha not in self.github_directories:
                raise rc.GitHubError("HTTP 404", True)
            return deepcopy(self.github_directories[sha])
        if path.startswith(f"contents/{rc.POLICY}?ref="):
            sha = path.split("?ref=", 1)[1]
            self.policy_reads.append(sha)
            if sha not in self.source_policies:
                raise rc.GitHubError("HTTP 404", True)
            return policy_response(self.source_policies[sha])
        if path.startswith("actions/runs/"):
            return deepcopy(self.runs[int(path.split("/")[2])])
        if path == "environments/release":
            return deepcopy(self.environment)
        if path.startswith("compare/"):
            return {
                "status": "identical" if self.default_head == SHA else "ahead",
                "merge_base_commit": {"sha": SHA},
            }
        if path.startswith("git/ref/tags/"):
            tag = path.removeprefix("git/ref/tags/")
            if tag not in self.refs:
                raise rc.GitHubError("HTTP 404", True)
            self.snapshot_reads += 1
            if self.mutate_snapshot and self.snapshot_reads > 1:
                self.refs[tag]["object"]["sha"] = "b" * 40
            return deepcopy(self.refs[tag])
        if path.startswith("releases/tags/"):
            tag = path.removeprefix("releases/tags/")
            matches = [
                release
                for release in self.releases.values()
                if release["tag_name"] == tag and not release["draft"]
            ]
            if not matches:
                raise rc.GitHubError("HTTP 404", True)
            return deepcopy(matches[0])
        if path == "git/refs" and method == "POST":
            tag = body["ref"].removeprefix("refs/tags/")
            if tag in self.refs:
                raise rc.GitHubError("HTTP 422 already exists")
            self.refs[tag] = {
                "ref": body["ref"],
                "object": {"type": "commit", "sha": body["sha"]},
            }
            return deepcopy(self.refs[tag])
        if path == "releases" and method == "POST":
            release_id = max(self.releases) + 1
            self.releases[release_id] = {
                **body,
                "id": release_id,
                "html_url": f"https://github.com/{REPO}/releases/tag/{body['tag_name']}",
            }
            self.assets[release_id] = []
            return deepcopy(self.releases[release_id])
        if path.startswith("releases/") and method == "PATCH":
            release_id = int(path.split("/")[1])
            self.releases[release_id].update(body)
            return deepcopy(self.releases[release_id])
        raise AssertionError(f"Unexpected API call {method} {path}")

    optional = rc.GitHub.optional

    def pages(self, path, field=None):
        """Return complete in-memory endpoint collections for the release engine."""
        assert field in (None, "jobs", "artifacts")
        if path.endswith("/jobs"):
            return deepcopy(self.jobs)
        if path.endswith("/artifacts"):
            return deepcopy(self.artifacts)
        if path.startswith("releases/") and path.endswith("/assets"):
            return deepcopy(self.assets[int(path.split("/")[1])])
        if path == "releases":
            return deepcopy(list(self.releases.values()))
        if path == "tags":
            return [{"name": tag} for tag in self.refs]
        raise AssertionError(f"Unexpected pages call {path}")

    def binary(self, path):
        """Return the exact stored release asset or immutable artifact bytes."""
        if path.startswith("releases/assets/"):
            return self.files[int(path.split("/")[2])]
        if path == "actions/artifacts/40/zip":
            return self.archive
        raise AssertionError(f"Unexpected download {path}")

    def download_asset(self, path, output):
        """Write the stored bytes through the release client's streaming contract."""
        output.write(self.binary(path))

    def upload(self, tag, path, *, release_id):
        """Model asset upload and optionally corrupt its server-side bytes."""
        self.writes.append(("upload", tag, path.name))
        assert self.releases[release_id]["tag_name"] == tag
        asset_id = max(self.files) + 1
        data = path.read_bytes()
        self.files[asset_id] = b"x" * len(data) if self.corrupt_upload else data
        self.assets[release_id].append(
            {"id": asset_id, "name": path.name, "size": len(data), "state": "uploaded"}
        )


# Each public method covers an independent adversarial release condition.
# pylint: disable-next=too-many-public-methods
class ReleaseControlTests(unittest.TestCase):
    """Exercise publication sequencing and adversarial promotion inputs."""

    def setUp(self):
        # enterContext also closes the directory when a test or setup fails.
        # pylint: disable-next=consider-using-with
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.event = self.directory / "event.json"
        self.event.write_text(json.dumps({"inputs": {"channel": "stable"}}))
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_RUN_ID": "99",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_WORKFLOW_REF": f"{REPO}/{rc.WORKFLOW}@refs/heads/main",
            "GITHUB_EVENT_PATH": str(self.event),
        }
        self.enterContext(patch.dict(os.environ, environment))
        self.enterContext(patch.object(rc, "checked_out_sha", return_value=SHA))
        self.gh = FakeGitHub()
        self.enterContext(patch.object(rc, "GitHub", return_value=self.gh))
        self.args = argparse.Namespace(repo=REPO, rc=RC_TAG, run_id="99")

    def reject_promotion(self):
        """Require invalid promotion to fail before making any remote writes."""
        with self.assertRaises(rc.ReleaseError):
            rc.promote(self.args)
        self.assertEqual(
            self.gh.writes, [], "Rejected promotion performed remote writes"
        )

    def test_promote_copies_exact_candidate_bytes_then_publishes_latest(self):
        """Promote copies exact candidate bytes then publishes latest."""
        result = rc.promote(self.args)
        self.assertEqual(result["tag"], "v1.2.3")
        stable = next(
            release
            for release in self.gh.releases.values()
            if release["tag_name"] == "v1.2.3"
        )
        self.assertFalse(stable["draft"])
        self.assertFalse(stable["prerelease"])
        self.assertEqual(stable["make_latest"], "true")
        source = {
            asset["name"]: self.gh.files[asset["id"]] for asset in self.gh.assets[10]
        }
        published = {
            asset["name"]: self.gh.files[asset["id"]]
            for asset in self.gh.assets[stable["id"]]
        }
        self.assertEqual(source, published)
        self.assertEqual(
            self.gh.writes[0],
            ("git/refs", "POST", {"ref": "refs/tags/v1.2.3", "sha": SHA}),
        )
        self.assertEqual(self.gh.writes[-1][1], "PATCH")

    def test_workflow_drift_rejects_promotion_without_stranding_stable_tag(self):
        """Known historical-workflow denial must not consume the stable identity."""
        self.gh.default_head = "b" * 40
        self.gh.github_directories[self.gh.default_head][0]["sha"] = "d" * 40
        with patch.object(
            self.gh, "download_asset", wraps=self.gh.download_asset
        ) as download:
            self.reject_promotion()
        download.assert_not_called()
        self.assertNotIn("v1.2.3", self.gh.refs)

    def test_older_rc_with_identical_workflows_keeps_exact_candidate_bytes(self):
        """Unrelated source changes do not prevent promotion of an accepted RC."""
        self.gh.default_head = "b" * 40
        self.test_promote_copies_exact_candidate_bytes_then_publishes_latest()

    def test_promotion_waits_for_fresh_active_run_and_keeps_rc_bytes(self):
        """A stale post-approval status delays, rather than bypasses, promotion."""
        original_api = self.gh.api
        statuses = iter(("waiting", "queued", "in_progress", "in_progress",
                         "in_progress", "in_progress"))

        def delayed(path, method="GET", body=None):
            value = original_api(path, method, body)
            if path == "actions/runs/99":
                value["status"] = next(statuses)
            return value

        with (
            patch.object(self.gh, "api", side_effect=delayed),
            patch.object(rc.time, "sleep") as sleep,
            patch.object(rc.time, "monotonic", return_value=0),
        ):
            result = rc.promote(self.args)
        self.assertEqual(sleep.call_count, 2)
        stable = next(
            item
            for item in self.gh.releases.values()
            if item["tag_name"] == result["tag"]
        )
        expected = {a["name"]: self.gh.files[a["id"]] for a in self.gh.assets[10]}
        actual = {
            a["name"]: self.gh.files[a["id"]] for a in self.gh.assets[stable["id"]]
        }
        self.assertEqual(actual, expected)

    def active_publisher_job(self):
        """Install one exact check-run job and no Release gate."""
        self.gh.runs[99]["run_started_at"] = "2026-01-01T00:00:00Z"
        job = {
            "id": 701, "run_id": 99, "run_attempt": 1, "head_sha": SHA,
            "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/800",
            "runner_id": 900, "runner_name": "runner-example", "name": "stable publisher",
            "status": "in_progress", "conclusion": None, "completed_at": None,
            "started_at": "2026-01-01T00:00:01Z", "steps": [
                {"number": 1, "status": "in_progress", "conclusion": None,
                 "started_at": "2026-01-01T00:00:02Z", "completed_at": None}],
        }
        return job

    def test_promotion_rechecks_check_run_proof_without_a_release_gate(self):
        """The same live publisher proof is required at both callbacks, with no gate job."""
        publisher = self.active_publisher_job()
        pages = self.gh.pages
        observations = []

        def observe(path, field=None):
            if path == "actions/runs/99/attempts/1/jobs":
                observations.append(path)
                return [publisher]
            return pages(path, field)

        with patch.dict(os.environ, {
            "RELEASE_EXECUTION_CHECK_RUN_ID": "800",
            "RUNNER_NAME": "runner-example",
            "GITHUB_JOB": "stable-publisher-job",
        }), patch.object(self.gh, "pages", side_effect=observe):
            result = rc.promote(self.args)

        self.assertEqual(result["tag"], "v1.2.3")
        self.assertEqual(len(observations), 3)  # Admission, full recheck and final attempt read.

    def test_terminal_attempt_after_jobs_inventory_blocks_first_protected_write(self):
        """A terminal attempt after stale jobs readback cannot authorize a tag write."""
        publisher = self.active_publisher_job()
        pages = self.gh.pages
        api = self.gh.api
        events = []
        job_reads = 0

        def observe_api(path, method="GET", body=None):
            if path == "actions/runs/99/attempts/1":
                events.append("attempt")
            return api(path, method, body)

        def finish_during_inventory(path, field=None):
            nonlocal job_reads
            if path == "actions/runs/99/attempts/1/jobs":
                job_reads += 1
                events.append("jobs")
                result = [deepcopy(publisher)]
                if job_reads == 2:
                    # The returned paginated inventory is stale: it still says
                    # in_progress although this exact attempt just terminated.
                    self.gh.runs[99].update(
                        status="completed", conclusion="cancelled",
                    )
                return result
            return pages(path, field)

        with (
            patch.dict(os.environ, {
                "RELEASE_EXECUTION_CHECK_RUN_ID": "800",
                "RUNNER_NAME": "runner-example",
                "GITHUB_JOB": "stable-publisher-job",
            }),
            patch.object(self.gh, "api", side_effect=observe_api),
            patch.object(self.gh, "pages", side_effect=finish_during_inventory),
            self.assertRaisesRegex(rc.ReleaseError, "Execution attempt is not active"),
        ):
            rc.promote(self.args)

        self.assertEqual(job_reads, 2)  # Admission, then pre-write revalidation.
        self.assertEqual(events[-3:], ["attempt", "jobs", "attempt"])
        self.assertEqual(self.gh.writes, [])

    def test_terminal_attempt_after_aggregate_read_blocks_first_protected_write(self):
        """Legacy execution context re-reads the exact attempt after its aggregate."""
        api = self.gh.api
        aggregate_reads = 0
        events = []

        def finish_after_aggregate(path, method="GET", body=None):
            nonlocal aggregate_reads
            result = api(path, method, body)
            if path == "actions/runs/99/attempts/1":
                events.append("attempt")
            elif path == "actions/runs/99":
                aggregate_reads += 1
                events.append("aggregate")
                if aggregate_reads == 2:
                    # The aggregate response was active when read, but the
                    # exact publisher attempt terminates before the next read.
                    self.gh.runs[99].update(
                        status="completed", conclusion="cancelled",
                    )
            return result

        with (
            patch.object(self.gh, "api", side_effect=finish_after_aggregate),
            self.assertRaisesRegex(rc.ReleaseError, "Execution attempt is not active"),
        ):
            rc.promote(self.args)

        self.assertEqual(aggregate_reads, 2)  # Admission then pre-write check.
        self.assertEqual(events[-3:], ["attempt", "aggregate", "attempt"])
        self.assertEqual(self.gh.writes, [])

    def test_cancel_during_final_promotion_order_read_prevents_ledger_put(self):
        """Publisher cancellation after order reads is checked before ledger CAS."""
        from test_versioned_release import LedgerGitHub, policy as version_policy, state

        gh = LedgerGitHub()
        gh.releases.clear()
        plan = state.reserve_plan(
            gh, version_policy("promote-bytes"), "1.2.3", "rc", SHA, 99, 1,
            datetime(2026, 9, 13, tzinfo=timezone.utc), None,
        )
        state.begin_publication(gh, plan, 99)
        gh.releases[50] = {"tag_name": plan["tag"], "draft": False}
        before_writes = len(gh.ledger_writes)
        publisher = {
            "id": 100, "run_attempt": 1, "head_sha": SHA, "path": rc.WORKFLOW,
            "event": "workflow_dispatch", "status": "in_progress", "conclusion": None,
        }
        gh.runs[100] = publisher.copy()
        order_reads = 0
        events = []
        original_pages = gh.pages

        def mutate_after_final_order_read(path, field=None):
            nonlocal order_reads
            result = original_pages(path, field)
            if path == "releases":
                order_reads += 1
                events.append(f"order-read-{order_reads}")
                if order_reads == 2:
                    gh.runs[100].update(status="completed", conclusion="cancelled")
            return result

        def confirm_active_attempt():
            current = gh.api("actions/runs/100/attempts/1")
            events.append(f"publisher-{current['status']}")
            rc.require_active_attempt(current, publisher)

        with patch.object(gh, "pages", side_effect=mutate_after_final_order_read):
            with self.assertRaisesRegex(rc.ReleaseError, "not active"):
                rc._begin_promotion_publication(
                    gh, {"version_plan": plan}, 100, confirm_active_attempt,
                )

        self.assertEqual(order_reads, 2)
        self.assertEqual(events, ["order-read-1", "order-read-2", "publisher-completed"])
        self.assertEqual(len(gh.ledger_writes), before_writes)

    def test_legacy_promotion_confirms_active_publisher_before_publication(self):
        """Legacy manifests still validate the publisher before creating release state."""
        confirmed = []
        rc._begin_promotion_publication(
            self.gh, {"version_plan": None}, 100,
            lambda: confirmed.append("active"),
        )
        self.assertEqual(confirmed, ["active"])

    def test_promotion_rechecks_publisher_before_public_release_patch(self):
        """A publisher cancelled after draft upload leaves the release unpublished."""
        original_api = self.gh.api
        attempt_reads = 0
        publisher = self.active_publisher_job()
        original_pages = self.gh.pages

        def cancel_before_confirm_publish(path, method="GET", body=None):
            nonlocal attempt_reads
            if path == "actions/runs/99/attempts/1":
                attempt_reads += 1
                status = "completed" if attempt_reads >= 5 else "in_progress"
                conclusion = None if status == "in_progress" else "cancelled"
                current = original_api(path, method, body)
                current.update(status=status, conclusion=conclusion)
                return current
            return original_api(path, method, body)

        def active_job(path, field=None):
            if path == "actions/runs/99/attempts/1/jobs":
                return [publisher]
            return original_pages(path, field)

        with (
            patch.dict(os.environ, {
                "RELEASE_EXECUTION_CHECK_RUN_ID": "800",
                "RUNNER_NAME": "runner-example",
                "GITHUB_JOB": "stable-publisher-job",
                "GITHUB_REPOSITORY": REPO,
            }),
            patch.object(self.gh, "api", side_effect=cancel_before_confirm_publish),
            patch.object(self.gh, "pages", side_effect=active_job),
        ):
            with self.assertRaisesRegex(rc.ReleaseError, "not active"):
                rc.promote(self.args)

        self.assertGreaterEqual(attempt_reads, 3)
        stable = next(item for item in self.gh.releases.values()
                      if item["tag_name"] == "v1.2.3")
        self.assertTrue(stable["draft"])
        self.assertTrue(any(write[0] == "upload" for write in self.gh.writes))
        self.assertFalse(any(write[1] == "PATCH" for write in self.gh.writes))

    def test_candidate_waits_for_fresh_active_run_before_gate(self):
        """The unversioned publisher uses the same bounded execution check."""
        self.event.write_text(json.dumps({"inputs": {"channel": "rc"}}))
        assets = self.directory / "candidate"
        assets.mkdir()
        (assets / "candidate.zip").write_bytes(b"candidate-build")
        args = argparse.Namespace(
            repo=REPO,
            channel="rc",
            version="2.0.0",
            sha=SHA,
            run_id="99",
            run_attempt="1",
            sequence=None,
            assets=str(assets),
        )
        original_api = self.gh.api
        statuses = iter(("requested", "in_progress"))

        def delayed(path, method="GET", body=None):
            value = original_api(path, method, body)
            if path == "actions/runs/99":
                value["status"] = next(statuses)
            return value

        with (
            patch.object(self.gh, "api", side_effect=delayed),
            patch.object(rc.time, "sleep") as sleep,
            patch.object(rc.time, "monotonic", return_value=0),
            patch.object(rc, "EVIDENCE", self.directory / "evidence" / rc.MANIFEST),
        ):
            result = rc.candidate(args)
        self.assertEqual(sleep.call_count, 1)
        self.assertTrue(result["tag"].startswith("v2.0.0-rc."))

    def test_untrusted_source_runs_never_write(self):
        """Untrusted source runs never write."""
        cases = [
            {"conclusion": "failure"},
            {"conclusion": "skipped"},
            {"status": "in_progress"},
            {"head_sha": "b" * 40},
            {"head_branch": "feature"},
            {"event": "pull_request_target"},
            {"event": "push"},
            {"path": ".github/workflows/unrelated.yml"},
            {"run_attempt": 2},
            {"repository": {"full_name": "fork/project"}},
            {"head_repository": {"full_name": "fork/project"}},
        ]
        for change in cases:
            with self.subTest(change=change):
                self.gh.runs[17] = {**run(), **change}
                self.reject_promotion()

    def test_gate_must_explicitly_succeed_for_source_sha(self):
        """Gate must explicitly succeed for source sha."""
        valid = deepcopy(self.gh.jobs[0])
        for jobs in (
            [],
            [valid, valid],
            [{**valid, "conclusion": "skipped"}],
            [{**valid, "conclusion": "failure"}],
            [{**valid, "status": "queued"}],
            [{**valid, "head_sha": "b" * 40}],
            [{**valid, "name": "Almost release gate"}],
        ):
            with self.subTest(jobs=jobs):
                self.gh.jobs = jobs
                self.reject_promotion()

    def test_candidate_checksum_change_never_writes(self):
        """Candidate checksum change never writes."""
        self.gh.files[20] = b"different-bytes!!!!\x00"
        self.reject_promotion()

    def test_manifest_rewrite_cannot_forge_actions_evidence(self):
        """Manifest rewrite cannot forge actions evidence."""
        forged = manifest()
        forged["assets"][0]["sha256"] = "f" * 64
        self.gh.files[21] = rc.json_bytes(forged)
        self.reject_promotion()

    def test_missing_expired_duplicated_or_corrupt_evidence_never_writes(self):
        """Missing expired duplicated or corrupt evidence never writes."""
        valid = deepcopy(self.gh.artifacts[0])
        for artifacts in (
            [],
            [{**valid, "expired": True}],
            [valid, valid],
            [{**valid, "digest": None}],
            [{**valid, "digest": "sha256:" + "a" * 64}],
            [{**valid, "workflow_run": {"id": 5, "head_sha": SHA}}],
        ):
            with self.subTest(artifacts=artifacts):
                self.gh.artifacts = artifacts
                self.reject_promotion()

    def test_reviewers_are_required(self):
        """Reviewers are required."""
        for environment in (
            {"name": "release", "protection_rules": []},
            {
                "name": "release",
                "protection_rules": [{"type": "wait_timer", "wait_timer": 1}],
            },
            {
                "name": "release",
                "protection_rules": [{"type": "required_reviewers", "reviewers": []}],
            },
        ):
            with self.subTest(environment=environment):
                self.gh.environment = environment
                self.reject_promotion()

    def test_manual_stable_context_required(self):
        """Manual stable context required."""
        for key, value in (
            ("GITHUB_ACTIONS", "false"),
            ("GITHUB_RUN_ID", "17"),
            ("GITHUB_REF", "refs/heads/feature"),
            ("GITHUB_EVENT_NAME", "push"),
            ("GITHUB_WORKFLOW_REF", "wrong"),
            ("GITHUB_RUN_ATTEMPT", "2"),
        ):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                self.reject_promotion()
        self.event.write_text(json.dumps({"inputs": {"channel": "rc"}}))
        self.reject_promotion()

    def test_checkout_sha_must_match_current_run(self):
        """Checkout sha must match current run."""
        with patch.object(rc, "checked_out_sha", return_value="b" * 40):
            self.reject_promotion()

    def test_stable_tag_collision_never_overwrites(self):
        """Stable tag collision never overwrites."""
        self.gh.refs["v1.2.3"] = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": SHA},
        }
        self.reject_promotion()

    def test_draft_collision_never_overwrites(self):
        """Draft collision never overwrites."""
        self.gh.releases[11] = {"id": 11, "tag_name": "v1.2.3", "draft": True}
        self.reject_promotion()

    def test_candidate_snapshot_mutation_never_writes(self):
        """Candidate snapshot mutation never writes."""
        self.gh.mutate_snapshot = True
        self.reject_promotion()

    def test_extra_release_asset_never_writes(self):
        """Extra release asset never writes."""
        self.gh.assets[10].append(
            {"name": "surprise.txt", "id": 44, "state": "uploaded", "size": 0}
        )
        self.reject_promotion()

    def test_corrupt_upload_leaves_draft_and_does_not_publish(self):
        """Corrupt upload leaves draft and does not publish."""
        self.gh.corrupt_upload = True
        with self.assertRaisesRegex(rc.ReleaseError, "Release asset size or checksum mismatch"):
            rc.promote(self.args)
        stable = next(
            release
            for release in self.gh.releases.values()
            if release["tag_name"] == "v1.2.3"
        )
        self.assertTrue(stable["draft"])
        self.assertFalse(any(write[1] == "PATCH" for write in self.gh.writes))

    def test_candidate_channels_publish_unique_prereleases(self):
        """Candidate channels publish unique prereleases."""
        for channel in ("nightly", "beta", "rc"):
            with self.subTest(channel=channel):
                self.gh.runs[99]["status"] = "in_progress"
                self.event.write_text(json.dumps({"inputs": {"channel": channel}}))
                assets = self.directory / channel
                assets.mkdir()
                (assets / "candidate.zip").write_bytes(b"candidate-build")
                args = argparse.Namespace(
                    repo=REPO,
                    channel=channel,
                    version="2.0.0",
                    sha=SHA,
                    run_id="99",
                    run_attempt="1",
                    sequence=None,
                    assets=str(assets),
                )
                with patch.object(
                    rc, "EVIDENCE", self.directory / "evidence" / rc.MANIFEST
                ):
                    result = rc.candidate(args)
                self.assertEqual(result["status"], "published")
                released = next(
                    value
                    for value in self.gh.releases.values()
                    if value["tag_name"] == result["tag"]
                )
                self.assertTrue(released["prerelease"])
                self.assertFalse(released["draft"])
                self.assertEqual(released["make_latest"], "false")
                self.assertTrue(result["tag"].startswith(f"v2.0.0-{channel}."))

    def test_invalid_candidate_notes_preserve_existing_evidence(self):
        """Notes are a precondition for persistent evidence as well as GitHub writes."""
        self.gh.runs[99]["status"] = "in_progress"
        self.event.write_text(json.dumps({"inputs": {"channel": "beta"}}))
        assets = self.directory / "notes-assets"
        assets.mkdir()
        (assets / "candidate.zip").write_bytes(b"candidate-build")
        evidence = self.directory / "existing-evidence"
        evidence.write_bytes(b"previous successful release")
        args = argparse.Namespace(
            repo=REPO,
            channel="beta",
            version="2.0.0",
            sha=SHA,
            run_id="99",
            run_attempt="1",
            sequence=None,
            assets=str(assets),
        )
        with (
            patch.object(rc, "EVIDENCE", evidence),
            patch.object(
                rc, "release_notes", side_effect=rc.ReleaseError("missing notes")
            ),
            self.assertRaisesRegex(rc.ReleaseError, "missing notes"),
        ):
            rc.candidate(args)
        self.assertEqual(evidence.read_bytes(), b"previous successful release")
        self.assertEqual(self.gh.writes, [])

    def test_legacy_automatic_candidate_never_writes_evidence_when_superseded(self):
        """Both publication entry points apply the same early and final guard."""
        assets = self.directory / "automatic"
        assets.mkdir()
        (assets / "candidate.zip").write_bytes(b"candidate-build")
        evidence = self.directory / "evidence" / rc.MANIFEST
        args = argparse.Namespace(
            repo=REPO,
            channel="beta",
            version="2.0.0",
            sha=SHA,
            run_id="99",
            run_attempt="1",
            sequence=None,
            assets=str(assets),
        )
        self.gh.runs[99]["event"] = "push"
        os.environ["GITHUB_EVENT_NAME"] = "push"
        original = rc.stage_assets

        def advance():
            self.gh.default_head = "b" * 40
            self.gh.successors = [
                {
                    **run(100, False),
                    "head_sha": "b" * 40,
                    "event": "push",
                }
            ]

        for late in (True, False):
            self.gh.default_head = SHA

            def staged(*values):
                result = original(*values)
                advance()
                return result

            if not late:
                advance()
            with (
                patch.object(rc, "stage_assets", side_effect=staged),
                patch.object(rc, "EVIDENCE", evidence),
            ):
                result = rc.candidate(args)
            self.assertEqual(result["status"], "superseded")
            self.assertEqual(self.gh.writes, [])
            self.assertFalse(evidence.exists())

    def test_candidate_base_cannot_already_be_stable(self):
        """Candidate base cannot already be stable."""
        self.gh.refs["v2.0.0"] = {
            "ref": "refs/tags/v2.0.0",
            "object": {"type": "commit", "sha": SHA},
        }
        for channel in ("beta", "rc"):
            with self.subTest(channel=channel):
                self.event.write_text(json.dumps({"inputs": {"channel": channel}}))
                args = argparse.Namespace(
                    repo=REPO,
                    channel=channel,
                    version="2.0.0",
                    sha=SHA,
                    run_id="99",
                    run_attempt="1",
                    sequence=None,
                    assets=str(self.directory),
                )
                with self.assertRaisesRegex(rc.ReleaseError, "Tag already exists"):
                    rc.candidate(args)
                self.assertEqual(self.gh.writes, [])

    def test_rc_creation_rejects_source_policy_blockers_before_writes(self):
        """Rc creation rejects source policy blockers before writes."""
        self.event.write_text(json.dumps({"inputs": {"channel": "rc"}}))
        args = argparse.Namespace(
            repo=REPO,
            channel="rc",
            version="2.0.0",
            sha=SHA,
            run_id="99",
            run_attempt="1",
            sequence=None,
            assets=str(self.directory),
        )
        for field in ("release_blockers", "stable_blockers"):
            with self.subTest(field=field):
                self.gh.source_policies[SHA] = {
                    **policy(),
                    field: ["Integration coverage is missing"],
                }
                with self.assertRaisesRegex(rc.ReleaseError, field):
                    rc.candidate(args)
                self.assertEqual(self.gh.writes, [])

    def test_exploratory_candidates_record_blockers_without_qualifying_for_stable(self):
        """Exploratory candidates record blockers without qualifying for stable."""
        blocked = {**policy(), "stable_blockers": ["Hardware validation is missing"]}
        self.gh.source_policies[SHA] = blocked
        for channel in ("nightly", "beta"):
            with self.subTest(channel=channel):
                self.event.write_text(json.dumps({"inputs": {"channel": channel}}))
                assets = self.directory / channel
                assets.mkdir()
                (assets / "package.zip").write_bytes(b"experimental-build")
                args = argparse.Namespace(
                    repo=REPO,
                    channel=channel,
                    version="2.0.0",
                    sha=SHA,
                    run_id="99",
                    run_attempt="1",
                    sequence=None,
                    assets=str(assets),
                )
                with patch.object(
                    rc, "EVIDENCE", self.directory / "evidence" / rc.MANIFEST
                ):
                    result = rc.candidate(args)
                release_id = next(
                    key
                    for key, release in self.gh.releases.items()
                    if release["tag_name"] == result["tag"]
                )
                manifest_id = next(
                    asset["id"]
                    for asset in self.gh.assets[release_id]
                    if asset["name"] == rc.MANIFEST
                )
                recorded = json.loads(self.gh.files[manifest_id])
                self.assertEqual(recorded["source_policy"], policy_snapshot(blocked))
                with self.assertRaises(rc.ReleaseError):
                    rc.validate_policy_snapshot(recorded["source_policy"], REPO)

    def test_old_source_blockers_cannot_be_cleared_by_current_policy(self):
        """Old source blockers cannot be cleared by current policy."""
        newer_sha = "b" * 40
        self.gh.runs[99]["head_sha"] = newer_sha
        self.gh.source_policies[newer_sha] = policy()
        self.gh.source_policies[SHA] = {
            **policy(),
            "stable_blockers": ["Original source had no integration tests"],
        }
        with patch.object(rc, "checked_out_sha", return_value=newer_sha):
            self.reject_promotion()
        self.assertEqual(self.gh.policy_reads, [SHA])

    def test_policy_snapshot_content_must_match_the_original_commit(self):
        """Policy snapshot content must match the original commit."""
        self.gh.source_policies[SHA] = {
            **policy(),
            "validation_workflows": ["different-ci.yml"],
        }
        with self.assertRaisesRegex(rc.ReleaseError, "snapshot differs"):
            rc.promote(self.args)
        self.assertEqual(self.gh.writes, [])

    def test_source_mode_must_be_release(self):
        """Source mode must be release."""
        for mode in ("validation-only", None):
            with self.subTest(mode=mode):
                self.gh.source_policies[SHA] = {**policy(), "mode": mode}
                self.reject_promotion()

    def test_legacy_rc_without_immutable_policy_snapshot_is_rejected(self):
        """Legacy rc without immutable policy snapshot is rejected."""
        legacy = manifest()
        legacy.pop("source_policy")
        raw = rc.json_bytes(legacy)
        self.gh.files[21] = raw
        self.gh.assets[10][1]["size"] = len(raw)
        self.gh.set_evidence(raw)
        self.reject_promotion()


class PublicationPermissionTests(unittest.TestCase):
    """Publication credentials fail closed before any ledger or release mutation."""

    @staticmethod
    def probe(scopes="repo, workflow", **repository):
        """Return captured permission headers, never a real credential."""
        body = {"full_name": REPO, "private": False, "permissions": {"push": True}}
        body.update(repository)
        header = "HTTP/2.0 200 OK\r\nX-OAuth-Scopes: " + scopes + "\r\n\r\n"
        return subprocess.CompletedProcess([], 0, header.encode() + rc.json_bytes(body))

    def test_default_token_does_not_change_or_probe(self):
        """Consumers that did not opt in preserve their GITHUB_TOKEN behavior."""
        with (
            patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}),
            patch.object(rc, "run_process") as command,
        ):
            gh = rc.GitHub(REPO)
        self.assertFalse(gh.workflow_scope_verified)
        command.assert_not_called()

    def test_scoped_classic_token_probe_is_read_only_and_secret_safe(self):
        """Both public-only and repo scopes work without credentials in arguments."""
        for scopes in ("repo, workflow", "public_repo, workflow"):
            with (
                self.subTest(scopes=scopes),
                patch.dict(
                    os.environ,
                    {
                        # Deliberate test credential, never used for authentication.
                        "GH_TOKEN": "test-secret",  # nosec B105
                        "RELEASE_REQUIRE_WORKFLOW_SCOPE": "true",
                    },
                ),
                patch.object(
                    rc, "run_process", return_value=self.probe(scopes)
                ) as command,
            ):
                gh = rc.GitHub(REPO)
                self.assertTrue(gh.workflow_scope_verified)
                rc.check_workflow_publication(gh, SHA)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(
                command.call_args.args[0],
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    "GET",
                    "--include",
                    "--",
                    "repos/" + REPO,
                ],
            )
            self.assertNotIn("test-secret", str(command.call_args))

    def test_scope_or_repository_mismatch_rejects_before_write(self):
        """Missing scopes, fine-grained tokens and read-only access cannot publish."""
        cases = [
            self.probe("repo"),
            self.probe("workflow"),
            self.probe(""),
            self.probe("public_repo, workflow", private=True),
            self.probe(full_name="other/project"),
            self.probe(permissions={"push": False}),
        ]
        for response in cases:
            with (
                self.subTest(response=response.stdout),
                patch.dict(
                    os.environ,
                    {
                        # Deliberate test credential, never used for authentication.
                        "GH_TOKEN": "test-secret",  # nosec B105
                        "RELEASE_REQUIRE_WORKFLOW_SCOPE": "true",
                    },
                ),
                patch.object(rc, "run_process", return_value=response) as command,
                self.assertRaises(rc.ReleaseError),
            ):
                rc.GitHub(REPO)
            self.assertEqual(command.call_count, 1)
            self.assertEqual(command.call_args.args[0][5], "GET")

    def test_missing_secret_never_uses_a_fallback_identity(self):
        """An empty selected Actions secret must fail before gh can fall back."""
        with (
            patch.dict(
                # Deliberate test credential, never used for authentication.
                os.environ,
                {"GH_TOKEN": "", "RELEASE_REQUIRE_WORKFLOW_SCOPE": "true"},  # nosec B105
            ),
            patch.object(rc, "run_process") as command,
            self.assertRaises(rc.ReleaseError),
        ):
            rc.GitHub(REPO)
        command.assert_not_called()

    def test_failed_reprobe_clears_previous_workflow_authorization(self):
        """A previously scoped client cannot retain authorization after a failed probe."""
        with (
            patch.dict(
                os.environ,
                # Deliberate test credential, never used for authentication.
                {"GH_TOKEN": "test-secret", "RELEASE_REQUIRE_WORKFLOW_SCOPE": "true"},  # nosec B105
            ),
            patch.object(rc, "run_process", return_value=self.probe()) as command,
        ):
            gh = rc.GitHub(REPO)
            command.return_value = self.probe("repo")
            with self.assertRaises(rc.ReleaseError):
                gh.verify_publication_permissions()
            self.assertFalse(gh.workflow_scope_verified)

    def test_probe_failure_does_not_print_payload_or_auth_diagnostics(self):
        """The permission probe never returns raw potentially sensitive diagnostics."""
        response = subprocess.CompletedProcess(
            [], 1, b"private response", b"test-secret"
        )
        with (
            patch.dict(
                os.environ,
                # Deliberate test credential, never used for authentication.
                {"GH_TOKEN": "test-secret", "RELEASE_REQUIRE_WORKFLOW_SCOPE": "true"},  # nosec B105
            ),
            patch.object(rc, "run_process", return_value=response),
            self.assertRaises(rc.ReleaseError) as error,
        ):
            rc.GitHub(REPO)
        self.assertEqual(
            str(error.exception), "Publication token permission probe failed"
        )

    def test_api_error_identifies_method_and_route_without_body(self):
        """Future publication failures identify the operation without a debug dump."""
        with patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}):
            gh = rc.GitHub(REPO)
        with (
            patch.object(
                rc, "run_process",
                return_value=http_response(
                    403, b"private body",
                    "gh: Resource not accessible by integration (HTTP {status})",
                ),
            ),
            self.assertRaises(rc.GitHubError) as error,
        ):
            gh.api("git/refs", "POST", {"private": "request body"})
        self.assertIn("POST repos/example/project/git/refs: ", str(error.exception))
        self.assertNotIn("private", str(error.exception))


class WorkflowPublicationTests(unittest.TestCase):
    """Check historical-source permission preflight without performing writes."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.default_head = "b" * 40

    def test_current_source_does_not_download_workflow_inventories(self):
        """The common current-HEAD path needs only repository and ref metadata."""
        with patch.object(self.gh, "api", wraps=self.gh.api) as read:
            rc.check_workflow_publication(self.gh, self.gh.default_head)
        self.assertEqual(
            [call.args[0] for call in read.call_args_list], ["", "git/ref/heads/main"]
        )
        self.assertEqual(self.gh.writes, [])

    def test_workflow_directory_metadata_must_be_regular_and_unambiguous(self):
        """Missing, replaced and truncated directory inventories fail closed."""
        original = self.gh.github_directories[SHA][0]
        cases = (
            [],
            {},
            [None],
            [original, original],
            [original] * 1000,
            [{**original, "type": "symlink"}],
            [{**original, "type": "file"}],
            [{**original, "name": "other"}],
            [{**original, "path": ".github/other"}],
            [{**original, "sha": "main"}],
            [{**original, "sha": None}],
        )
        for sha in (SHA, self.gh.default_head):
            for entries in cases:
                with self.subTest(sha=sha, entries=entries):
                    self.gh.github_directories[sha] = entries
                    with self.assertRaises(rc.ReleaseError):
                        rc.check_workflow_publication(self.gh, SHA)
                    self.assertEqual(self.gh.writes, [])
            self.gh.github_directories[sha] = [original]

    def test_missing_directory_and_transport_errors_do_not_mean_equal_trees(self):
        """A missing API resource cannot stand in for an empty workflow tree."""
        original = self.gh.api
        for error in (rc.GitHubError("HTTP 404", True), rc.GitHubError("HTTP 403")):
            with self.subTest(error=str(error)):

                def failed_directory(path, method="GET", body=None, failure=error):
                    if path.startswith("contents/.github?"):
                        raise failure
                    return original(path, method, body)

                with patch.object(self.gh, "api", side_effect=failed_directory):
                    with self.assertRaises(rc.GitHubError):
                        rc.check_workflow_publication(self.gh, SHA)
                self.assertEqual(self.gh.writes, [])

    def test_branch_change_during_workflow_lookup_is_rejected(self):
        """The immutable tree comparison must still describe the fresh branch head."""
        original = self.gh.api

        def advanced(path, method="GET", body=None):
            result = original(path, method, body)
            if path == f"contents/.github?ref={'b' * 40}":
                self.gh.default_head = "d" * 40
            return result

        with patch.object(self.gh, "api", side_effect=advanced):
            with self.assertRaisesRegex(rc.ReleaseError, "changed during publication"):
                rc.check_workflow_publication(self.gh, SHA)
        self.assertEqual(self.gh.writes, [])

    def test_environment_flag_alone_does_not_authorize_historical_workflows(self):
        """Only the successful token probe can grant the existing exception."""
        self.gh.github_directories[self.gh.default_head][0]["sha"] = "d" * 40
        with patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": "true"}):
            with self.assertRaisesRegex(rc.ReleaseError, "workflows differ"):
                rc.check_workflow_publication(self.gh, SHA)
        self.assertEqual(self.gh.writes, [])

    def test_publish_boundary_rechecks_before_creating_a_tag(self):
        """A branch advance after the earlier check cannot strand a public tag."""
        rc.check_workflow_publication(self.gh, SHA)
        self.gh.github_directories[self.gh.default_head][0]["sha"] = "d" * 40
        with tempfile.TemporaryDirectory() as temp:
            stage = Path(temp)
            (stage / "package.bin").write_bytes(b"accepted bytes")
            with self.assertRaisesRegex(rc.ReleaseError, "workflows differ"):
                rc.publish(self.gh, "v1.2.3", SHA, stage, False, "")
        self.assertEqual(self.gh.writes, [])
        self.assertNotIn("v1.2.3", self.gh.refs)


class TransportTests(unittest.TestCase):
    """Reject CLI argument and path injection before a subprocess can execute."""

    def test_repository_cannot_supply_host_or_options(self):
        """An explicit github.com owner/repository identity is mandatory."""
        for repo in (
            "--hostname/evil",
            "evil.com/owner/repo",
            "owner/repo --help",
            "../repo",
        ):
            with self.subTest(repo=repo), self.assertRaises(rc.ReleaseError):
                rc.GitHub(repo)

    def test_api_routes_methods_and_traversal_fail_before_execution(self):
        """Neither flags, URLs, query fields nor encoded traversal become API routes."""
        gh = rc.GitHub(REPO)
        for path, method, body in (
            ("--hostname=evil.com", "GET", None),
            ("https://evil.com/repos/owner/repo", "GET", None),
            ("../../other/repo/releases", "GET", None),
            ("releases?hostname=evil.com", "GET", None),
            (f"compare/{SHA}...main%2F..%2F..%2Freleases", "GET", None),
            ("releases", "--hostname", None),
            ("releases", "DELETE", None),
            ("", "POST", {}),
            ("releases", "GET", {"draft": False}),
        ):
            with (
                self.subTest(path=path, method=method),
                patch.object(rc, "run_process") as command,
                self.assertRaises(rc.ReleaseError),
            ):
                gh.api(path, method, body)
            command.assert_not_called()

    def test_attempt_detail_route_rejects_invalid_ids_and_write_methods(self):
        """The added metadata route grants no write, query or traversal surface."""
        with patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}):
            gh = rc.GitHub(REPO)
        cases = [(path, "GET", None) for path in (
            "actions/runs/0/attempts/2", "actions/runs/123/attempts/0",
            "actions/runs/0123/attempts/2", "actions/runs/123/attempts/02",
            "actions/runs/123/attempts/2/", "actions/runs/123/attempts/2?x=1",
            "actions/runs/123/attempts/2/artifacts", "actions/runs/123/attempts/%32",
            "actions/runs/١٢٣/attempts/2", "actions/runs/123/attempts/٢",
            "actions/runs/1١/attempts/2", "actions/runs/123/attempts/2٢",
        )]
        cases += [("actions/runs/123/attempts/2", method, {})
                  for method in ("POST", "PATCH", "PUT", "DELETE")]
        for path, method, body in cases:
            with self.subTest(path=path, method=method), patch.object(rc, "run_process") as command:
                with self.assertRaises(rc.ReleaseError):
                    gh.request(path, method, body)
                command.assert_not_called()

    def test_default_endpoint_and_json_body_have_fixed_argument_boundaries(self):
        """A hostile body remains stdin data, never command-line options."""
        gh = rc.GitHub(REPO)
        body = {"body": "--hostname evil.com --clobber #label"}
        with patch.object(
            rc, "run_process", return_value=http_response()
        ) as command:
            gh.api("")
            gh.api("releases", "POST", body)
        root_call, create_call = command.call_args_list
        self.assertEqual(
            root_call.args[0],
            [
                "gh",
                "api",
                "--hostname",
                "github.com",
                "--include",
                "--method",
                "GET",
                "--",
                f"repos/{REPO}",
            ],
        )
        self.assertEqual(
            create_call.args[0][-4:], ["--input", "-", "--", f"repos/{REPO}/releases"]
        )
        self.assertEqual(create_call.kwargs["input"], rc.json_bytes(body))
        self.assertNotIn(body["body"], create_call.args[0])

    def test_expected_release_routes_remain_available(self):
        """Preserve every internal REST resource, including encoded default branches."""
        gh = rc.GitHub(REPO)
        paths = [
            "",
            "tags",
            "releases",
            "releases/10",
            "releases/10/assets",
            "releases/assets/21",
            f"releases/tags/{RC_TAG}",
            f"git/ref/tags/{RC_TAG}",
            "environments/release",
            "actions/runs/17",
            "actions/runs/17/artifacts",
            "actions/runs/17/attempts/2/jobs",
            "actions/artifacts/40/zip",
            f"contents/{rc.POLICY}?ref={SHA}",
            f"contents/.github?ref={SHA}",
            f"compare/{SHA}...feature%2Fbranch",
        ]
        with patch.object(
            rc, "run_process", return_value=http_response()
        ) as command:
            for path in paths:
                gh.api(path)
            gh.api("git/refs", "POST", {"ref": f"refs/tags/{RC_TAG}", "sha": SHA})
            gh.api("releases/10", "PATCH", {"draft": False})
        self.assertEqual(command.call_count, len(paths) + 2)

    def test_upload_rejects_tags_labels_symlinks_and_unstaged_paths(self):
        """Only a regular asset in the private staging directory can reach gh."""
        gh = rc.GitHub(REPO)
        with tempfile.TemporaryDirectory(prefix="release-candidate-") as temp:
            root = Path(temp)
            asset = root / "package.tar.gz"
            asset.write_bytes(b"package")
            link = root / "link.tar.gz"
            link.symlink_to(asset)
            nested = root / "nested"
            nested.mkdir()
            (nested / asset.name).write_bytes(b"package")
            invalid = [
                ("--clobber", asset),
                ("v1.2.3-rc.01", asset),
                ("v1.2.3#label", asset),
                ("v1.2.٣-rc.2", asset),
                (RC_TAG, link),
                (RC_TAG, nested / asset.name),
                (RC_TAG, Path(asset.name)),
                (RC_TAG, root),
            ]
            for name in ("package.tar.gz#label", "--clobber", "asset*.tar.gz"):
                path = root / name
                path.write_bytes(b"package")
                invalid.append((RC_TAG, path))
            for tag, path in invalid:
                with (
                    self.subTest(tag=tag, path=path),
                    patch.object(rc, "run_process") as command,
                    self.assertRaises(rc.ReleaseError),
                ):
                    gh.upload(tag, path, release_id=10)
                command.assert_not_called()

    def test_upload_never_overwrites_or_retries_an_existing_asset(self):
        """A conflicting asset cannot cause a second POST or an overwrite."""
        gh = rc.GitHub(REPO)
        with tempfile.TemporaryDirectory(prefix="release-promote-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            with (
                patch.object(
                    rc, "run_process",
                    return_value=subprocess.CompletedProcess(
                        [], 1, stderr=b"HTTP 422 already_exists"
                    ),
                ) as command,
                patch.object(gh, "pages", return_value=[]),
                self.assertRaises(rc.GitHubError),
            ):
                gh.upload("v1.2.3", asset, release_id=10)
            command.assert_called_once_with(
                [
                    "gh",
                    "api",
                    "--hostname",
                    "github.com",
                    "--include",
                    "--method",
                    "POST",
                    "-H",
                    "Content-Type: application/octet-stream",
                    "--input",
                    str(asset.resolve()),
                    "--",
                    f"https://uploads.github.com/repos/{REPO}/releases/10/assets?name=package.tar.gz",
                ],
                check=False,
                timeout=rc.ASSET_UPLOAD_TIMEOUT,
            )

    def test_upload_uses_known_id_and_encodes_asset_name_without_tag_lookup(self):
        gh = rc.GitHub(REPO)
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package+arm64.tar.gz"
            asset.write_bytes(b"package")
            response = subprocess.CompletedProcess(
                [],
                0,
                b"HTTP/2.0 201 Created\r\n\r\n"
                + json.dumps(
                    {
                        "id": 91,
                        "name": asset.name,
                        "state": "uploaded",
                        "size": 7,
                    }
                ).encode(),
                b"",
            )
            with patch.object(rc, "run_process", return_value=response) as command:
                gh.upload(RC_TAG, asset, release_id=42)
            command.assert_called_once()
            args = command.call_args.args[0]
            self.assertEqual(
                args[-1],
                f"https://uploads.github.com/repos/{REPO}/releases/42/assets?name=package%2Barm64.tar.gz",
            )
            self.assertNotIn(RC_TAG, args)
            self.assertEqual(
                Path(args[args.index("--input") + 1]).read_bytes(), b"package"
            )

    def test_upload_rejects_invalid_id_before_any_request(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            for release_id in (0, -1, True, "01", "42/assets?other=1", None):
                with (
                    self.subTest(release_id=release_id),
                    patch.object(rc, "run_process") as command,
                ):
                    with self.assertRaises(rc.ReleaseError):
                        rc.GitHub(REPO).upload(RC_TAG, asset, release_id=release_id)
                    command.assert_not_called()

    def test_upload_timeout_is_not_retried_and_does_not_expose_output(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            timeout = rc.ProcessTimeout(900, b"partial-private-body", b"")
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", side_effect=timeout) as command,
                patch.object(gh, "pages", return_value=[]) as inventory,
            ):
                with self.assertRaises(rc.GitHubError) as caught:
                    gh.upload(RC_TAG, asset, release_id=42)
                command.assert_called_once()
                inventory.assert_called_once_with("releases/42/assets")
            self.assertNotIn("private", str(caught.exception))
            self.assertNotIn(temp, str(caught.exception))
            self.assertIn("reconciliation=absent", str(caught.exception))

    def test_upload_output_limit_uses_exact_asset_readback_without_retry(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            payload = b"package"
            asset.write_bytes(payload)
            expected = {
                "id": 91, "name": asset.name, "state": "uploaded", "size": len(payload),
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
            }
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", side_effect=rc.ProcessOutputLimit("too large")) as post,
                patch.object(gh, "pages", return_value=[expected]) as inventory,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            inventory.assert_called_once_with("releases/42/assets")

    def test_upload_requires_created_matching_complete_asset(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            expected = {"id": 91, "name": asset.name, "state": "uploaded", "size": 7}
            variants = [
                (200, expected),
                (201, []),
                (201, {**expected, "id": 0}),
                (201, {**expected, "name": "wrong.tar.gz"}),
                (201, {**expected, "state": "starter"}),
                (201, {**expected, "size": 6}),
                (201, {**expected, "size": True}),
                (201, {**expected, "digest": "sha256:" + "0" * 64}),
            ]
            for status, body in variants:
                response = subprocess.CompletedProcess(
                    [],
                    0,
                    f"HTTP/2.0 {status} Response\r\n\r\n".encode()
                    + json.dumps(body).encode(),
                    b"",
                )
                gh = rc.GitHub(REPO)
                with (
                    self.subTest(status=status, body=body),
                    patch.object(
                        rc, "run_process", return_value=response
                    ) as command,
                    patch.object(gh, "pages", return_value=[]),
                ):
                    with self.assertRaises(rc.ReleaseError):
                        gh.upload(RC_TAG, asset, release_id=42)
                    command.assert_called_once()

    def test_success_status_cannot_hide_staged_file_mutation_during_post(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            created = {"id": 91, "name": asset.name, "state": "uploaded", "size": 7}
            response = subprocess.CompletedProcess(
                [], 0,
                b"HTTP/2.0 201 Created\r\n\r\n" + json.dumps(created).encode(),
                b"",
            )

            def mutate_after_post(*_args, **_kwargs):
                asset.write_bytes(b"changed")
                return response

            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", side_effect=mutate_after_post) as post,
                patch.object(gh, "pages", return_value=[]) as inventory,
                self.assertRaisesRegex(
                    rc.GitHubError,
                    "outcome=http-201-invalid-response.*reconciliation=failed",
                ),
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            inventory.assert_called_once_with("releases/42/assets")

    def test_upload_parse_failure_keeps_draft_and_reports_safe_asset_context(self):
        """A committed upload with a broken response must not be retried or published."""
        gh = FakeGitHub()
        client = rc.GitHub(REPO)
        tag = "v1.2.3-beta.7"

        def accepted_upload_then_failed_response(upload_tag, path, *, release_id):
            FakeGitHub.upload(gh, upload_tag, path, release_id=release_id)
            client.upload(upload_tag, path, release_id=release_id)

        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / rc.MANIFEST
            asset.write_bytes(b"private package bytes")
            with (
                # Deliberate test credential, never used for authentication.
                patch.dict(os.environ, {"GH_TOKEN": "private-token-fixture"}),  # nosec B105
                patch.object(
                    gh, "upload", side_effect=accepted_upload_then_failed_response
                ) as upload,
                patch.object(
                    rc, "run_process",
                    return_value=subprocess.CompletedProcess(
                        [], 1, b"private response body", b"unexpected end of JSON input"
                    ),
                ) as command,
                patch.object(client, "pages", return_value=[]),
                self.assertRaises(rc.GitHubError) as error,
            ):
                rc.publish(gh, tag, SHA, Path(temp), True, "private release body")
            release_id = next(
                key
                for key, release in gh.releases.items()
                if release["tag_name"] == tag
            )
            self.assertTrue(gh.releases[release_id]["draft"])
            self.assertEqual(len(gh.assets[release_id]), 1)
            self.assertEqual(sum(write[0] == "upload" for write in gh.writes), 1)
            self.assertFalse(any(write[1] == "PATCH" for write in gh.writes))
            upload.assert_called_once_with(tag, asset, release_id=release_id)
            command.assert_called_once()
            self.assertNotIn("--clobber", command.call_args.args[0])
            self.assertNotIn(temp, str(error.exception))
            self.assertNotIn("private", str(error.exception))
            self.assertIn("outcome=invalid-http-response", str(error.exception))
            self.assertIn("reconciliation=absent", str(error.exception))

    def test_upload_unconfirmed_http_response_is_never_retried(self):
        """A successful CLI exit alone cannot confirm the committed asset."""
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            for stdout in (None, b"private response body", b"HTTP/2.0 201 Created\r\n"):
                response = subprocess.CompletedProcess([], 0, stdout, b"")
                gh = rc.GitHub(REPO)
                with (
                    self.subTest(stdout=stdout),
                    patch.object(rc, "run_process", return_value=response) as command,
                    patch.object(gh, "pages", return_value=[]),
                ):
                    with self.assertRaises(rc.GitHubError) as error:
                        gh.upload(RC_TAG, asset, release_id=42)
                    command.assert_called_once()
                    self.assertIn(f"upload {RC_TAG} {asset.name}", str(error.exception))
                    self.assertNotIn("private response body", str(error.exception))
                    self.assertNotIn(temp, str(error.exception))

    def test_malformed_201_is_confirmed_only_by_exact_matching_inventory(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            entry = {
                "id": 91, "name": asset.name, "state": "uploaded", "size": 7,
                "digest": "sha256:" + rc.hashlib.sha256(b"package").hexdigest(),
            }
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", return_value=subprocess.CompletedProcess(
                    [], 0, b"HTTP/2.0 201 Created\r\n\r\n{malformed", b"")) as post,
                patch.object(gh, "pages", return_value=[entry]) as inventory,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            inventory.assert_called_once_with("releases/42/assets")

    def test_upload_http_500_reconciles_matching_server_digest_without_retry(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            entry = {
                "id": 91, "name": asset.name, "state": "uploaded", "size": 7,
                "digest": "sha256:" + rc.hashlib.sha256(b"package").hexdigest(),
            }
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", return_value=http_response(500)) as post,
                patch.object(gh, "pages", return_value=[entry]) as inventory,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            inventory.assert_called_once_with("releases/42/assets")

    def test_upload_http_500_absent_allows_one_retry_after_exact_inventory_read(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            created = {
                "id": 91, "name": asset.name, "state": "uploaded", "size": 7,
            }
            created_response = subprocess.CompletedProcess(
                [], 0,
                b"HTTP/2.0 201 Created\r\n\r\n" + json.dumps(created).encode(),
                b"",
            )
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", side_effect=[
                    http_response(500), created_response,
                ]) as post,
                patch.object(gh, "pages", return_value=[]) as inventory,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(post.call_args_list[0], post.call_args_list[1])
            inventory.assert_called_once_with("releases/42/assets")

    def test_delayed_first_commit_confirms_after_retry_conflict_without_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            entry = {
                "id": 91, "name": asset.name, "state": "uploaded", "size": 7,
                "digest": "sha256:" + rc.hashlib.sha256(b"package").hexdigest(),
            }
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", side_effect=[
                    http_response(500), http_response(422),
                ]) as post,
                patch.object(gh, "pages", side_effect=[[], [entry]]) as inventory,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            self.assertEqual(post.call_count, 2)
            self.assertEqual(inventory.call_count, 2)
            self.assertTrue(all(call.args == ("releases/42/assets",)
                                for call in inventory.call_args_list))

    def test_matching_asset_on_later_inventory_page_confirms_upload(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            entry = {"id": 91, "name": asset.name, "state": "uploaded", "size": 7,
                     "digest": "sha256:" + rc.hashlib.sha256(b"package").hexdigest()}
            later_page = http_response(stdout=json.dumps([entry]).encode()).stdout
            first_page = http_response(
                stdout=b"[]",
                link=b'<https://api.github.com/repos/example/project/releases/42/assets?per_page=100&page=2>; rel="next"',
            ).stdout
            pages = first_page + b"\n" + later_page
            gh = rc.GitHub(REPO)
            with patch.object(rc, "run_process", side_effect=[
                http_response(500),
                subprocess.CompletedProcess([], 0, pages, b""),
            ]) as command:
                gh.upload(RC_TAG, asset, release_id=42)
            self.assertEqual(command.call_count, 2)
            self.assertIn("--paginate", command.call_args_list[1].args[0])

    def test_later_inventory_page_failure_blocks_retry(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            first_page = http_response(
                stdout=b"[]",
                link=b'<https://api.github.com/repos/example/project/releases/42/assets?per_page=100&page=2>; rel="next"',
            ).stdout
            denied_page = http_response(403, b'{"message":"private"}').stdout
            pages = first_page + b"\n" + denied_page
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", side_effect=[
                    http_response(502),
                    subprocess.CompletedProcess([], 1, pages, b"HTTP 403 private"),
                ]) as command,
                self.assertRaisesRegex(rc.GitHubError, "reconciliation=failed"),
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            self.assertEqual(command.call_count, 2)
            self.assertEqual(command.call_args_list[0].args[0][command.call_args_list[0].args[0].index("--method") + 1], "POST")
            self.assertEqual(command.call_args_list[1].args[0][command.call_args_list[1].args[0].index("--method") + 1], "GET")

    def test_upload_publish_path_never_patches_public_after_retry_is_unconfirmed(self):
        gh = FakeGitHub()
        client = rc.GitHub(REPO)

        def reconcile_upload(tag, path, *, release_id):
            client.upload(tag, path, release_id=release_id)

        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            stage = Path(temp)
            (stage / "package.bin").write_bytes(b"candidate bytes")
            with (
                patch.object(gh, "upload", side_effect=reconcile_upload),
                patch.object(rc, "run_process", side_effect=[
                    http_response(503), http_response(500),
                ]) as posts,
                patch.object(client, "pages", side_effect=[[], []]) as inventories,
                self.assertRaisesRegex(rc.GitHubError,
                                       "attempt 2.*outcome=http-500.*reconciliation=absent"),
            ):
                rc.publish(gh, "v1.2.3-beta.9", SHA, stage, True, "")
            draft = next(item for item in gh.releases.values()
                         if item["tag_name"] == "v1.2.3-beta.9")
            self.assertTrue(draft["draft"])
            self.assertFalse(any(write[1] == "PATCH" for write in gh.writes))
            self.assertEqual(posts.call_count, 2)
            self.assertEqual(inventories.call_count, 2)

    def test_only_explicit_server_5xx_can_authorize_one_retry(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            for status, expected_posts in ((500, 2), (502, 2), (503, 2), (504, 2),
                                           (429, 1), (422, 1)):
                with self.subTest(status=status):
                    gh = rc.GitHub(REPO)
                    responses = [http_response(status)]
                    if expected_posts == 2:
                        responses.append(http_response(422))
                    with (
                        patch.object(rc, "run_process", side_effect=responses) as post,
                        patch.object(gh, "pages", return_value=[]),
                        self.assertRaises(rc.GitHubError),
                    ):
                        gh.upload(RC_TAG, asset, release_id=42)
                    self.assertEqual(post.call_count, expected_posts)

    def test_upload_timeout_or_transport_absence_never_replays_post(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            failures = (
                subprocess.TimeoutExpired(["gh", "api"], 900),
                subprocess.CompletedProcess(
                    [], 1, b"", b"gh: connection reset by peer",
                ),
            )
            for failure in failures:
                with self.subTest(failure=type(failure).__name__):
                    gh = rc.GitHub(REPO)
                    with (
                        patch.object(
                            rc, "run_process",
                            side_effect=failure if isinstance(failure, BaseException) else None,
                            return_value=None if isinstance(failure, BaseException) else failure,
                        ) as post,
                        patch.object(gh, "pages", return_value=[]) as inventory,
                        self.assertRaises(rc.GitHubError) as error,
                    ):
                        gh.upload(RC_TAG, asset, release_id=42)
                    post.assert_called_once()
                    inventory.assert_called_once_with("releases/42/assets")
                    self.assertIn("reconciliation=absent", str(error.exception))

    def test_upload_conflicting_existing_asset_stops_without_retry(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            entry = {
                "id": 91, "name": asset.name, "state": "uploaded", "size": 7,
                "digest": "sha256:" + "0" * 64,
            }
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", return_value=http_response(422)) as post,
                patch.object(gh, "pages", return_value=[entry]) as inventory,
                self.assertRaisesRegex(rc.GitHubError, "reconciliation=failed"),
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            inventory.assert_called_once_with("releases/42/assets")

    def test_upload_rejects_casefold_name_collision(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "Package.tar.gz"
            asset.write_bytes(b"package")
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", return_value=http_response(502)) as post,
                patch.object(gh, "pages", return_value=[{
                    "id": 91, "name": "package.tar.gz", "state": "uploaded", "size": 7,
                }]),
                self.assertRaises(rc.GitHubError),
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()

    def test_upload_malformed_inventory_fails_closed_without_retry(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            malformed = [
                [{"id": 91, "name": asset.name, "state": "uploaded", "size": 7,
                  "digest": "sha256:invalid"}],
                [{"id": 91, "name": asset.name, "state": "starter", "size": 7}],
                [{"id": 91, "name": asset.name, "state": "uploaded", "size": 8}],
                [{"id": 91, "name": asset.name, "state": "uploaded", "size": True}],
                [{"id": 91, "name": "other.tar.gz", "state": "uploaded", "size": True}],
                [{"id": 91, "name": "other.tar.gz", "state": "unknown", "size": 7}],
                [{"id": 91, "name": "other.tar.gz", "state": "uploaded", "size": 7,
                  "digest": "sha256:bad"}],
                [{"id": 91, "name": asset.name, "state": "uploaded", "size": 7},
                 {"id": 91, "name": "other.tar.gz", "state": "uploaded", "size": 7}],
                [{"id": 91, "name": asset.name, "state": "uploaded", "size": 7},
                 {"id": 92, "name": asset.name, "state": "uploaded", "size": 7}],
            ]
            for inventory_items in malformed:
                with self.subTest(inventory=inventory_items):
                    gh = rc.GitHub(REPO)
                    with (
                        patch.object(rc, "run_process", return_value=http_response(502)) as post,
                        patch.object(gh, "pages", return_value=inventory_items),
                        self.assertRaises(rc.GitHubError),
                    ):
                        gh.upload(RC_TAG, asset, release_id=42)
                    post.assert_called_once()

    def test_digestless_inventory_download_is_bound_to_captured_identity(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            expected = {
                "size": 7,
                "sha256": rc.hashlib.sha256(b"package").hexdigest(),
            }
            item = {"id": 91, "name": asset.name, "state": "uploaded", "size": 7}

            def mutate_during_download(_gh, _asset_id, _destination, *, expected, budget):
                self.assertEqual(expected, {
                    "size": 7,
                    "sha256": rc.hashlib.sha256(b"package").hexdigest(),
                })
                asset.write_bytes(b"changed")
                return expected

            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "download_asset", side_effect=mutate_during_download),
            ):
                with self.assertRaisesRegex(rc.ReleaseError, "changed during upload readback"):
                    rc.verify_uploaded_asset(
                        gh, item, asset, budget=rc.DownloadBudget(21), expected=expected,
                    )

    def test_reconciliation_local_io_error_is_redacted_and_never_retries(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            gh = rc.GitHub(REPO)

            def remove_during_inventory(_endpoint):
                asset.unlink()
                return []

            with (
                patch.object(rc, "run_process", return_value=http_response(503)) as post,
                patch.object(gh, "pages", side_effect=remove_during_inventory),
                self.assertRaisesRegex(
                    rc.GitHubError,
                    "outcome=http-503; reconciliation=failed; draft remains unpublished",
                ) as error,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            self.assertNotIn(temp, str(error.exception))

    def test_staged_file_change_between_attempts_blocks_second_post(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            gh = rc.GitHub(REPO)
            def absent_then_mutate(_endpoint):
                asset.write_bytes(b"changed")
                return []
            with (
                patch.object(rc, "run_process", return_value=http_response(503)) as post,
                patch.object(gh, "pages", side_effect=absent_then_mutate),
                self.assertRaisesRegex(rc.GitHubError, "reconciliation=failed"),
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()

    def test_upload_failure_reports_bounded_status_attempt_and_reconciliation(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", side_effect=[
                    http_response(502), http_response(503),
                ]) as post,
                patch.object(gh, "pages", return_value=[]),
                self.assertRaises(rc.GitHubError) as caught,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            self.assertEqual(post.call_count, 2)
            message = str(caught.exception)
            self.assertIn("attempt 2", message)
            self.assertIn("outcome=http-503", message)
            self.assertIn("reconciliation=absent", message)
            self.assertNotIn("response body", message)

    def test_upload_reconciliation_error_is_redacted_and_never_retried(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            gh = rc.GitHub(REPO)
            with (
                patch.object(rc, "run_process", return_value=http_response(502)) as post,
                patch.object(gh, "pages", side_effect=rc.GitHubError(
                    "private response and signed URL"
                )),
                self.assertRaises(rc.GitHubError) as caught,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            message = str(caught.exception)
            self.assertIn("outcome=http-502", message)
            self.assertIn("reconciliation=failed", message)
            self.assertNotIn("private", message)
            self.assertNotIn("signed URL", message)

    def test_upload_without_api_digest_verifies_asset_bytes_before_accepting(self):
        with tempfile.TemporaryDirectory(prefix="release-versioned-") as temp:
            asset = Path(temp) / "package.tar.gz"
            asset.write_bytes(b"package")
            entry = {"id": 91, "name": asset.name, "state": "uploaded", "size": 7}
            gh = rc.GitHub(REPO)
            def stream_exact(_path, output):
                output.write(b"package")
            with (
                patch.object(rc, "run_process", return_value=http_response(503)) as post,
                patch.object(gh, "pages", return_value=[entry]),
                patch.object(gh, "download_asset", side_effect=stream_exact) as download,
            ):
                gh.upload(RC_TAG, asset, release_id=42)
            post.assert_called_once()
            download.assert_called_once_with("releases/assets/91", unittest.mock.ANY)

    def test_canonical_numeric_fields_remain_ascii(self):
        """Reject Unicode digits after adopting concise regexes with ASCII flags."""
        for value in ("1.٢.3", "01.2.3", "1.2.3\n"):
            with self.subTest(version=value), self.assertRaises(rc.ReleaseError):
                rc.version(value)
        for value in ("1١", "１２", "01", True):
            with self.subTest(identifier=value), self.assertRaises(rc.ReleaseError):
                rc.positive(value, "run ID")
        for tag in (
            "v0.2.3",
            RC_TAG,
            "v1.2.3-beta.4",
            "v1.2.3-nightly.20260912000000.17.1",
        ):
            self.assertIsNotNone(rc.TAG_RE.fullmatch(tag))
        with self.assertRaises(rc.ReleaseError):
            rc.parse_json(b"\xff", "invalid UTF-8")


class OfflineValidationTests(unittest.TestCase):
    """Check parsing, staging and REST contracts without network access."""

    def test_restricted_suffixes_reject_every_asset_before_staging_copies(self):
        """Do not partially stage allowed files before finding a retired package."""
        restrictions = (
            {
                "suffixes": [".apk", ".aab"],
                "reason": "Android APK/AAB publication moved to another repository",
            },
        )
        for suffix in (".apk", ".APK", ".aab", ".AaB"):
            with (
                self.subTest(suffix=suffix),
                tempfile.TemporaryDirectory() as temp,
                patch.object(rc, "ASSET_RESTRICTIONS", restrictions),
            ):
                source, output = Path(temp) / "source", Path(temp) / "output"
                source.mkdir()
                output.mkdir()
                (source / "first.zip").write_bytes(b"allowed package")
                (source / ("retired" + suffix)).write_bytes(b"retired package")
                with self.assertRaisesRegex(
                    rc.ReleaseError, "APK/AAB publication moved"
                ):
                    rc.stage_assets(source, output)
                self.assertEqual(list(output.iterdir()), [])

    def test_retired_rc_payload_is_rejected_before_any_publication_client_call(self):
        """The current guard applies when promoting bytes from an older RC."""
        restrictions = (
            {"suffixes": [".apk", ".aab"], "reason": "APK/AAB publication moved"},
        )
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(rc, "ASSET_RESTRICTIONS", restrictions),
        ):
            stage = Path(temp)
            (stage / "retired.APK").write_bytes(b"previously verified RC payload")
            gh = Mock(spec_set=[])
            with self.assertRaisesRegex(rc.ReleaseError, "APK/AAB publication moved"):
                rc.publish(gh, "v1.2.3", SHA, stage, False, "")
            self.assertEqual(gh.mock_calls, [])

    def test_restrictions_leave_unrelated_suffixes_and_payload_bytes_unchanged(self):
        """Match literal filename endings rather than substrings or package content."""
        payloads = {
            "app.ipa": b"ios",
            "app.dmg": b"mac",
            "app.exe": b"windows",
            "app.tar.gz": b"web",
            "app.apk.zip": b"archive",
        }
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(
                rc,
                "ASSET_RESTRICTIONS",
                ({"suffixes": [".apk", ".aab"], "reason": "Retired Android package"},),
            ),
        ):
            source, output = Path(temp) / "source", Path(temp) / "output"
            source.mkdir()
            output.mkdir()
            for name, data in payloads.items():
                (source / name).write_bytes(data)
            assets = rc.stage_assets(source, output)
            self.assertEqual({item["name"] for item in assets}, set(payloads))
            for name, data in payloads.items():
                self.assertEqual((output / name).read_bytes(), data)

    def test_unconfigured_repository_can_still_stage_android_packages(self):
        """A retirement rule applies only to the repository declaring it."""
        with (
            tempfile.TemporaryDirectory() as temp,
            patch.object(rc, "ASSET_RESTRICTIONS", ()),
        ):
            source, output = Path(temp) / "source", Path(temp) / "output"
            source.mkdir()
            output.mkdir()
            (source / "app.apk").write_bytes(b"native Android app")
            self.assertEqual(rc.stage_assets(source, output)[0]["name"], "app.apk")

    def test_actions_zip_uses_default_accept_and_release_assets_use_binary_accept(self):
        """Actions zip uses default accept and release assets use binary accept."""
        gh = rc.GitHub(REPO)

        def service(command, **_kwargs):
            if command[-1].endswith("actions/artifacts/40/zip"):
                if "-H" in command:
                    return subprocess.CompletedProcess(
                        command, 1, stderr=b"HTTP 415 Unsupported Accept header"
                    )
                data = b"zip archive"
            else:
                self.assertIn("Accept: application/octet-stream", command)
                data = b"release asset"
            return http_response(stdout=data)

        with patch.object(rc, "run_process", side_effect=service):
            self.assertEqual(gh.binary("actions/artifacts/40/zip"), b"zip archive")
            self.assertEqual(gh.binary("releases/assets/21"), b"release asset")
            with self.assertRaises(rc.ReleaseError):
                gh.binary("unexpected/endpoint")

    def test_source_policy_blob_hash_and_encoding_are_checked(self):
        """Source policy blob hash and encoding are checked."""
        gh = rc.GitHub(REPO)
        valid = policy_response(policy())
        for changes in (
            {"type": "symlink"},
            {"encoding": "none"},
            {"content": "!invalid!"},
            {"sha": "b" * 40},
            {"size": 0},
        ):
            with (
                self.subTest(changes=changes),
                patch.object(gh, "api", return_value={**valid, **changes}),
                self.assertRaises(rc.ReleaseError),
            ):
                rc.source_policy_snapshot(gh, SHA)

    def test_version_rejects_injection_and_noncanonical_values(self):
        """Version rejects injection and noncanonical values."""
        for value in (
            "01.2.3",
            "1.02.3",
            "1.2.03",
            "v1.2.3",
            "1.2",
            "1.2.3-rc.1",
            "1.2.3\n",
            "1.2.3; touch /tmp/owned",
            "$(id)",
        ):
            with self.subTest(value=value), self.assertRaises(rc.ReleaseError):
                rc.version(value)
        self.assertEqual(rc.version("0.0.0"), "0.0.0")

    def test_manifest_rejects_beta_nightly_bad_versions_and_unsafe_names(self):
        """Manifest rejects beta nightly bad versions and unsafe names."""
        for change in (
            {"channel": "beta"},
            {"channel": "nightly"},
            {"version": "01.2.3"},
            {"repository": "other/repo"},
            {"source_sha": "short"},
            {"workflow_path": "other.yml"},
            {"run_id": 0},
            {"run_attempt": True},
            {"tag": "v1.2.3-rc.01"},
            {"assets": []},
        ):
            raw = rc.json_bytes({**manifest(), **change})
            with self.subTest(change=change), self.assertRaises(rc.ReleaseError):
                rc.validate_manifest(raw, REPO, RC_TAG)
        for name in (
            "../escape",
            "/absolute",
            "nested/file",
            "release-manifest.json",
            "asset\n.txt",
        ):
            invalid = manifest()
            invalid["assets"][0]["name"] = name
            raw = rc.json_bytes(invalid)
            with self.subTest(name=name), self.assertRaises(rc.ReleaseError):
                rc.validate_manifest(raw, REPO, RC_TAG)

    def test_duplicate_manifest_fields_and_assets_rejected(self):
        """Duplicate manifest fields and assets rejected."""
        with self.assertRaises(rc.ReleaseError):
            rc.validate_manifest(b'{"schema":1,"schema":2}', REPO, RC_TAG)
        invalid = manifest()
        invalid["assets"].append({**invalid["assets"][0], "name": "PACKAGE.TAR.GZ"})
        raw = rc.json_bytes(invalid)
        with self.assertRaises(rc.ReleaseError):
            rc.validate_manifest(raw, REPO, RC_TAG)

    def test_empty_nested_symlink_reserved_and_colliding_assets_rejected(self):
        """Empty nested symlink reserved and colliding assets rejected."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, output = root / "source", root / "output"
            source.mkdir()
            output.mkdir()
            with self.assertRaises(rc.ReleaseError):
                rc.stage_assets(source, output)
            (source / "directory").mkdir()
            with self.assertRaises(rc.ReleaseError):
                rc.stage_assets(source, output)
            (source / "directory").rmdir()
            (source / "link").symlink_to(root / "missing")
            with self.assertRaises(rc.ReleaseError):
                rc.stage_assets(source, output)
            (source / "link").unlink()
            (source / rc.MANIFEST).write_bytes(b"reserved")
            with self.assertRaises(rc.ReleaseError):
                rc.stage_assets(source, output)

    def test_nightly_identity_and_monotonic_rc_sequences(self):
        """Nightly identity and monotonic rc sequences."""
        gh = FakeGitHub()
        now = datetime(2026, 9, 12, 1, 2, 3, tzinfo=timezone.utc)
        self.assertEqual(
            rc.candidate_tag(gh, "nightly", "1.2.3", 17, 2, None, now),
            "v1.2.3-nightly.20260912010203.17.2",
        )
        self.assertEqual(rc.next_sequence(gh, "1.2.3", "rc"), 3)
        self.assertEqual(rc.next_sequence(gh, "1.2.3", "beta"), 1)
        with self.assertRaises(rc.ReleaseError):
            rc.candidate_tag(gh, "nightly", "1.2.3", 17, 2, 1, now)

    def test_pagination_keeps_gate_on_later_pages(self):
        """Pagination keeps gate on later pages."""
        gh = rc.GitHub(REPO)
        with patch.object(
            rc, "run_process",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                http_response(
                    stdout=b'{"jobs":[{"name":"build"}]}',
                    link=b'<https://api.github.com/repos/example/project/actions/runs/17/attempts/1/jobs?per_page=20&page=2>; rel=next',
                ).stdout
                + b"\n"
                + http_response(stdout=b'{"jobs":[{"name":"Release gate"}]}').stdout,
            ),
        ) as command:
            jobs = gh.pages("actions/runs/17/attempts/1/jobs", "jobs")
        self.assertEqual([job["name"] for job in jobs], ["build", "Release gate"])
        self.assertIn("--paginate", command.call_args.args[0])
        self.assertNotIn("--slurp", command.call_args.args[0])
        self.assertIn("--include", command.call_args.args[0])

    def test_permission_errors_do_not_masquerade_as_absent_tags(self):
        """Permission errors do not masquerade as absent tags."""
        gh = rc.GitHub(REPO)
        with (
            patch.object(gh, "api", side_effect=rc.GitHubError("HTTP 403 forbidden")),
            self.assertRaises(rc.GitHubError),
        ):
            gh.optional("git/ref/tags/v1.2.3")


class ExecutingRunStatusTests(unittest.TestCase):
    """Exercise delayed aggregate status with real provenance/execution guards."""

    def setUp(self):
        # pylint: disable-next=consider-using-with
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.event = directory / "event.json"
        self.event.write_text(json.dumps({"inputs": {"channel": "stable"}}))
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "GITHUB_ACTIONS": "true",
                    "GITHUB_REPOSITORY": REPO,
                    "GITHUB_RUN_ID": "99",
                    "GITHUB_RUN_ATTEMPT": "1",
                    "GITHUB_REF": "refs/heads/main",
                    "GITHUB_EVENT_NAME": "workflow_dispatch",
                    "GITHUB_WORKFLOW_REF": f"{REPO}/{rc.WORKFLOW}@refs/heads/main",
                    "GITHUB_EVENT_PATH": str(self.event),
                },
            )
        )
        self.checkout = self.enterContext(
            patch.object(rc, "checked_out_sha", return_value=SHA)
        )
        self.gh = FakeGitHub()
        self.now = 0
        self.enterContext(
            patch.object(rc.time, "monotonic", side_effect=lambda: self.now)
        )
        self.sleep = self.enterContext(
            patch.object(rc.time, "sleep", side_effect=self.advance)
        )

    def advance(self, seconds):
        """Move a fake monotonic clock without real sleeps."""
        self.now += seconds

    def wait(self, gate=False):
        """Use the exact current run binding with an independently tested gate."""
        return rc.wait_for_executing_run(
            self.gh, 99, "stable", self.gh.info, SHA, 1, gate=gate
        )

    def test_gate_metadata_must_catch_up_after_the_run_becomes_active(self):
        """A running publisher can precede its prerequisite's jobs API update."""
        ready = self.gh.jobs[0]
        snapshots = (
            [[]]
            + [
                [{**ready, "status": status, "conclusion": None}]
                for status in (
                    "queued",
                    "requested",
                    "pending",
                    "waiting",
                    "in_progress",
                )
            ]
            + [[ready]]
        )
        with (
            patch.object(self.gh, "api", return_value=run(99, False)) as api,
            patch.object(self.gh, "pages", side_effect=snapshots) as pages,
        ):
            result = self.wait(gate=True)
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(api.call_count, len(snapshots))
        self.assertEqual(self.checkout.call_count, len(snapshots))
        self.assertEqual(self.now, 2 * (len(snapshots) - 1))
        for call in pages.call_args_list:
            self.assertEqual(call.args, ("actions/runs/99/attempts/1/jobs", "jobs"))
        self.assertEqual(self.gh.writes, [])


    def test_run_and_gate_propagation_share_one_deadline(self):
        """Becoming active must not restart the five-minute publication wait."""

        def delayed_run(_path):
            return {
                **run(99, False),
                "status": "queued" if self.now < 80 else "in_progress",
            }

        with (
            patch.object(self.gh, "api", side_effect=delayed_run),
            patch.object(self.gh, "pages", return_value=[]),
            patch.object(rc.sys, "stderr", new_callable=io.StringIO),
            self.assertRaisesRegex(rc.ReleaseError, "Release gate.*within 300 seconds"),
        ):
            self.wait(gate=True)
        self.assertEqual(self.now, 300)
        self.assertEqual(self.sleep.call_count, 150)
        self.assertEqual(self.gh.writes, [])

    def test_slow_successful_read_cannot_exceed_the_shared_deadline(self):
        """A final API call completing late must not silently extend the budget."""
        for delayed_read in ("run", "gate"):
            self.now = 0

            def read_run(_path):
                if delayed_read == "run":
                    self.now = rc.EXECUTING_RUN_WAIT_SECONDS + 1
                return run(99, False)

            def read_gate(_path, _field):
                if delayed_read == "gate":
                    self.now = rc.EXECUTING_RUN_WAIT_SECONDS + 1
                return self.gh.jobs

            with (
                self.subTest(delayed_read=delayed_read),
                patch.object(self.gh, "api", side_effect=read_run),
                patch.object(self.gh, "pages", side_effect=read_gate),
                self.assertRaisesRegex(rc.ReleaseError, "within 300 seconds"),
            ):
                self.wait(gate=True)
        self.sleep.assert_not_called()
        self.assertEqual(self.gh.writes, [])

    def test_invalid_or_unsuccessful_gate_is_never_polled_into_success(self):
        """Only absent or consistent nonterminal metadata may be retried."""
        ready = self.gh.jobs[0]
        snapshots = [
            [{**ready, "conclusion": conclusion}]
            for conclusion in (
                "failure",
                "skipped",
                "cancelled",
                "timed_out",
                "action_required",
                "neutral",
                None,
            )
        ] + [
            [ready, ready],
            [{**ready, "head_sha": "b" * 40}],
            [{**ready, "status": "queued", "head_sha": "b" * 40, "conclusion": None}],
            [{**ready, "status": "unknown", "conclusion": None}],
            [{**ready, "status": "queued", "conclusion": "failure"}],
            [{**ready, "status": None, "conclusion": None}],
        ]
        for snapshot in snapshots:
            with (
                self.subTest(snapshot=snapshot),
                patch.object(self.gh, "api", return_value=run(99, False)),
                patch.object(self.gh, "pages", return_value=snapshot) as pages,
                self.assertRaises(rc.ReleaseError),
            ):
                self.wait(gate=True)
            pages.assert_called_once()
        self.sleep.assert_not_called()
        self.assertEqual(self.gh.writes, [])

    def test_gate_wait_rechecks_execution_identity_before_each_snapshot(self):
        """An old pending gate cannot authorize a rerun or a different source."""
        for change in ({"run_attempt": 2}, {"head_sha": "b" * 40}, {"id": 100}):
            with (
                self.subTest(change=change),
                patch.object(
                    self.gh,
                    "api",
                    side_effect=[run(99, False), {**run(99, False), **change}],
                ) as api,
                patch.object(self.gh, "pages", return_value=[]) as pages,
                self.assertRaises(rc.ReleaseError),
            ):
                self.wait(gate=True)
            self.assertEqual(api.call_count, 2)
            pages.assert_called_once()
        self.assertEqual(self.sleep.call_count, 3)
        self.assertEqual(self.gh.writes, [])

    def test_completed_run_never_waits_for_missing_or_pending_gate(self):
        """The propagation exception belongs only to the executing publisher."""
        for snapshot in (
            [],
            [{**self.gh.jobs[0], "status": "queued", "conclusion": None}],
        ):
            with (
                self.subTest(snapshot=snapshot),
                patch.object(self.gh, "pages", return_value=snapshot),
            ):
                with self.assertRaises(rc.ReleaseError):
                    rc.validate_run(self.gh, run(), self.gh.info, SHA, 1, completed=True)
        self.sleep.assert_not_called()

    def test_duplicate_gate_on_later_page_is_not_a_propagation_delay(self):
        """Every page participates in the uniqueness check."""
        client = rc.GitHub(REPO)
        with (
            patch.object(
                client,
                "request",
                return_value=json.dumps(
                    [{"jobs": self.gh.jobs}, {"jobs": self.gh.jobs}]
                ).encode(),
            ),
            self.assertRaisesRegex(rc.ReleaseError, "Exactly one"),
        ):
            rc.release_gate_ready(client, 99, SHA, 1, allow_pending=True)
        self.sleep.assert_not_called()

    def test_transitional_states_require_a_fresh_in_progress_response(self):
        """Neither queued nor waiting itself grants permission to publish."""
        responses = [
            {**run(99, False), "status": status}
            for status in ("queued", "requested", "pending", "waiting", "in_progress")
        ]
        responses[-1]["updated_at"] = "fresh-response"
        with patch.object(self.gh, "api", side_effect=responses) as api:
            result = self.wait()
        self.assertEqual(result, responses[-1])
        self.assertEqual(api.call_count, 5)
        self.assertEqual(self.checkout.call_count, 5)
        self.assertEqual(self.sleep.call_count, 4)
        self.assertEqual(self.gh.writes, [])

    def test_running_publication_survives_aggregate_delay_beyond_one_minute(self):
        """An executing job can see stale aggregate status beyond the old limit."""
        responses = [{**run(99, False), "status": "queued"} for _ in range(40)] + [
            run(99, False)
        ]
        with (
            patch.object(self.gh, "api", side_effect=responses) as api,
            patch.object(rc.sys, "stderr", new_callable=io.StringIO) as diagnostics,
        ):
            result = self.wait()
        self.assertEqual(result["status"], "in_progress")
        self.assertEqual(self.now, 80)
        self.assertEqual(api.call_count, 41)
        self.assertEqual(self.checkout.call_count, 41)
        self.assertEqual(diagnostics.getvalue().count("Waiting for Actions run"), 1)
        self.assertIn("attempt 1", diagnostics.getvalue())
        self.assertEqual(self.gh.writes, [])

    def test_extended_wait_still_rejects_changed_attempt(self):
        """The extra wait never authorizes a response from a later rerun."""
        responses = [{**run(99, False), "status": "queued"} for _ in range(40)] + [
            {**run(99, False), "run_attempt": 2}
        ]
        with (
            patch.object(self.gh, "api", side_effect=responses),
            self.assertRaisesRegex(rc.ReleaseError, "attempt mismatch"),
        ):
            self.wait()
        self.assertEqual(self.now, 80)
        self.assertEqual(self.gh.writes, [])

    def test_every_refreshed_identity_field_is_rechecked(self):
        """A newly active response cannot swap a run, source, workflow or attempt."""
        changes = [
            {"id": 100},
            {"run_attempt": 2},
            {"head_sha": "b" * 40},
            {"path": ".github/workflows/other.yml"},
            {"head_branch": "feature"},
            {"event": "push"},
            {"repository": {"full_name": "fork/project"}},
            {"head_repository": {"full_name": "fork/project"}},
        ]
        for change in changes:
            with (
                self.subTest(change=change),
                patch.object(
                    self.gh,
                    "api",
                    side_effect=[
                        {**run(99, False), "status": "waiting"},
                        {**run(99, False), **change},
                    ],
                ) as api,
            ):
                previous = self.sleep.call_count
                with self.assertRaises(rc.ReleaseError):
                    self.wait()
                self.assertEqual(api.call_count, 2)
                self.assertEqual(self.sleep.call_count, previous + 1)
                self.assertEqual(self.gh.writes, [])

    def test_execution_checkout_is_rechecked_after_wait(self):
        """The checked-out source remains bound after an aggregate-state delay."""
        with (
            patch.object(
                self.gh,
                "api",
                side_effect=[{**run(99, False), "status": "queued"}, run(99, False)],
            ),
            patch.object(rc, "checked_out_sha", side_effect=[SHA, "b" * 40]),
            self.assertRaisesRegex(rc.ReleaseError, "Checkout"),
        ):
            self.wait()
        self.assertEqual(self.sleep.call_count, 1)

    def test_terminal_or_unknown_state_fails_without_sleep(self):
        """Cancelled/completed/unknown executions are never polled into permission."""
        states = [
            ("completed", "success"),
            ("completed", "failure"),
            ("completed", "cancelled"),
            ("cancelled", None),
            ("unknown", None),
            ("waiting", "failure"),
            ("in_progress", "failure"),
        ]
        for status, conclusion in states:
            with (
                self.subTest(status=status, conclusion=conclusion),
                patch.object(
                    self.gh,
                    "api",
                    return_value={
                        **run(99, False),
                        "status": status,
                        "conclusion": conclusion,
                    },
                ),
                self.assertRaises(rc.ReleaseError),
            ):
                self.wait()
        self.sleep.assert_not_called()
        self.assertEqual(self.gh.writes, [])

    def test_timeout_is_bounded_and_does_not_publish(self):
        """A perpetually stale API fails closed after exactly the bounded wait."""
        with (
            patch.object(
                self.gh, "api", return_value={**run(99, False), "status": "waiting"}
            ) as api,
            self.assertRaisesRegex(
                rc.ReleaseError, "within 300 seconds.*waiting.*99.*1"
            ),
        ):
            self.wait()
        self.assertEqual(self.now, 300)
        self.assertEqual(api.call_count, 151)
        self.assertEqual(self.sleep.call_count, 150)
        self.assertEqual(self.gh.writes, [])

    def test_completed_candidate_validation_does_not_poll(self):
        """Accepted RCs still require completed success on the exact attempt."""
        with patch.object(self.gh, "api") as api:
            rc.validate_run(self.gh, run(), self.gh.info, SHA, 1, completed=True)
            for change in (
                {"status": "queued"},
                {"status": "waiting"},
                {"conclusion": "failure"},
                {"run_attempt": 2},
            ):
                candidate = {**run(), **change}
                with self.subTest(change=change), self.assertRaises(rc.ReleaseError):
                    rc.validate_run(
                        self.gh,
                        candidate,
                        self.gh.info,
                        SHA,
                        1,
                        completed=True,
                    )
            api.assert_not_called()
        self.sleep.assert_not_called()


class SupersededCandidateTests(unittest.TestCase):
    """Use real API response fields to prove replacement without claiming release."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.source = {**run(99, False), "event": "push"}
        self.head = "b" * 40
        self.successor = {
            **run(100, False),
            "event": "push",
            "head_sha": self.head,
        }
        self.gh.default_head = self.head
        self.gh.successors = [self.successor]

    def check(self, channel="beta"):
        return rc.superseded_candidate(self.gh, self.gh.info, self.source, channel)

    def test_current_head_always_uses_normal_publication(self):
        self.gh.default_head = SHA
        self.gh.successors = []
        self.assertIsNone(self.check())
        self.assertEqual(self.gh.writes, [])

    def test_push_and_schedule_only_record_proven_supersession(self):
        for event, channel in (("push", "beta"), ("schedule", "nightly")):
            self.source["event"] = event
            result = self.check(channel)
            self.assertEqual(result["status"], "superseded")
            self.assertEqual(result["source_sha"], SHA)
            self.assertEqual(result["superseded_by"], self.head)
            self.assertEqual(result["successor_run_id"], "100")
            self.assertNotIn("manifest_path", result)
            self.assertEqual(self.gh.writes, [])

    def test_manual_dispatch_is_unchanged_even_at_older_source(self):
        self.source["event"] = "workflow_dispatch"
        with patch.object(self.gh, "api") as api:
            for channel in ("beta", "nightly", "rc", "stable"):
                self.assertIsNone(self.check(channel))
            api.assert_not_called()

    def test_successor_completion_is_not_a_publication_claim(self):
        for status, conclusion in (("queued", None), ("completed", "failure")):
            self.successor.update(status=status, conclusion=conclusion)
            self.assertEqual(self.check()["status"], "superseded")

    def test_missing_or_ambiguous_replacement_fails_closed(self):
        for successors in ([], [self.successor, self.successor]):
            self.gh.successors = successors
            with self.assertRaisesRegex(rc.ReleaseError, "one proven replacement"):
                self.check()
        self.assertEqual(self.gh.writes, [])

    def test_forged_old_or_manual_replacement_fails_closed(self):
        for change in (
            {"repository": {"full_name": "other/repo"}},
            {"head_repository": {"full_name": "other/repo"}},
            {"head_branch": "other"},
            {"head_sha": SHA},
            {"path": ".github/workflows/other.yml"},
            {"event": "workflow_dispatch"},
            {"id": 99},
            {"run_number": 99},
            {"run_attempt": 0},
            {"status": "unknown"},
        ):
            with self.subTest(change=change):
                self.gh.successors = [{**self.successor, **change}]
                with self.assertRaises(rc.ReleaseError):
                    self.check()
        self.assertEqual(self.gh.writes, [])

    def test_malformed_or_changed_ref_and_divergence_fail_closed(self):
        original = self.gh.api
        for bad_ref in (
            {},
            {"ref": "refs/heads/other"},
            {"ref": "refs/heads/main", "object": {"type": "tag", "sha": self.head}},
            {"ref": "refs/heads/main", "object": {"type": "commit", "sha": "short"}},
        ):

            def malformed(path, method="GET", body=None):
                return (
                    bad_ref
                    if path == "git/ref/heads/main"
                    else original(path, method, body)
                )

            with patch.object(self.gh, "api", side_effect=malformed):
                with self.assertRaisesRegex(
                    rc.ReleaseError, "verify current default branch"
                ):
                    self.check()
        for status in ("diverged", "behind", "identical"):

            def divergent(path, method="GET", body=None):
                if path.startswith("compare/"):
                    return {"status": status, "merge_base_commit": {"sha": SHA}}
                return original(path, method, body)

            with patch.object(self.gh, "api", side_effect=divergent):
                with self.assertRaises(rc.ReleaseError):
                    self.check()
        reads = 0

        def changing(path, method="GET", body=None):
            nonlocal reads
            result = original(path, method, body)
            if path == "git/ref/heads/main":
                reads += 1
                if reads > 1:
                    result["object"]["sha"] = "c" * 40
            return result

        with patch.object(self.gh, "api", side_effect=changing):
            with self.assertRaisesRegex(rc.ReleaseError, "changed while verifying"):
                self.check()

    def test_default_branch_rename_does_not_skip_against_an_old_branch(self):
        original = self.gh.api

        def renamed(path, method="GET", body=None):
            value = original(path, method, body)
            if path == "":
                value["default_branch"] = "renamed"
            return value

        with patch.object(self.gh, "api", side_effect=renamed):
            with self.assertRaisesRegex(rc.ReleaseError, "Default branch changed"):
                self.check()

    def test_unknown_api_response_and_errors_do_not_become_success(self):
        original = self.gh.api
        for response in (
            {},
            {"total_count": True, "workflow_runs": [self.successor]},
            {"total_count": 101, "workflow_runs": [self.successor]},
        ):

            def invalid(path, method="GET", body=None):
                if path.startswith("actions/workflows/"):
                    return response
                return original(path, method, body)

            with patch.object(self.gh, "api", side_effect=invalid):
                with self.assertRaises(rc.ReleaseError):
                    self.check()
        with patch.object(self.gh, "api", side_effect=rc.GitHubError("HTTP 403")):
            with self.assertRaises(rc.GitHubError):
                self.check()

    def test_narrow_transport_accepts_only_the_fixed_successor_query(self):
        client = rc.GitHub(REPO)
        path = (
            "actions/workflows/release-pipeline.yml/runs"
            f"?branch=main&event=push&head_sha={self.head}&per_page=100"
        )
        result = http_response(stdout=b'{"total_count":0,"workflow_runs":[]}')
        with patch.object(rc, "run_process", return_value=result) as command:
            client.api(path)
            self.assertEqual(command.call_args.args[0][-1], f"repos/{REPO}/{path}")
        for invalid in (
            path.replace("event=push", "event=pull_request"),
            path.replace("release-pipeline.yml", "other.yml"),
        ):
            with self.assertRaises(rc.ReleaseError):
                client.api(invalid)


class AtomicOutputTests(unittest.TestCase):
    """Output replacement must preserve aliases and never publish partial bytes."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = self.root / "evidence.json"
        self.before = b"existing output\n"
        self.after = b'{"verified":"exact bytes"}\n'

    def test_create_and_overwrite_preserve_exact_bytes_and_existing_mode(self):
        rc.atomic_write_bytes(self.output, self.before)
        if os.name == "posix":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        self.output.chmod(0o640)
        rc.atomic_write_bytes(self.output, self.after)
        self.assertEqual(self.output.read_bytes(), self.after)
        self.assertEqual(rc.digest(self.output.read_bytes()), rc.digest(self.after))
        if os.name == "posix":
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_hardlink_replacement_preserves_other_name_and_bytes(self):
        original = self.root / "outside.json"
        original.write_bytes(self.before)
        os.link(original, self.output)
        rc.atomic_write_bytes(self.output, self.after)
        self.assertEqual(original.read_bytes(), self.before)
        self.assertEqual(self.output.read_bytes(), self.after)
        self.assertNotEqual(original.stat().st_ino, self.output.stat().st_ino)
        self.assertEqual(original.stat().st_nlink, 1)

    def test_symlink_dangling_symlink_directory_and_fifo_fail_closed(self):
        original = self.root / "outside.json"
        original.write_bytes(self.before)
        kinds = ["symlink", "dangling", "directory"]
        if hasattr(os, "mkfifo"):
            kinds.append("fifo")
        for kind in kinds:
            with self.subTest(kind=kind):
                output = self.root / kind
                if kind == "directory":
                    output.mkdir()
                elif kind == "fifo":
                    os.mkfifo(output)
                else:
                    output.symlink_to(
                        original if kind == "symlink" else self.root / "missing"
                    )
                with self.assertRaisesRegex(ValueError, "plain file"):
                    rc.atomic_write_bytes(output, self.after)
                self.assertEqual(original.read_bytes(), self.before)
                self.assertFalse((self.root / "missing").exists())
                self.assertEqual(list(self.root.glob(".release-output-*")), [])

    def test_failed_write_flush_or_replace_preserves_old_output_and_cleans_staging(
        self,
    ):
        original_fdopen = os.fdopen

        @contextmanager
        def partial_write(fd, mode):
            with original_fdopen(fd, mode) as handle:

                def fail(data):
                    handle.write(data[:5])
                    raise OSError("injected partial write")

                writer = Mock(wraps=handle)
                writer.write.side_effect = fail
                yield writer

        for operation, replacement in (
            ("fdopen", partial_write),
            ("fsync", OSError("injected flush failure")),
            ("replace", OSError("injected replacement failure")),
        ):
            with self.subTest(operation=operation):
                self.output.write_bytes(self.before)
                with (
                    patch.object(rc.os, operation, side_effect=replacement),
                    self.assertRaises(OSError),
                ):
                    rc.atomic_write_bytes(self.output, self.after)
                self.assertEqual(self.output.read_bytes(), self.before)
                self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_symlink_inserted_during_staging_is_rejected_without_target_write(self):
        original = self.root / "outside.json"
        original.write_bytes(self.before)
        self.output.write_bytes(b"old output")
        fsync = os.fsync

        def swap(fd):
            fsync(fd)
            self.output.unlink()
            self.output.symlink_to(original)

        with (
            patch.object(rc.os, "fsync", side_effect=swap),
            self.assertRaisesRegex(ValueError, "plain file"),
        ):
            rc.atomic_write_bytes(self.output, self.after)
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(original.read_bytes(), self.before)
        self.assertEqual(list(self.root.glob(".release-output-*")), [])


class CurrentAttemptFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.detail = {'id': 123, 'run_attempt': 2, 'head_sha': SHA, 'path': rc.WORKFLOW,
                       'run_started_at': '2026-11-03T02:00:00Z',
                       'created_at': '2026-11-03T01:00:00Z'}
        self.job = {'name': 'checks / CI gate', 'status': 'completed', 'conclusion': 'success',
                    'head_sha': SHA, 'run_attempt': 2, 'started_at': '2026-11-03T02:01:00Z',
                    'completed_at': '2026-11-03T02:05:00Z',
                    'steps': [{'name': 'Check', 'status': 'completed', 'conclusion': 'success',
                               'started_at': '2026-11-03T02:02:00Z', 'completed_at': '2026-11-03T02:04:00Z'}]}
        self.gh = Mock()
        self.gh.api.return_value = self.detail

    def check(self, jobs=None):
        rc.require_current_attempt_jobs(self.gh, 123, SHA, 2, [self.job] if jobs is None else jobs)

    def test_fresh_execution_and_old_run_creation_are_accepted(self):
        self.check()
        self.gh.api.assert_called_once_with('actions/runs/123/attempts/2')

    def test_fresh_attempt_uses_the_real_allowlisted_transport(self):
        """Exercise API shape validation as well as the execution-time guard."""
        resolved_gh = "/reviewed/bin/gh"
        with (
            patch.dict(os.environ, {"RELEASE_REQUIRE_WORKFLOW_SCOPE": ""}),
            patch.object(rc, "resolve_executable", return_value=resolved_gh),
            patch.object(rc, "run_process", return_value=http_response(
                0, rc.json_bytes(self.detail)
            )) as command,
        ):
            gh = rc.GitHub(REPO)
            rc.require_current_attempt_jobs(gh, 123, SHA, 2, [self.job])
        command.assert_called_once()
        self.assertEqual(command.call_args.args[0], [
            resolved_gh, "api", "--hostname", "github.com", "--include", "--method", "GET",
            "--", f"repos/{REPO}/actions/runs/123/attempts/2",
        ])
        self.assertEqual(command.call_args.kwargs["timeout"], 30)

    def test_timezone_offsets_compare_as_instants(self):
        self.detail['run_started_at'] = '2026-11-02T18:00:00-08:00'
        self.check()

    def test_first_attempt_preserves_historical_missing_clock_contract(self):
        rc.require_current_attempt_jobs(self.gh, 123, SHA, 1, [{'conclusion': 'success'}])
        self.gh.api.assert_not_called()

    def test_cloned_success_rejected_even_with_new_attempt_number(self):
        self.job.update(started_at='2026-11-03T01:01:00Z', completed_at='2026-11-03T01:05:00Z')
        with self.assertRaisesRegex(rc.ReleaseError, 'predates'):
            self.check()

    def test_old_steps_cannot_hide_under_new_job_timestamps(self):
        self.job['steps'][0].update(started_at='2026-11-03T01:01:00Z', completed_at='2026-11-03T01:05:00Z')
        with self.assertRaisesRegex(rc.ReleaseError, 'outside'):
            self.check()

    def setup_boundary_fixture(self):
        """Reproduce the runner setup timestamps observed in a completed job."""
        self.detail['run_started_at'] = '2026-11-08T17:30:00Z'
        self.job.update(started_at='2026-11-08T18:46:21Z', completed_at='2026-11-08T18:47:27Z')
        self.job['steps'] = [{'name': 'Set up job', 'number': 1, 'status': 'completed',
                             'conclusion': 'success', 'started_at': '2026-11-08T18:46:20Z',
                             'completed_at': '2026-11-08T18:46:21Z'}]

    def test_first_setup_step_may_cross_exact_one_second_job_boundary(self):
        self.setup_boundary_fixture()
        self.check()

    def test_setup_boundary_exception_cannot_admit_other_steps_or_intervals(self):
        for change in ({'started_at': '2026-11-08T18:46:19Z'},
                       {'completed_at': '2026-11-08T18:46:20.500Z'},
                       {'name': 'Run checks'},
                       {'number': 2}, {'number': True}, {'number': '1'},
                       {'completed_at': '2026-11-08T18:46:20Z'},
                       {'completed_at': '2026-11-08T18:47:28Z'}, {'status': 'in_progress'}):
            with self.subTest(change=change):
                self.setup_boundary_fixture()
                self.job['steps'][0].update(change)
                with self.assertRaises(rc.ReleaseError):
                    self.check()
        self.setup_boundary_fixture()
        self.job['steps'].insert(0, {'conclusion': 'skipped'})
        with self.assertRaisesRegex(rc.ReleaseError, 'outside'):
            self.check()

    def test_setup_boundary_never_crosses_the_attempt_start(self):
        self.setup_boundary_fixture()
        self.detail['run_started_at'] = '2026-11-08T18:46:21Z'
        with self.assertRaisesRegex(rc.ReleaseError, 'outside'):
            self.check()

    def test_regular_step_still_requires_exact_job_interval(self):
        self.setup_boundary_fixture()
        self.job['steps'].append({**self.job['steps'][0], 'name': 'Check', 'number': 2})
        with self.assertRaisesRegex(rc.ReleaseError, 'outside'):
            self.check()

    def test_intentional_skips_and_unfinished_publisher_do_not_need_clocks(self):
        self.job['steps'].append({'conclusion': 'skipped'})
        self.job['steps'].append({'conclusion': 'failure', 'status': 'completed'})
        self.check([self.job, {'status': 'completed', 'conclusion': 'skipped'},
                    {'status': 'in_progress', 'conclusion': None}])

    def test_wrong_attempt_metadata_is_rejected(self):
        for key, value in [('id', 124), ('head_sha', 'b' * 40), ('run_attempt', 1), ('path', 'wrong.yml')]:
            with self.subTest(key=key), self.assertRaisesRegex(rc.ReleaseError, 'identity'):
                self.gh.api.return_value = self.detail | {key: value}
                self.check()

    def test_malformed_attempt_metadata_is_actionable(self):
        for bad in [None, [], "invalid", 2, True]:
            with self.subTest(value=bad), self.assertRaisesRegex(rc.ReleaseError, "Malformed release attempt metadata"):
                self.gh.api.return_value = bad
                self.check()

    def test_successful_job_needs_executed_success_step(self):
        self.job['steps'] = [{'status': 'completed', 'conclusion': 'skipped'}]
        with self.assertRaisesRegex(rc.ReleaseError, "lacks successful execution steps"):
            self.check()

    def test_attempt_jobs_endpoint_allows_omitted_optional_run_attempt(self):
        self.job.pop("run_attempt")
        self.check([self.job])

    def test_wrong_successful_job_identity_is_rejected(self):
        for key, value in [('head_sha', 'b' * 40), ('run_attempt', 1), ('head_sha', None)]:
            with self.subTest(key=key), self.assertRaisesRegex(rc.ReleaseError, 'differs'):
                self.check([self.job | {key: value}])

    def test_missing_naive_invalid_and_non_string_timestamps_are_rejected(self):
        for bad in [None, '', 0, '2026-11-03T02:00:00', '2026-99-03T02:00:00Z', '2026-11-03T02:00:00+00:60', '2026-11-03T02:00:00+24:00', 'now']:
            for target, key in [('detail', 'run_started_at'), ('job', 'started_at'), ('job', 'completed_at'),
                                ('step', 'started_at'), ('step', 'completed_at')]:
                with self.subTest(target=target, key=key, bad=bad), self.assertRaises(rc.ReleaseError):
                    detail, job = deepcopy(self.detail), deepcopy(self.job)
                    (detail if target == 'detail' else job if target == 'job' else job['steps'][0])[key] = bad
                    self.gh.api.return_value = detail
                    self.check([job])
        self.gh.api.return_value = self.detail

    def test_timestamp_shape_rejects_non_ascii_digits(self):
        for value in ["٢٠٢٦-11-03T02:00:00Z", "2026-11-03T٠٢:00:00Z",
                      "2026-11-03T02:00:00.٥Z", "2026-11-03T02:00:00+٠٠:00"]:
            with self.subTest(value=value), self.assertRaises(rc.ReleaseError):
                rc._execution_timestamp(value, "test timestamp")

    def test_reversed_intervals_and_missing_steps_are_rejected(self):
        for changes in [{'completed_at': '2026-11-03T02:00:00Z'}, {'steps': []}, {'steps': None},
                        {'steps': [{'conclusion': 'success', 'status': 'queued'}]}, {'steps': [None]}]:
            with self.subTest(changes=changes), self.assertRaises(rc.ReleaseError):
                self.check([self.job | changes])




class CheckRunExecutionTests(unittest.TestCase):
    """Official check-run identity can resolve stale aggregate status offline."""

    setUp = ExecutingRunStatusTests.setUp
    advance = ExecutingRunStatusTests.advance

    def execution(self):
        """Return synthetic same-attempt observations with deliberately distinct IDs."""
        current = {**run(99, False), "status": "queued",
                   "run_started_at": "2026-01-01T00:00:00Z"}
        job = {"id": 700, "run_id": 99, "run_attempt": 1, "head_sha": SHA,
               "check_run_url": f"https://api.github.com/repos/{REPO}/check-runs/800",
               "runner_id": 900, "runner_name": "runner-example", "name": "Display (matrix)",
               "status": "in_progress", "conclusion": None, "completed_at": None,
               "started_at": "2026-01-01T00:00:01Z", "steps": [
                   {"number": 1, "status": "in_progress", "conclusion": None,
                    "started_at": "2026-01-01T00:00:02Z", "completed_at": None}]}
        self.enterContext(patch.dict(os.environ, {
            "RELEASE_EXECUTION_CHECK_RUN_ID": "800", "RUNNER_NAME": "runner-example",
            "GITHUB_JOB": "logical-job-id",}))
        return current, job

    def wait(self, proof=None, gate=False):
        """Use the real execution guards and a synthetic API only."""
        return rc.wait_for_executing_run(
            self.gh, 99, "stable", self.gh.info, SHA, 1, gate=gate, execution_proof=proof)

    def test_stale_run_returns_raw_status_and_separate_proof(self):
        current, job = self.execution()
        proof = []
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", return_value=[job]):
            observed = self.wait(proof)
        self.assertEqual(observed, current)
        self.assertEqual(observed["status"], "queued")
        self.assertEqual(proof, [rc.ExecutionJobProof(700, 800, 900, "runner-example")])
        self.assertNotIn("_execution_job_id", observed)
        self.assertEqual(self.gh.writes, [])

    def test_exact_attempt_jobs_endpoint_allows_optional_attempt_field_omission(self):
        current, job = self.execution()
        job.pop("run_attempt")
        with patch.object(self.gh, "pages", return_value=[job]):
            proof = rc.current_execution_job(self.gh, current, 800)
        self.assertEqual(proof, rc.ExecutionJobProof(700, 800, 900, "runner-example"))

    def test_successful_gate_rejects_publisher_proof_if_job_is_now_terminal(self):
        current, publisher = self.execution()
        publisher.update(status="completed", conclusion="success",
                         completed_at="2026-01-01T00:01:00Z")
        gate = {"id": 701, "run_id": 99, "head_sha": SHA,
                "name": "Release gate", "status": "completed", "conclusion": "success"}
        proof = rc.ExecutionJobProof(700, 800, 900, "runner-example")
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", return_value=[gate, publisher]), \
                self.assertRaisesRegex(rc.ReleaseError, "not executing"):
            rc.release_gate_ready(
                self.gh, 99, SHA, 1,
                publisher_proof=proof,
                publisher_run=current,
            )

    def test_successful_gate_accepts_current_publisher_proof_and_omitted_attempt(self):
        current, publisher = self.execution()
        publisher.pop("run_attempt")
        gate = {"id": 701, "run_id": 99, "head_sha": SHA,
                "name": "Release gate", "status": "completed", "conclusion": "success"}
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", return_value=[gate, publisher]):
            self.assertTrue(rc.release_gate_ready(
                self.gh, 99, SHA, 1,
                publisher_proof=rc.ExecutionJobProof(700, 800, 900, "runner-example"),
                publisher_run=current,
            ))

    def test_live_publisher_rejects_terminal_exact_attempt_despite_stale_active_snapshot(self):
        """Fresh attempt metadata wins over a previously active publisher snapshot."""
        current, publisher = self.execution()
        current["run_attempt"] = 2
        current["status"] = "in_progress"
        current["run_started_at"] = "2026-01-01T00:00:00Z"
        current["event"] = "workflow_dispatch"
        publisher.update(run_attempt=2)
        gate = {"id": 701, "run_id": 99, "run_attempt": 2, "head_sha": SHA,
                "name": "Release gate", "status": "completed", "conclusion": "success"}
        terminal = {
            "id": 99, "run_attempt": 2, "head_sha": SHA,
            "path": rc.WORKFLOW, "event": "workflow_dispatch",
            "status": "completed", "conclusion": "cancelled",
            "run_started_at": "2026-01-01T00:00:00Z",
        }
        with patch.object(self.gh, "api", return_value=terminal), \
                patch.object(self.gh, "pages", return_value=[gate, publisher]), \
                self.assertRaisesRegex(rc.ReleaseError, "not active"):
            rc.release_gate_ready(
                self.gh, 99, SHA, 2,
                publisher_proof=rc.ExecutionJobProof(700, 800, 900, "runner-example"),
                publisher_run=current,
            )

    def test_successful_gate_waits_for_pending_publisher_steps_when_allowed(self):
        current, publisher = self.pending_step_job()
        gate = {"id": 701, "run_id": 99, "head_sha": SHA,
                "name": "Release gate", "status": "completed", "conclusion": "success"}
        proof = rc.ExecutionJobProof(700, 800, 900, "runner-example")
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", return_value=[gate, publisher]):
            self.assertFalse(rc.release_gate_ready(
                self.gh, 99, SHA, 1, allow_pending=True,
                publisher_proof=proof, publisher_run=current,
            ))

        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", return_value=[gate, publisher]), \
                self.assertRaisesRegex(rc.ReleaseError, "Exactly one executing step"):
            rc.release_gate_ready(
                self.gh, 99, SHA, 1,
                publisher_proof=proof, publisher_run=current,
            )

    def test_attempt_in_progress_is_used_without_rewriting_aggregate(self):
        current, _ = self.execution()
        active = {**current, "status": "in_progress"}
        with patch.object(self.gh, "api", side_effect=[current, active]), \
                patch.object(self.gh, "pages") as jobs:
            observed = self.wait()
        self.assertIs(observed, active)
        self.assertEqual(current["status"], "queued")
        jobs.assert_not_called()

    def test_normal_active_admission_captures_proof_for_later_readback(self):
        current, job = self.execution()
        current["status"] = "in_progress"
        proof = []
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", side_effect=[[], [job]]):
            self.wait(proof)
        self.assertEqual(proof[0].job_id, 700)
        self.assertEqual(self.now, 2)

    def test_missing_and_consistent_pending_job_wait_without_authorizing(self):
        current, job = self.execution()
        pending = {**job, "status": "queued", "runner_id": 0, "runner_name": ""}
        proof = []
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", side_effect=[[], [pending], [job]]):
            self.wait(proof)
        self.assertEqual(self.now, 4)
        self.assertEqual(len(proof), 1)

    def test_current_job_does_not_replace_the_required_gate(self):
        current, job = self.execution()
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", return_value=[job]), \
                patch.object(rc, "release_gate_ready", return_value=False), \
                self.assertRaisesRegex(rc.ReleaseError, "within 300 seconds"):
            self.wait([], gate=True)
        self.assertEqual(self.now, 300)

    def test_attempt_terminal_or_identity_conflict_stops_before_jobs(self):
        current, _ = self.execution()
        for change in ({"status": "completed"}, {"conclusion": "cancelled"},
                       {"run_attempt": 2}, {"head_sha": "b" * 40},
                       {"repository": {"full_name": "other/repository"}}):
            with self.subTest(change=change), \
                    patch.object(self.gh, "api", side_effect=[current, {**current, **change}]), \
                    patch.object(self.gh, "pages") as jobs, \
                    self.assertRaises(rc.ReleaseError):
                self.wait([])
            jobs.assert_not_called()

    def test_late_attempt_or_jobs_cannot_extend_the_same_deadline(self):
        current, job = self.execution()
        for late in ("attempt", "jobs"):
            self.now = 0
            def read(path):
                if late == "attempt" and "/attempts/" in path:
                    self.now = 301
                return current
            def pages(*_args):
                self.now = 301
                return [job]
            with self.subTest(late=late), patch.object(self.gh, "api", side_effect=read), \
                    patch.object(self.gh, "pages", side_effect=pages), \
                    self.assertRaisesRegex(rc.ReleaseError, "within 300 seconds"):
                self.wait([])

    def test_hostile_job_identity_status_runner_and_timing_fail_closed(self):
        current, job = self.execution()
        changes = [{"id": True}, {"run_id": 100}, {"run_attempt": True},
                   {"run_attempt": None},
                   {"head_sha": "b" * 40}, {"runner_id": 0}, {"runner_name": "other"},
                   {"check_run_url": "https://foreign.test/check-runs/800"},
                   {"status": "completed"}, {"conclusion": "failure"},
                   {"started_at": "2025-12-31T23:59:59Z"}, {"steps": []}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(rc.ReleaseError):
                rc.require_executing_job({**job, **change}, current, REPO, 800)

    def test_duplicate_id_or_check_identity_is_not_a_pending_job(self):
        current, job = self.execution()
        for jobs in ([job, job], [job, {**job, "id": 701}]):
            with self.subTest(jobs=jobs), patch.object(self.gh, "pages", return_value=jobs), \
                    self.assertRaises(rc.ReleaseError):
                rc.current_execution_job(self.gh, current, 800)

    def test_empty_context_keeps_strict_status_and_invalid_id_fails(self):
        for context in ("", "0", "01", "-1", "1.0", "true"):
            with self.subTest(context=context), patch.dict(os.environ, {
                "RELEASE_EXECUTION_CHECK_RUN_ID": context,
            }):
                if context:
                    with self.assertRaises(rc.ReleaseError):
                        rc.execution_check_run_id()
                else:
                    self.assertIsNone(rc.execution_check_run_id())

    def test_repository_case_is_compatible_but_other_url_components_are_strict(self):
        current, job = self.execution()
        self.assertEqual(rc.require_executing_job(job, current, REPO.upper(), 800).job_id, 700)
        for url in (f"https://api.github.com/repos/{REPO}/check-runs/800?extra=1",
                    f"https://api.github.com/repos/{REPO}/check-runs/800#fragment",
                    f"https://api.github.com/repos/{REPO}/check-runs/800?",
                    f"https://api.github.com/repos/{REPO}/check-runs/800#",
                    f" https://api.github.com/repos/{REPO}/check-runs/800",
                    f"https://api.github.com/repos/{REPO}/check-runs/800\n",
                    f"https:\n//api.github.com/repos/{REPO}/check-runs/800",
                    "https://api.github.com/repos/foreign/repo/check-runs/800",
                    f"https://user@api.github.com/repos/{REPO}/check-runs/800"):
            with self.subTest(url=url), self.assertRaises(rc.ReleaseError):
                rc.require_executing_job({**job, "check_run_url": url}, current, REPO, 800)

    def test_insecure_scheme_is_rejected_with_other_execution_identity_unchanged(self):
        current, job = self.execution()
        self.assertEqual(rc.require_executing_job(job, current, REPO, 800).job_id, 700)
        accepted = urlsplit(job["check_run_url"])
        # Mutate only the scheme of an otherwise accepted route, without sending it.
        rejected = accepted._replace(scheme="http")
        self.assertEqual(rejected.scheme, "http")
        self.assertEqual(rejected._replace(scheme=accepted.scheme), accepted)
        rejected_job = {**job, "check_run_url": rejected.geturl()}
        with self.assertRaisesRegex(rc.ReleaseError, "Invalid execution check-run URL"):
            rc.require_executing_job(rejected_job, current, REPO, 800)

    def pending_step_job(self):
        """Model step propagation while the exact current job already runs."""
        current, job = self.execution()
        job["steps"] = [
            {"number": 1, "status": "completed", "conclusion": "success",
             "started_at": "2026-01-01T00:00:01Z", "completed_at": "2026-01-01T00:00:02Z"},
            {"number": 2, "status": "queued", "conclusion": None,
             "started_at": None, "completed_at": None},
        ]
        return current, job

    def test_valid_queued_steps_are_unresolved_and_not_an_execution_proof(self):
        current, job = self.pending_step_job()
        with patch.object(self.gh, "pages", return_value=[job]):
            self.assertIsNone(rc.current_execution_job(self.gh, current, 800))

    def test_first_setup_step_one_second_before_job_start_is_allowed_for_pending_job(self):
        current, job = self.pending_step_job()
        current["run_started_at"] = "2026-01-01T00:00:00Z"
        job["started_at"] = "2026-01-01T00:00:01Z"
        job["steps"][0].update(
            name="Set up job", number=1,
            started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z",
        )
        with patch.object(self.gh, "pages", return_value=[job]):
            self.assertIsNone(rc.current_execution_job(self.gh, current, 800))

    def test_setup_step_offset_still_cannot_cross_attempt_or_exceed_one_second(self):
        current, job = self.pending_step_job()
        job["started_at"] = "2026-01-01T00:00:02Z"
        job["steps"][0].update(
            name="Set up job", number=1,
            started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z",
        )
        with patch.object(self.gh, "pages", return_value=[job]), \
                self.assertRaisesRegex(rc.ReleaseError, "outside"):
            rc.current_execution_job(self.gh, current, 800)
        current["run_started_at"] = "2026-01-01T00:00:01Z"
        job["started_at"] = "2026-01-01T00:00:01Z"
        with patch.object(self.gh, "pages", return_value=[job]), \
                self.assertRaisesRegex(rc.ReleaseError, "outside"):
            rc.current_execution_job(self.gh, current, 800)

    def test_step_becoming_active_uses_the_original_wait_budget(self):
        current, pending = self.pending_step_job()
        active = deepcopy(pending)
        active["steps"][1].update(status="in_progress", started_at="2026-01-01T00:00:03Z")
        proof = []
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", side_effect=[[pending], [active]]):
            self.wait(proof)
        self.assertEqual(self.now, 2)
        self.assertEqual(proof, [rc.ExecutionJobProof(700, 800, 900, "runner-example")])

    def test_never_visible_active_step_expires_without_a_proof(self):
        current, job = self.pending_step_job()
        proof = []
        with patch.object(self.gh, "api", return_value=current), \
                patch.object(self.gh, "pages", return_value=[job]), \
                self.assertRaisesRegex(rc.ReleaseError, "within 300 seconds"):
            self.wait(proof)
        self.assertEqual(self.now, 300)
        self.assertEqual(proof, [])

    def test_direct_execution_validator_keeps_pending_steps_strict(self):
        current, job = self.pending_step_job()
        with self.assertRaisesRegex(rc.ReleaseError, "Exactly one executing step"):
            rc.require_executing_job(job, current, REPO, 800)

    def test_invalid_zero_active_steps_are_fatal_not_delayed(self):
        current, job = self.pending_step_job()
        completed, pending = job["steps"]
        invalid = [[], [completed], [pending, pending],
                   [{**pending, "status": "unknown"}],
                   [{**pending, "started_at": "2026-01-01T00:00:03Z"}],
                   [{**pending, "conclusion": "failure"}],
                   [{**completed, "started_at": "2025-12-31T23:59:59Z"}, pending],
                   [{**completed, "completed_at": "2999-01-01T00:00:00Z"}, pending],
                   [{**completed, "conclusion": "failure"}, pending]]
        for steps in invalid:
            with self.subTest(steps=steps), patch.object(self.gh, "pages", return_value=[{**job, "steps": steps}]), \
                    self.assertRaises(rc.ReleaseError):
                rc.current_execution_job(self.gh, current, 800)

    def test_waiting_steps_cannot_mask_wrong_job_source_or_runner(self):
        current, job = self.pending_step_job()
        for change in ({"head_sha": "b" * 40}, {"run_attempt": 2}, {"runner_name": "other"},
                       {"runner_id": 0}, {"status": "completed"}):
            with self.subTest(change=change), \
                    patch.object(self.gh, "pages", return_value=[{**job, **change}]), \
                    self.assertRaises(rc.ReleaseError):
                rc.current_execution_job(self.gh, current, 800)


class PublishBoundaryOrderingTests(unittest.TestCase):
    def test_final_exact_publisher_read_after_tag_blocks_patch_if_terminal(self):
        """A publisher ending during the final tag GET blocks the public PATCH."""
        events = []
        deadlines = []

        class Client:
            repo = REPO
            tag_sha = SHA
            tag_reads = 0
            publisher_status = "in_progress"

            def api(self, path, method="GET", body=None):
                events.append((path, method))
                if path == "git/refs" and method == "POST":
                    return {"ref": "refs/tags/v1.2.3"}
                if path == "releases" and method == "POST":
                    return {"id": 20, "tag_name": "v1.2.3", "draft": True}
                if path == "git/ref/tags/v1.2.3":
                    self.tag_reads += 1
                    if self.tag_reads == 1:
                        self.publisher_status = "completed"
                    deadlines.append(rc._API_READ_DEADLINE.get())
                    return {"ref": "refs/tags/v1.2.3",
                            "object": {"type": "commit", "sha": self.tag_sha}}
                if path == "actions/runs/99/attempts/1":
                    deadlines.append(rc._API_READ_DEADLINE.get())
                    conclusion = None if self.publisher_status == "in_progress" else "cancelled"
                    return {"id": 99, "run_attempt": 1, "head_sha": SHA,
                            "path": rc.WORKFLOW, "event": "workflow_dispatch",
                            "status": self.publisher_status, "conclusion": conclusion}
                if path == "releases/20" and method == "PATCH":
                    return {"id": 20, "tag_name": "v1.2.3", "draft": False,
                            "prerelease": True,
                            "html_url": "https://example.invalid/release"}
                raise AssertionError(f"Unexpected API call {method} {path}")

            def upload(self, *_args, **_kwargs):
                events.append(("upload", "POST"))

            def pages(self, path, *_args, **_kwargs):
                events.append((path, "GET"))
                return [{"name": "package.bin"}]

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "package.bin").write_bytes(b"package")

            def confirm_publish():
                events.append(("full-publisher-validation", "GET"))
                client.pages("actions/runs/99/attempts/1/jobs")

            def confirm_final_publish():
                events.append(("confirm-publisher-attempt", "GET"))
                current = client.api("actions/runs/99/attempts/1")
                rc.require_active_attempt(current, current)

            client = Client()
            with (
                patch.object(rc, "verify_required_signatures"),
                patch.object(rc, "reject_restricted_assets"),
                patch.object(rc, "ensure_absent"),
                patch.object(rc, "check_workflow_publication"),
                patch.object(rc, "verify_uploaded_asset"),
                self.assertRaisesRegex(rc.ReleaseError, "Execution attempt is not active"),
            ):
                rc._publish_prepared(
                    client, "v1.2.3", SHA, directory, True, "notes",
                    confirm_publish=confirm_publish,
                    confirm_final_publish=confirm_final_publish,
                )

        self.assertEqual(events[-5:], [
            ("full-publisher-validation", "GET"),
            ("actions/runs/99/attempts/1/jobs", "GET"),
            ("git/ref/tags/v1.2.3", "GET"),
            ("confirm-publisher-attempt", "GET"),
            ("actions/runs/99/attempts/1", "GET"),
        ])
        self.assertEqual(len(deadlines), 2)
        self.assertIsInstance(deadlines[0], float)
        self.assertEqual(deadlines[0], deadlines[1])
        self.assertFalse(any(event == ("releases/20", "PATCH") for event in events))

    def test_full_publish_confirmation_precedes_final_tag_and_move_blocks_patch(self):
        events = []

        class Client:
            repo = REPO
            tag_sha = SHA

            def api(self, path, method="GET", body=None):
                events.append((path, method))
                if path == "git/refs" and method == "POST":
                    return {"ref": "refs/tags/v1.2.3"}
                if path == "releases" and method == "POST":
                    return {"id": 20, "tag_name": "v1.2.3", "draft": True}
                if path == "git/ref/tags/v1.2.3":
                    return {"ref": "refs/tags/v1.2.3", "object": {"type": "commit", "sha": self.tag_sha}}
                if path == "releases/20" and method == "PATCH":
                    return {"id": 20, "html_url": "https://example.invalid/release"}
                raise AssertionError(f"Unexpected API call {method} {path}")

            def upload(self, *_args, **_kwargs):
                events.append(("upload", "POST"))

            def pages(self, *_args, **_kwargs):
                return [{"name": "package.bin"}]

        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "package.bin").write_bytes(b"package")

            def confirm_publish():
                events.append(("full-confirm", "READ"))
                self.assertNotIn(("releases/20", "PATCH"), events)
                # The tag can move while source/publisher checks are running;
                # the tag read after this full callback must catch it.
                client.tag_sha = "b" * 40

            def confirm_final_publish():
                events.append(("exact-attempt-confirm", "READ"))

            client = Client()
            with (
                patch.object(rc, "verify_required_signatures"),
                patch.object(rc, "reject_restricted_assets"),
                patch.object(rc, "ensure_absent"),
                patch.object(rc, "check_workflow_publication"),
                patch.object(rc, "verify_uploaded_asset"),
                self.assertRaisesRegex(rc.ReleaseError, "Tag moved before publication"),
            ):
                rc._publish_prepared(
                    client, "v1.2.3", SHA, directory, True, "notes",
                    confirm_publish=confirm_publish,
                    confirm_final_publish=confirm_final_publish,
                )
        self.assertEqual(events[-2:], [
            ("full-confirm", "READ"),
            ("git/ref/tags/v1.2.3", "GET"),
        ])
        self.assertNotIn(("exact-attempt-confirm", "READ"), events)
        self.assertNotIn(("releases/20", "PATCH"), events)


if __name__ == "__main__":
    unittest.main()
