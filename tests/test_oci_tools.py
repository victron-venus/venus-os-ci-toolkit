"""Local adversarial tests; no network, Go compilation, root or daemons."""

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_oci_tools as build  # noqa: E402
import qualify_oci_tools as runtime  # noqa: E402
import oci_vulnerability as vulnerability  # noqa: E402
import oci_header_disposition as headers  # noqa: E402


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target = self.root / "artifact"
        self.payload = b"exact reviewed bytes"
        self.sha = hashlib.sha256(self.payload).hexdigest()
        self.url = "https://example.invalid/locked-package.deb"

    def http_error(self, code):
        return urllib.error.HTTPError(self.url, code, "fixture", {}, io.BytesIO(b"error"))

    def test_transient_http_recovers_without_partial_target(self):
        errors = [self.http_error(502), self.http_error(503)]
        def response(*args, **kwargs):
            self.assertFalse(self.target.exists())
            if errors:
                raise errors.pop(0)
            return io.BytesIO(self.payload)
        with patch.object(build.urllib.request, "urlopen", side_effect=response) as fetch, \
                patch.object(build.time, "sleep") as sleep:
            build.download(self.url, self.target, self.sha, len(self.payload))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertEqual(list(self.root.iterdir()), [self.target])

    def test_transient_http_exhausts_exactly_five_attempts(self):
        with patch.object(build.urllib.request, "urlopen", \
                          side_effect=[self.http_error(503) for _ in range(5)]) as fetch, \
                patch.object(build.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                build.download(self.url, self.target, self.sha, len(self.payload))
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8, 16])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_permanent_http_and_transport_errors_are_not_retried(self):
        for error in [self.http_error(code) for code in (400, 401, 403, 404, 429)] + [
                urllib.error.URLError("certificate verify failed")]:
            with self.subTest(error=error), \
                    patch.object(build.urllib.request, "urlopen", side_effect=error) as fetch, \
                    patch.object(build.time, "sleep") as sleep:
                with self.assertRaises(urllib.error.URLError):
                    build.download(self.url, self.target, self.sha, len(self.payload))
                fetch.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_untrusted_bytes_never_publish_or_retry(self):
        cases = [(b"wrong", len(self.payload)), (self.payload + b"x", len(self.payload)),
                 (self.payload[:-1] + b"!", len(self.payload)),
                 (self.payload, len(self.payload) + 1)]
        for payload, size in cases:
            with self.subTest(payload=payload, size=size), \
                    patch.object(build.urllib.request, "urlopen", return_value=io.BytesIO(payload)) as fetch, \
                    patch.object(build.time, "sleep") as sleep:
                with self.assertRaisesRegex(ValueError, "Download (identity mismatch|exceeds bound)"):
                    build.download(self.url, self.target, self.sha, size)
                fetch.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(list(self.root.iterdir()), [])

    def test_existing_destination_is_never_overwritten(self):
        self.target.write_bytes(b"preserved")
        with patch.object(build.urllib.request, "urlopen", return_value=io.BytesIO(self.payload)), \
                patch.object(build.time, "sleep") as sleep:
            with self.assertRaises(FileExistsError):
                build.download(self.url, self.target, self.sha, len(self.payload))
        self.assertEqual(self.target.read_bytes(), b"preserved")
        self.assertEqual(list(self.root.iterdir()), [self.target])
        sleep.assert_not_called()

    def test_partial_stream_error_removes_temporary_bytes(self):
        class Interrupted(io.BytesIO):
            def read(inner, size=-1):
                if inner.tell():
                    raise urllib.error.URLError("connection reset")
                return super().read(3)
        with patch.object(build.urllib.request, "urlopen", return_value=Interrupted(self.payload)), \
                patch.object(build.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.URLError):
                build.download(self.url, self.target, self.sha, len(self.payload))
        self.assertEqual(list(self.root.iterdir()), [])
        sleep.assert_not_called()

    def test_non_https_refused_before_network(self):
        with patch.object(build.urllib.request, "urlopen") as fetch:
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                build.download("http://example.invalid/package", self.target, self.sha)
        fetch.assert_not_called()


class NativePackageDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def inputs(self, arch="amd64"):
        spec = json.loads((build.INPUTS / f"native-{arch}.json").read_text())
        item = spec["packages"][0]
        return spec, item, self.root / Path(item["filename"]).name

    def failure(self, code):
        return urllib.error.HTTPError("https://snapshot.ubuntu.com/", code,
                                      "fixture", {}, io.BytesIO(b"error"))

    def test_each_architecture_falls_back_with_exact_locked_identity(self):
        for arch, archive in (("amd64", "https://archive.ubuntu.com/ubuntu/"),
                              ("arm64", "https://ports.ubuntu.com/ubuntu-ports/")):
            for status in (500, 502, 503, 504):
                with self.subTest(arch=arch, status=status):
                    spec, item, target = self.inputs(arch)
                    with patch.object(build, "download", side_effect=[self.failure(status), None]) as fetch:
                        self.assertTrue(build.download_native_package(spec, item, target))
                    self.assertTrue(callable(fetch.call_args_list[0].kwargs["opener"]))
                    self.assertEqual(fetch.call_args_list[0].kwargs, fetch.call_args_list[1].kwargs)
                    self.assertEqual([call.args for call in fetch.call_args_list], [
                        (spec["base_url"] + item["filename"], target, item["sha256"], item["size"]),
                        (archive + item["filename"], target, item["sha256"], item["size"]),
                    ])

    def test_available_snapshot_needs_no_archive(self):
        spec, item, target = self.inputs()
        with patch.object(build, "download") as fetch:
            self.assertFalse(build.download_native_package(spec, item, target))
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args, (spec["base_url"] + item["filename"], target,
                                               item["sha256"], item["size"]))
        self.assertTrue(callable(fetch.call_args.kwargs["opener"]))

    def test_security_identity_and_permanent_errors_never_switch_origins(self):
        errors = [self.failure(code) for code in (401, 403, 404, 429)] + [
            urllib.error.URLError("certificate verify failed"),
            ValueError("Download identity mismatch"), ValueError("Download exceeds bound"),
            OSError("cannot write"),
        ]
        spec, item, target = self.inputs()
        for error in errors:
            with self.subTest(error=error), patch.object(build, "download", side_effect=error) as fetch:
                with self.assertRaises(type(error)):
                    build.download_native_package(spec, item, target)
                fetch.assert_called_once()

    def test_invalid_origins_and_metadata_are_rejected_before_network(self):
        spec, item, target = self.inputs()
        cases = [dict(spec, base_url="https://example.invalid/ubuntu/"),
                 dict(spec, base_url=spec["base_url"].replace("://", "://user:password@")),
                 dict(spec, base_url=spec["base_url"].replace(".com/", ".com:443/")),
                 dict(spec, snapshot="different-snapshot"), dict(spec, architecture="s390x"),
                 dict(spec, distribution="ubuntu:25.10")]
        for value in cases:
            with self.subTest(spec=value), patch.object(build, "download") as fetch:
                with self.assertRaisesRegex(ValueError, "origin"):
                    build.download_native_package(value, item, target)
                fetch.assert_not_called()

    def test_invalid_paths_are_rejected_before_network(self):
        spec, item, target = self.inputs()
        for path in ("https://other.invalid/pkg.deb", "pool/../pkg.deb", "pool//pkg.deb",
                     "pool/pkg.deb?query", "pool/pkg.deb#fragment", "pool/%2e%2e/pkg.deb"):
            with self.subTest(path=path), patch.object(build, "download") as fetch:
                with self.assertRaisesRegex(ValueError, "path"):
                    build.download_native_package(spec, dict(item, filename=path), target)
                fetch.assert_not_called()

    def test_archive_failure_is_final(self):
        spec, item, target = self.inputs()
        for error in (self.failure(503), ValueError("Download identity mismatch")):
            with self.subTest(error=error), \
                    patch.object(build, "download", side_effect=[self.failure(502), error]) as fetch:
                with self.assertRaises(type(error)):
                    build.download_native_package(spec, item, target)
                self.assertEqual(fetch.call_count, 2)

    def test_diagnostics_include_basename_without_urls(self):
        spec, item, target = self.inputs()
        with patch.object(build, "download", side_effect=[self.failure(503), None]), \
                patch("builtins.print") as output:
            build.download_native_package(spec, item, target)
        messages = [call.args[0] for call in output.call_args_list]
        self.assertTrue(all(target.name in message for message in messages))
        self.assertTrue(all("https://" not in message and str(self.root) not in message
                            for message in messages))

    def test_native_download_uses_strict_redirect_handler(self):
        spec, item, target = self.inputs()
        with patch.object(build.urllib.request, "build_opener") as factory, \
                patch.object(build, "download") as fetch:
            build.download_native_package(spec, item, target)
        factory.assert_called_once()
        self.assertIsInstance(factory.call_args.args[0], build.NativeArchiveRedirect)
        self.assertEqual(fetch.call_args.kwargs["opener"], factory.return_value.open)

    def test_native_redirect_allows_same_origin_https(self):
        url = "https://archive.ubuntu.com/ubuntu/pool/old.deb"
        target = "https://archive.ubuntu.com/ubuntu/pool/new.deb"
        request = build.NativeArchiveRedirect().redirect_request(
            build.urllib.request.Request(url), None, 302, "Found", {}, target)
        self.assertEqual(request.full_url, target)

    def test_native_redirect_rejects_downgrade_and_origin_changes(self):
        request = build.urllib.request.Request("https://archive.ubuntu.com/ubuntu/pool/package.deb")
        for target in ("http://archive.ubuntu.com/ubuntu/pool/package.deb",
                       "https://other.invalid/package.deb",
                       "https://archive.ubuntu.com:8443/package.deb",
                       "https://user:password@archive.ubuntu.com/package.deb"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "HTTPS origin"):
                build.NativeArchiveRedirect().redirect_request(request, None, 302, "Found", {}, target)

    def test_verified_fallback_is_reused_for_remaining_packages(self):
        spec, _, _ = self.inputs()
        selected = False
        with patch.object(build, "download", side_effect=[self.failure(503), None, None, None]) as fetch:
            for item in spec["packages"][:3]:
                target = self.root / Path(item["filename"]).name
                selected = build.download_native_package(spec, item, target, archive_selected=selected)
                self.assertTrue(selected)
        first, second, third = spec["packages"][:3]
        self.assertEqual([call.args[0] for call in fetch.call_args_list], [
            spec["base_url"] + first["filename"],
            "https://archive.ubuntu.com/ubuntu/" + first["filename"],
            "https://archive.ubuntu.com/ubuntu/" + second["filename"],
            "https://archive.ubuntu.com/ubuntu/" + third["filename"],
        ])

    def test_failed_fallback_does_not_persist_a_preference(self):
        spec, item, target = self.inputs()
        with patch.object(build, "download", side_effect=[self.failure(502), ValueError("bad hash")]):
            with self.assertRaisesRegex(ValueError, "bad hash"):
                build.download_native_package(spec, item, target)
        with patch.object(build, "download") as fetch:
            self.assertFalse(build.download_native_package(spec, item, target))
        self.assertEqual(fetch.call_args.args[0], spec["base_url"] + item["filename"])

    def test_selected_archive_failure_never_switches_back(self):
        spec, item, target = self.inputs()
        with patch.object(build, "download", side_effect=self.failure(503)) as fetch:
            with self.assertRaises(urllib.error.HTTPError):
                build.download_native_package(spec, item, target, archive_selected=True)
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0],
                         "https://archive.ubuntu.com/ubuntu/" + item["filename"])

    def test_fresh_native_install_resets_the_preference(self):
        original_read = Path.read_text
        def read_text(path, *args, **kwargs):
            if str(path) == "/etc/os-release":
                return 'ID=ubuntu\nVERSION_ID="24.04"\n'
            return original_read(path, *args, **kwargs)
        manifest = json.loads(build.MANIFEST.read_text())
        for number in range(2):
            stage = self.root / str(number)
            stage.mkdir()
            with patch.object(Path, "read_text", autospec=True, side_effect=read_text), \
                    patch.object(build, "download_native_package", return_value=True) as fetch, \
                    patch.object(headers, "verify_archive", return_value={}), \
                    patch.object(build, "run", side_effect=RuntimeError("stop before apt")):
                with self.assertRaisesRegex(RuntimeError, "stop before apt"):
                    build.install_native(stage, "amd64", manifest)
            selected = [call.kwargs["archive_selected"] for call in fetch.call_args_list]
            self.assertEqual(selected, [False] + [True] * (len(selected) - 1))
            self.assertGreater(len(selected), 1)


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.hosted = {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux",
                       "RUNNER_ARCH": "X64", "RUNNER_ENVIRONMENT": "github-hosted"}

    def test_negative_add_needs_confinement_diagnostic_not_echoed_fixture_name(self):
        for diagnostic in ("invalid archive path: ../sentinel",
                           "entry ../sentinel is outside the destination",
                           "path traversal detected"):
            runtime.require_confinement_failure(diagnostic)
        for unrelated in ("ADD escape.tar /data/: no space left on device",
                          "traversal fixture failed: daemon exited",
                          "COPY /invalid/path: input/output error"):
            with self.subTest(unrelated=unrelated), self.assertRaisesRegex(ValueError, "unproven"):
                runtime.require_confinement_failure(unrelated)

    def test_negative_add_matches_exact_expected_entry_in_final_error(self):
        entry = "../" * 64 + "tmp/host-sentinel"
        diagnostic = "error: failed to solve: invalid entry name " + json.dumps(entry)
        runtime.require_confinement_failure("progress\n" + diagnostic + "\n", entry)
        for unrelated in (diagnostic.replace("host-sentinel", "other-file"),
                          diagnostic + "\nerror: daemon unavailable\n",
                          "Dockerfile: RUN echo " + diagnostic,
                          "error: failed to solve: invalid entry name \"normal-file\"",
                          diagnostic + " (connection lost)"):
            with self.subTest(unrelated=unrelated), self.assertRaisesRegex(ValueError, "unproven"):
                runtime.require_confinement_failure(unrelated, entry)
        with self.assertRaisesRegex(ValueError, "unproven"):
            runtime.require_confinement_failure(diagnostic)
        with self.assertRaisesRegex(ValueError, "unproven"):
            runtime.require_confinement_failure(
                'error: failed to solve: invalid entry name "normal-file"', "normal-file")

    def test_build_fixture_binds_rejection_to_the_created_traversal_entry(self):
        binaries = self.root / "bin"
        binaries.mkdir()
        (binaries / "oci-probe").write_bytes(b"fixture")
        root = self.root / "runtime"
        root.mkdir()

        def command(args, **kwargs):
            if "--output" in args:
                export = root / "export"
                (export / "data").mkdir(parents=True)
                (export / "result").write_text("BUILD_PROBE_OK\n")
                (export / "data/file").write_bytes(b"archive-payload")
            return ""

        def escape(args, work, sentinel):
            with tarfile.open(work / "context/escape.tar") as archive:
                entry = archive.getnames()[0]
            self.assertEqual(entry, "../" * 64 + str(sentinel).lstrip("/"))
            return subprocess.CompletedProcess(args, 1, stdout="", stderr=(
                "error: failed to solve: invalid entry name " + json.dumps(entry) + "\n"))

        with patch.object(runtime, "run", side_effect=command), \
                patch.object(runtime, "escape_build", side_effect=escape), \
                patch.object(runtime.subprocess, "Popen") as daemon, \
                patch.object(runtime.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            daemon.return_value.poll.return_value = None
            runtime.build_fixture(root, binaries)
            daemon.return_value.terminate.assert_called_once()

    def test_timeout_retains_observation_and_original_failure(self):
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"unchanged")
        timeout = subprocess.TimeoutExpired(["buildctl"], 120, output=b"partial", stderr=b" timeout")
        with patch.object(runtime.subprocess, "run", side_effect=timeout), \
                self.assertRaises(subprocess.TimeoutExpired) as caught:
            runtime.escape_build(["buildctl"], self.root, sentinel)
        self.assertIs(caught.exception, timeout)
        report = json.loads((self.root / "escape-result.json").read_text())
        self.assertTrue(report["timed_out"])
        self.assertIs(report["outside_write"], False)
        self.assertEqual((self.root / "escape-build.log").read_text(), "partial timeout")
        with patch.object(runtime.subprocess, "run", side_effect=timeout), \
                patch.object(runtime, "write_json", side_effect=PermissionError("fixture")), \
                self.assertRaises(subprocess.TimeoutExpired) as caught:
            runtime.escape_build(["buildctl"], self.root, sentinel)
        self.assertIs(caught.exception, timeout)

    def test_sentinel_observation_is_bounded_and_does_not_follow_replaced_links(self):
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"unchanged")
        self.assertIs(runtime.observe_sentinel(sentinel)["outside_write"], False)
        with sentinel.open("r+b") as stream:
            stream.truncate(1024 * 1024)
        self.assertIs(runtime.observe_sentinel(sentinel)["outside_write"], True)
        target = self.root / "other"
        sentinel.rename(target)
        sentinel.symlink_to(target)
        self.assertIsNone(runtime.observe_sentinel(sentinel)["outside_write"])

    def test_changed_sentinel_is_recorded_before_failure(self):
        sentinel = self.root / "sentinel"
        sentinel.write_bytes(b"escaped")
        process = subprocess.CompletedProcess(["buildctl"], 0, stdout="done", stderr="")
        with patch.object(runtime.subprocess, "run", return_value=process), \
                self.assertRaisesRegex(ValueError, "sentinel"):
            runtime.escape_build(["buildctl"], self.root, sentinel)
        self.assertIs(json.loads((self.root / "escape-result.json").read_text())["outside_write"], True)

    def test_execution_refuses_mac_or_self_hosted_workers(self):
        self.assertEqual(build.hosted_arch(self.hosted), "amd64")
        for field, value in (("RUNNER_OS", "macOS"), ("RUNNER_ENVIRONMENT", "self-hosted"),
                             ("RUNNER_ARCH", "ARM"), ("GITHUB_ACTIONS", "false")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                build.hosted_arch(dict(self.hosted, **{field: value}))

    def test_privileged_gate_requires_exact_reviewed_checkout_and_manual_event(self):
        commit = "a" * 40
        env = dict(self.hosted, GITHUB_EVENT_NAME="workflow_dispatch", QUALIFY_RUNTIME="true",
                   REVIEWED_COMMIT=commit)
        runtime.review_gate(env, commit)
        for mutation in ({"GITHUB_EVENT_NAME": "pull_request"}, {"QUALIFY_RUNTIME": "false"},
                         {"REVIEWED_COMMIT": "a" * 7}, {"REVIEWED_COMMIT": "b" * 40}):
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                runtime.review_gate(dict(env, **mutation), commit)

    def container_fixture(self, mode, fail=False):
        binaries = self.root / "bin"
        binaries.mkdir(exist_ok=True)
        (binaries / "oci-probe").write_bytes(b"fixture")
        runtime_root = self.root / mode
        runtime_root.mkdir()
        captured = {}

        def command(args, **kwargs):
            if args[1] == "spec":
                runtime.write_json(kwargs["cwd"] / "config.json", {
                    "root": {}, "process": {}, "linux": {},
                })
                return ""
            bundle = Path(args[args.index("--bundle") + 1])
            captured.update(json.loads((bundle / "config.json").read_text()))
            if fail:
                raise ValueError("fixture container failed")
            return "OCI_PROBE_OK " + mode

        with patch.object(runtime, "run", side_effect=command), \
                patch.object(runtime.subprocess, "run") as cleanup:
            if fail:
                with self.assertRaisesRegex(ValueError, "fixture container failed"):
                    runtime.run_container(runtime_root, binaries, mode)
            else:
                runtime.run_container(runtime_root, binaries, mode)
        cleanup.assert_called_once()
        return runtime_root, captured

    def test_restricted_device_policy_never_resets_deny_all_with_partial_wildcard(self):
        _, config = self.container_fixture("restricted")
        rules = config["linux"]["resources"]["devices"]
        # The pinned cgroups emulator treats type 'a' as a complete policy reset,
        # regardless of access. In particular allow+'a'+'m' permits reads too.
        resets = [rule for rule in rules if rule.get("type", "a") == "a"]
        self.assertEqual(resets, [{"allow": False, "access": "rwm"}])
        mknod = [rule for rule in rules if rule["access"] == "m"]
        self.assertEqual({rule["type"] for rule in mknod}, {"b", "c"})
        self.assertTrue(all(rule["allow"] and rule.get("major", -1) == -1
                            and rule.get("minor", -1) == -1 for rule in mknod))
        self.assertEqual(config["linux"]["devices"], [{
            "path": "/dev/zero", "type": "c", "major": 1, "minor": 5,
            "fileMode": 0o666, "uid": 0, "gid": 0,
        }])
        self.assertIn({"allow": True, "type": "c", "major": 1, "minor": 3,
                       "access": "rwm"}, rules)

    def test_container_failure_preserves_exact_submitted_config(self):
        for mode in ("baseline", "restricted"):
            with self.subTest(mode=mode):
                root, config = self.container_fixture(mode, fail=True)
                self.assertEqual(json.loads((root / (mode + "-config.json")).read_text()), config)

    def test_go_configuration_ignores_ambient_cross_toolchain_or_cgo_flags(self):
        with patch.dict(os.environ, {"GOPRIVATE": "*", "CGO_LDFLAGS": "-L/unreviewed",
                                     "GOTOOLCHAIN": "auto", "GOEXPERIMENT": "unreviewed"}):
            env = build.environment("runc", "arm64")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["CGO_ENABLED"], "1")
        self.assertEqual(env["GOARCH"], "arm64")
        self.assertNotIn("CGO_LDFLAGS", env)
        self.assertNotIn("GOPRIVATE", env)
        self.assertNotIn("GOEXPERIMENT", env)

    def test_module_graph_cannot_hide_replacements_or_resolution_errors(self):
        text = '{"Path":"z","Version":"v1.0.0","Dir":"/private"}\n{"Path":"a","Main":true}'
        self.assertEqual(build.locked_modules(text), [{"Path": "a", "Main": True},
                                                    {"Path": "z", "Version": "v1.0.0"}])
        for text in ('{"Path":"a","Replace":{"Dir":"/tmp/override"}}',
                     '{"Path":"a","Error":{"Err":"missing"}}'):
            with self.assertRaises(ValueError):
                build.locked_modules(text)

    def test_vendor_digest_detects_paths_bytes_and_symlinks(self):
        (self.root / "one").write_bytes(b"data")
        initial = build.tree_digest(self.root)
        (self.root / "one").rename(self.root / "two")
        self.assertNotEqual(initial, build.tree_digest(self.root))
        (self.root / "two").write_bytes(b"changed")
        self.assertNotEqual(initial, build.tree_digest(self.root))
        (self.root / "link").symlink_to("two")
        with self.assertRaises(ValueError):
            build.tree_digest(self.root)

    def test_source_extraction_rejects_traversal_and_symlink_write_through(self):
        for entries in ([('source/../outside', tarfile.REGTYPE, b'bad')],
                        [('source/link', tarfile.SYMTYPE, 'inside'),
                         ('source/link/escaped', tarfile.REGTYPE, b'bad')]):
            with self.subTest(entries=entries):
                archive = self.root / "source.tar.gz"
                with tarfile.open(archive, "w:gz") as tf:
                    for name, kind, content in entries:
                        member = tarfile.TarInfo(name)
                        member.type = kind
                        if kind == tarfile.SYMTYPE:
                            member.linkname = content
                            tf.addfile(member)
                        else:
                            member.size = len(content)
                            tf.addfile(member, io.BytesIO(content))
                spec = {"root": "source", "allowed_symlinks": {"link": "inside"},
                        "archive_sha256": build.digest(archive)}
                with self.assertRaises(ValueError):
                    build.unpack(archive, self.root / "destination", spec)
                self.assertFalse((self.root / "outside").exists())

    def scan_fixture(self):
        return {"Results": [{"Target": "bin/" + name, "Type": "gobinary",
                              "Packages": [{"Name": "stdlib"}]} for name in
                             ("buildctl", "buildkitd", "runc", "oci-probe")]}

    def test_empty_or_partial_security_scan_is_not_a_pass(self):
        self.assertEqual(build.trivy_failures(self.scan_fixture(), "binaries"), [])
        for bad in ({}, {"Results": []}, {"Results": self.scan_fixture()["Results"][:2]}):
            with self.assertRaises(ValueError):
                build.trivy_failures(bad, "binaries")
        with self.assertRaises(ValueError):
            build.trivy_failures({}, "native")

    def test_high_findings_are_kept_even_without_a_fixed_version(self):
        data = self.scan_fixture()
        data["Results"][0]["Vulnerabilities"] = [
            {"VulnerabilityID": "GO-example", "PkgName": "example", "Severity": "HIGH"}]
        self.assertEqual(len(build.trivy_failures(data, "binaries")), 1)

    def test_native_scan_requires_exact_epoch_release_architecture_and_membership(self):
        package = {"ID": "zlib1g@1:1.3.dfsg-3.1ubuntu2.3", "Name": "zlib1g", "Epoch": 1,
                   "Version": "1.3.dfsg", "Release": "3.1ubuntu2.3", "Arch": "arm64"}
        expected = [{"name": "zlib1g", "version": "1:1.3.dfsg-3.1ubuntu2.3", "architecture": "arm64"}]
        data = {"Metadata": {"OS": {"Family": "ubuntu", "Name": "24.04"}},
                "Results": [{"Class": "os-pkgs", "Packages": [package]}]}
        self.assertEqual(build.trivy_failures(data, "native", expected), [])
        for field, value in (("Epoch", 0), ("Release", "older"), ("Arch", "amd64")):
            bad = copy.deepcopy(data)
            bad["Results"][0]["Packages"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                build.trivy_failures(bad, "native", expected)
        with self.assertRaises(ValueError):
            build.trivy_failures(data, "native", expected + expected)

    def test_modified_binary_or_failed_scan_prevents_runtime(self):
        output = self.root
        bins = output / "bin"
        bins.mkdir()
        digests = {}
        for name in ("buildctl", "buildkitd", "runc", "oci-probe"):
            (bins / name).write_bytes(name.encode())
            digests[name] = build.digest(bins / name)
        build.write_json(output / "evidence/security/gate.json", {"passed": True})
        build.write_json(output / "evidence/buildkit/provenance.json",
                         {"binaries": {k: v for k, v in digests.items() if k != "runc"}})
        build.write_json(output / "evidence/runc/provenance.json", {"binaries": {"runc": digests["runc"]}})
        build.write_json(output / "candidate.json", {"commit": "a" * 40, "architecture": "amd64",
                         "sources_sha256": build.digest(build.MANIFEST), "binaries": digests})
        runtime.verify_binaries(output, "a" * 40, "amd64")
        (bins / "runc").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "changed after scan"):
            runtime.verify_binaries(output, "a" * 40, "amd64")
        (bins / "runc").write_bytes(b"runc")
        build.write_json(output / "evidence/security/gate.json", {"passed": False})
        with self.assertRaisesRegex(ValueError, "security gate"):
            runtime.verify_binaries(output, "a" * 40, "amd64")

    def test_new_derivative_labels_are_embedded_in_every_candidate_binary(self):
        manifest = json.loads(build.MANIFEST.read_text())
        self.assertEqual(manifest["go_version"], "go1.27.4")
        for name, spec in manifest["components"].items():
            suffix = spec["downstream_version"].split("+", 1)[1]
            self.assertEqual(suffix, "victron.security.2")
            for command in build.commands(name, spec, self.root).values():
                flags = command[command.index("-ldflags") + 1]
                if name == "runc":
                    self.assertIn("main.extraVersion=" + suffix, flags)
                else:
                    self.assertIn("Version=" + spec["downstream_version"], flags)
                    self.assertIn("Revision=" + spec["commit"] + "+" + suffix, flags)
                self.assertNotIn("victron.security.1", flags)

    def test_unreviewed_locks_cannot_enter_native_build_scan_or_bundle(self):
        manifest = json.loads(build.MANIFEST.read_text())
        manifest["locks_reviewed"] = False
        path = self.root / "unreviewed.json"
        build.write_json(path, manifest)
        for action in ("native", "harden", "build", "scan", "bundle"):
            with self.subTest(action=action), patch.object(build, "MANIFEST", path), \
                    patch.object(sys, "argv", ["build_oci_tools.py", action]), \
                    patch.object(build, "hosted_arch", return_value="amd64"), \
                    patch.object(build.platform, "machine", return_value="x86_64"), \
                    patch.object(build, "stage_dir", return_value=self.root), \
                    patch.object(build, "install_native") as native, \
                    patch.object(build, "build_component") as compile_source, \
                    patch.object(build, "scan") as scan, \
                    patch.object(build.tarfile, "open") as bundle:
                with self.assertRaisesRegex(ValueError, "Lock preparation/review must finish"):
                    build.main()
                for boundary in (native, compile_source, scan, bundle):
                    boundary.assert_not_called()

    def test_reviewed_locks_bind_sources_and_full_graph(self):
        manifest = json.loads(build.MANIFEST.read_text())
        build.validate_locks(manifest)
        corrupted = copy.deepcopy(manifest)
        corrupted["components"]["runc"]["commit"] = "f" * 40
        with self.assertRaises(ValueError):
            build.validate_locks(corrupted)
        for name, spec in manifest["components"].items():
            modules = json.loads((build.INPUTS / name / "modules.json").read_text())
            if manifest["locks_reviewed"]:
                self.assertGreater(len(modules), 15)
            else:
                self.assertIsNone(spec["vendor_sha256"])
            self.assertEqual(spec["locked_files"]["modules.json"],
                             build.digest(build.INPUTS / name / "modules.json"))

    def test_native_inputs_are_architecture_specific_and_hash_pinned(self):
        for arch in ("amd64", "arm64"):
            data = json.loads((build.INPUTS / f"native-{arch}.json").read_text())
            names = {p["name"] for p in data["packages"]}
            self.assertTrue(set(data["roots"]) <= names)
            self.assertIn("libseccomp-dev", names)
            self.assertIn("libc6-dev", names)
            for package in data["packages"]:
                self.assertIn(package["architecture"], (arch, "all"))
                self.assertRegex(package["sha256"], r"^[a-f0-9]{64}$")
                self.assertGreater(package["size"], 0)

    def test_native_zlib_headers_match_the_locked_runtime(self):
        for arch in ("amd64", "arm64"):
            data = json.loads((build.INPUTS / f"native-{arch}.json").read_text())
            packages = {package["name"]: package for package in data["packages"]}
            self.assertIn("zlib1g-dev", data["roots"])
            self.assertEqual(packages["zlib1g-dev"]["version"], packages["zlib1g"]["version"])
            self.assertEqual(packages["zlib1g-dev"]["architecture"], arch)

    def test_native_install_keeps_no_remove_with_both_zlib_packages(self):
        original_read = Path.read_text

        def read_text(path, *args, **kwargs):
            if str(path) == "/etc/os-release":
                return 'ID=ubuntu\nVERSION_ID="24.04"\n'
            return original_read(path, *args, **kwargs)

        manifest = json.loads(build.MANIFEST.read_text())
        for arch in ("amd64", "arm64"):
            stage = self.root / arch
            stage.mkdir()
            with patch.object(Path, "read_text", autospec=True, side_effect=read_text), \
                    patch.object(build, "download"), \
                    patch.object(headers, "verify_archive", return_value={}), \
                    patch.object(build, "run", side_effect=RuntimeError("stop before apt")) as run:
                with self.assertRaisesRegex(RuntimeError, "stop before apt"):
                    build.install_native(stage, arch, manifest)
            run.assert_called_once()
            arguments = [str(value) for value in run.call_args.args[0]]
            self.assertIn("--no-remove", arguments)
            self.assertIn("--no-download", arguments)
            data = json.loads((build.INPUTS / f"native-{arch}.json").read_text())
            for package in data["packages"]:
                if package["name"] in {"zlib1g", "zlib1g-dev"}:
                    filename = f"{package['name']}_{package['version'].replace(':', '%3a')}_{arch}.deb"
                    self.assertIn(str(stage / "debs" / filename), arguments)

    def test_omitted_or_extra_lock_key_cannot_bypass_verification(self):
        target = self.root / "inputs"
        shutil.copytree(build.INPUTS, target)
        for key, add in (("go.sum", False), ("unchecked.txt", True)):
            manifest = json.loads(build.MANIFEST.read_text())
            spec = manifest["components"]["runc"]
            if add:
                spec["locked_files"][key] = "0" * 64
            else:
                del spec["locked_files"][key]
            build.write_json(target / "runc/inputs.json", {"go_version": manifest["go_version"], "source": spec})
            with patch.object(build, "INPUTS", target), self.assertRaisesRegex(ValueError, "Lock inventory"):
                build.validate_locks(manifest)

    def test_govulncheck_fixtures_match_pinned_upstream_hashes(self):
        directory = ROOT / "tests/oci_fixtures/govulncheck"
        for item in json.loads((directory / "sources.json").read_text()):
            with self.subTest(file=item["file"]):
                self.assertEqual(build.digest(directory / item["file"]), item["sha256"])

    def golden(self, filename, index=0):
        """Extract one unmodified JSON scenario from the pinned upstream .ct file."""
        content = (ROOT / "tests/oci_fixtures/govulncheck" / filename).read_text()
        block = re.split(r"(?m)^\$[^\n]*\n", content)[index + 1]
        block = block[block.index("{"):]
        records = []
        decoder = json.JSONDecoder()
        while block.lstrip().startswith("{"):
            block = block.lstrip()
            value, end = decoder.raw_decode(block)
            records.append(value)
            block = block[end:]
        return records

    def test_authentic_clean_binary_stream_passes_but_config_alone_does_not(self):
        records = self.golden("binary_call_json.ct")
        report = "\n".join(map(json.dumps, records))
        self.assertTrue(vulnerability.evaluate(report, "binary", completed=True)["passed"])
        with self.assertRaisesRegex(ValueError, "SBOM"):
            vulnerability.evaluate(json.dumps(records[0]), "binary", completed=True)
        with self.assertRaisesRegex(ValueError, "complete"):
            vulnerability.evaluate(report, "binary", completed=False)

    def test_authentic_json_findings_fail_even_with_successful_process_exit(self):
        records = self.golden("binary_call_json.ct", 1)
        summary = vulnerability.evaluate("\n".join(map(json.dumps, records)), "binary", completed=True)
        self.assertFalse(summary["passed"])
        self.assertTrue(any(f["level"] == "symbol_present" and f["status"] == "affected" for f in summary["findings"]))
        self.assertTrue(any(f["status"] == "review_required" for f in summary["findings"]))
        self.assertEqual(summary["suppressed_findings"], [])

    def test_authentic_source_trace_is_reachable_but_not_a_binary_trace(self):
        records = self.golden("source_call_json.ct")
        findings = vulnerability.classify_findings(records, "source")
        self.assertTrue(any(f["level"] == "reachable_call" and len(f["trace"]) > 1 for f in findings))
        with self.assertRaisesRegex(ValueError, "binary trace"):
            vulnerability.classify_findings(records, "binary")

    def test_malformed_incomplete_and_wrong_target_scans_are_rejected(self):
        clean = self.golden("binary_call_json.ct")
        report = "\n".join(map(json.dumps, clean))
        for bad in ("", report + "{", report + json.dumps(clean[0]),
                    '{"config":{},"config":{}}'):
            with self.subTest(bad=bad[:50]), self.assertRaises(ValueError):
                vulnerability.evaluate(bad, "binary", completed=True)
        with self.assertRaisesRegex(ValueError, "different root"):
            vulnerability.evaluate(report, "binary", completed=True, expected_root="wrong")
        with self.assertRaisesRegex(ValueError, "mode"):
            vulnerability.evaluate(report, "source", completed=True)
        vulnerable = self.golden("binary_call_json.ct", 1)
        next(r["finding"] for r in vulnerable if "finding" in r)["trace"] = []
        with self.assertRaisesRegex(ValueError, "trace"):
            vulnerability.evaluate("\n".join(map(json.dumps, vulnerable)), "binary", completed=True)


class HeaderApplicabilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = json.loads(build.MANIFEST.read_text())
        self.packages = json.loads((build.INPUTS / "native-amd64.json").read_text())["packages"]
        self.policy, self.binding, _ = headers.load_policy(
            build.INPUTS, self.manifest["header_disposition_sha256"], "amd64", self.packages)

    def installed_fixture(self):
        path = self.root / headers.CONNTRACK_HEADER
        path.parent.mkdir(parents=True)
        path.write_bytes(b"#define _NF_CONNTRACK_COMMON_H\n#define SAFE_USERSPACE 1\n")
        path.chmod(0o644)
        files = [{"path": headers.CONNTRACK_HEADER, "type": "file", "size": path.stat().st_size,
                  "mode": 0o644, "uid": path.stat().st_uid, "gid": path.stat().st_gid,
                  "sha256": headers.sha256(path)}]
        dirs = [{"path": p.relative_to(self.root).as_posix(), "uid": p.stat().st_uid,
                 "gid": p.stat().st_gid, "mode": stat.S_IMODE(p.stat().st_mode)}
                for p in [self.root, *path.parents]
                if p == self.root or p.is_relative_to(self.root)]
        dirs = list({d["path"]: d for d in dirs}.values())
        owned = ["/" + d["path"] for d in dirs] + ["/" + headers.CONNTRACK_HEADER]
        return path, files, owned, dirs

    def disposition_fixture(self):
        policy = copy.deepcopy(self.policy)
        policy["reviewed"] = True
        package = {"Name": self.binding["name"], "ID": self.binding["name"] + "@" + self.binding["version"],
                   "Arch": "amd64", "SrcName": "linux", "Version": "6.8.0", "Release": "147.147"}
        finding = {"VulnerabilityID": policy["cve_ids"][0], "Severity": "HIGH",
                   "PkgName": package["Name"], "PkgID": package["ID"],
                   "InstalledVersion": self.binding["version"], "DataSource": {"ID": "ubuntu"}}
        data = {"Metadata": {"OS": policy["distro"]},
                "Results": [{"Class": "os-pkgs", "Type": "ubuntu", "Packages": [package],
                             "Vulnerabilities": [finding]}]}
        receipt = {"verified": True, "archive_verified": True, "ownership_verified": True,
                   "package": self.binding,
                   "policy_sha256": self.manifest["header_disposition_sha256"],
                   "file_count": self.binding["file_count"], "bytes": self.binding["total_bytes"],
                   "exported_conntrack": self.binding["exported_conntrack"]}
        return policy, data, receipt

    def classify(self, policy, data, receipt):
        return headers.classify(data, policy, self.binding, receipt,
                                self.manifest["header_disposition_sha256"])

    def test_pending_review_never_exempts_a_finding(self):
        _, data, receipt = self.disposition_fixture()
        policy = dict(self.policy, reviewed=False)
        self.assertEqual(self.classify(policy, data, receipt), [])

    def test_exact_reviewed_fixture_preserves_raw_findings(self):
        policy, data, receipt = self.disposition_fixture()
        original = copy.deepcopy(data)
        findings = self.classify(policy, data, receipt)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["status"], "not_affected")
        view = headers.remaining_findings(data, findings)
        self.assertEqual(view["Results"][0]["Vulnerabilities"], [])
        self.assertEqual(data, original)

    def test_scanner_finding_requires_its_own_package_inventory(self):
        policy, data, receipt = self.disposition_fixture()
        original = data["Results"][0]
        finding = original["Vulnerabilities"].pop()
        data["Results"].append({"Class": "os-pkgs", "Type": "ubuntu", "Packages": [],
                                "Vulnerabilities": [finding]})
        dispositions = self.classify(policy, data, receipt)
        self.assertEqual(dispositions, [])
        remaining = headers.remaining_findings(data, dispositions)
        self.assertEqual(build.trivy_failures(remaining, "native", [self.binding]),
                         build.trivy_failures(data, "native", [self.binding]))
        self.assertEqual(len(build.trivy_failures(remaining, "native", [self.binding])), 1)

    def test_scanner_result_type_must_be_ubuntu(self):
        for kind in (None, "debian", "gobinary", ""):
            policy, data, receipt = self.disposition_fixture()
            data["Results"][0]["Type"] = kind
            with self.subTest(kind=kind):
                self.assertEqual(self.classify(policy, data, receipt), [])

    def test_unreviewed_ids_packages_versions_and_scanners_still_block(self):
        for key, value in (("VulnerabilityID", "CVE-2999-12345"), ("PkgName", "libc6"),
                           ("InstalledVersion", "6.8.0-other"), ("PkgID", "other@1"),
                           ("DataSource", {"ID": "debian"})):
            policy, data, receipt = self.disposition_fixture()
            data["Results"][0]["Vulnerabilities"][0][key] = value
            with self.subTest(key=key):
                self.assertEqual(self.classify(policy, data, receipt), [])

    def test_duplicate_display_label_does_not_remove_unreviewed_finding(self):
        policy, data, receipt = self.disposition_fixture()
        wrong = copy.deepcopy(data["Results"][0]["Vulnerabilities"][0])
        wrong["InstalledVersion"] = "other"
        data["Results"][0]["Vulnerabilities"].append(wrong)
        dispositions = self.classify(policy, data, receipt)
        view = headers.remaining_findings(data, dispositions)
        self.assertEqual(view["Results"][0]["Vulnerabilities"], [wrong])
        self.assertEqual(len(build.trivy_failures(view, "native", [self.binding])), 1)

    def test_binary_and_linked_native_findings_are_never_exempted(self):
        policy, data, receipt = self.disposition_fixture()
        bad = copy.deepcopy(data["Results"][0]["Vulnerabilities"][0])
        bad["PkgName"] = "libseccomp2"
        data["Results"][0]["Vulnerabilities"].append(bad)
        data["Results"].append({"Class": "lang-pkgs", "Type": "gobinary",
                                "Vulnerabilities": [copy.deepcopy(bad)]})
        view = headers.remaining_findings(data, self.classify(policy, data, receipt))
        self.assertEqual(sum(len(r["Vulnerabilities"]) for r in view["Results"]), 2)

    def test_missing_or_changed_install_and_archive_receipts_fail(self):
        for key, value in (("verified", False), ("archive_verified", False), ("ownership_verified", False),
                           ("package", {}),
                           ("policy_sha256", "f" * 64), ("file_count", 0), ("bytes", 0),
                           ("exported_conntrack", {})):
            policy, data, receipt = self.disposition_fixture()
            receipt[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.classify(policy, data, receipt)
        for key, value in (("Arch", "arm64"), ("SrcName", "other")):
            policy, data, receipt = self.disposition_fixture()
            data["Results"][0]["Packages"][0][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.classify(policy, data, receipt)

    def test_policy_archive_and_per_id_evidence_are_bound(self):
        target = self.root / "inputs"
        shutil.copytree(build.INPUTS, target)
        for name in ("header-disposition.json", "header-cve-evidence.json",
                     "linux-libc-dev-files-amd64.json", "linux-libc-dev-archive-amd64.json"):
            path = target / name
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            with self.subTest(name=name), self.assertRaises(ValueError):
                headers.load_policy(target, self.manifest["header_disposition_sha256"], "amd64", self.packages)
            path.write_bytes(original)
        for key, value in (("sha256", "0" * 64), ("version", "other"), ("architecture", "arm64"), ("size", 1)):
            packages = copy.deepcopy(self.packages)
            next(p for p in packages if p["name"] == "linux-libc-dev")[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                headers.load_policy(target, self.manifest["header_disposition_sha256"], "amd64", packages)

    def test_installed_exact_bytes_pass_and_changes_fail(self):
        path, files, owned, dirs = self.installed_fixture()
        result = headers.verify_installed(self.root, files, owned, dirs)
        self.assertTrue(result["exported_conntrack"]["kernel_guard_absent"])
        original = path.read_bytes()
        path.write_bytes(original.replace(b"1", b"2"))
        with self.assertRaises(ValueError):
            headers.verify_installed(self.root, files, owned, dirs)
        path.write_bytes(original)
        for mode in (0o755, 0o666):
            path.chmod(mode)
            with self.assertRaises(ValueError):
                headers.verify_installed(self.root, files, owned, dirs)

    def test_installed_file_ownership_must_match_manifest(self):
        _, files, owned, dirs = self.installed_fixture()
        for field in ("uid", "gid"):
            altered = copy.deepcopy(files)
            altered[0][field] = 0 if files[0][field] else 1
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "owner"):
                headers.verify_installed(self.root, altered, owned, dirs)

    def test_installed_directory_ownership_and_permissions_must_match(self):
        _, files, owned, dirs = self.installed_fixture()
        for index, directory in enumerate(dirs):
            for field in ("uid", "gid", "mode"):
                altered = copy.deepcopy(dirs)
                altered[index][field] = (directory[field] ^ 0o020) if field == "mode" else directory[field] + 1
                with self.subTest(path=directory["path"], field=field), self.assertRaisesRegex(ValueError, "directory"):
                    headers.verify_installed(self.root, files, owned, altered)

    def test_missing_extra_duplicate_and_symlink_installed_members_fail(self):
        path, files, owned, dirs = self.installed_fixture()
        extra = self.root / "extra.h"
        extra.write_text("extra")
        for paths in (owned[:-1], owned + ["/extra.h"], owned + [owned[-1]]):
            with self.assertRaises(ValueError):
                headers.verify_installed(self.root, files, paths, dirs)
        path.unlink()
        path.symlink_to(extra)
        with self.assertRaises(ValueError):
            headers.verify_installed(self.root, files, owned, dirs)

    def test_parent_symlink_and_non_normal_paths_fail(self):
        path, files, owned, dirs = self.installed_fixture()
        parent = path.parent
        parent.rename(parent.with_name("real"))
        parent.symlink_to(parent.with_name("real"), target_is_directory=True)
        with self.assertRaises((ValueError, OSError)):
            headers.verify_installed(self.root, files, owned, dirs)
        for name in ("/tmp/file", "../file", "a/../file", "a//file", "a/./file", "a\\file"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                headers.normal_path(name)

    def test_exported_header_rejects_kernel_branch_or_flag(self):
        for data in (b"#define _NF_CONNTRACK_COMMON_H\n#ifdef __KERNEL__",
                     b"#define _NF_CONNTRACK_COMMON_H\n#define NF_CT_EXPECT_DEAD 8", b"wrong"):
            with self.assertRaises(ValueError):
                headers.verify_exported_conntrack(data)

    def test_tar_special_duplicate_and_executable_payloads_fail(self):
        for kind, mode, payload, repeat in ((tarfile.SYMTYPE, 0o644, b"", 1),
                                           (tarfile.LNKTYPE, 0o644, b"", 1),
                                           (tarfile.FIFOTYPE, 0o644, b"", 1),
                                           (tarfile.REGTYPE, 0o755, b"text", 1),
                                           (tarfile.REGTYPE, 0o644, b"\x7fELFdata", 1),
                                           (tarfile.REGTYPE, 0o644, b"text", 2)):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w") as tf:
                for _ in range(repeat):
                    member = tarfile.TarInfo("usr/include/fixture.h")
                    member.type, member.mode, member.size = kind, mode, len(payload)
                    tf.addfile(member, io.BytesIO(payload))
            with self.subTest(kind=kind, mode=mode, repeat=repeat), self.assertRaises(ValueError):
                headers.tar_inventory(stream.getvalue())

    def test_archive_hash_is_checked_before_decompression(self):
        path = self.root / "fake.deb"
        path.write_bytes(b"untrusted")
        with patch.object(headers.subprocess, "run") as command, self.assertRaises(ValueError):
            headers.inspect_deb(path, {"size": path.stat().st_size, "sha256": "0" * 64})
        command.assert_not_called()

    def test_control_script_cannot_be_omitted_from_archive_audit(self):
        path = self.root / "fake.deb"
        data = b"!<arch>\n"
        for name, content in (("debian-binary", b"2.0\n"), ("control.tar.zst", b"00"), ("data.tar.zst", b"00")):
            record = f"{name:<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n".encode()
            self.assertEqual(len(record), 60)
            data += record + content
        path.write_bytes(data)
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for name in ("control", "md5sums", "postinst"):
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(b""))
        command_result = subprocess.CompletedProcess([], 0, stdout=stream.getvalue())
        with patch.object(headers.subprocess, "run", return_value=command_result), \
                self.assertRaisesRegex(ValueError, "control script"):
            headers.inspect_deb(path, {"size": len(data), "sha256": headers.sha256(path)})


    def hardening_fixture(self):
        self.root.chmod(0o755)
        package_dir = self.root / "usr/share/doc/linux-libc-dev"
        package_dir.mkdir(parents=True)
        files = []
        for name in ("changelog.Debian.gz", "copyright"):
            path = package_dir / name
            path.write_bytes(name.encode())
            files.append({"path": path.relative_to(self.root).as_posix(), "size": path.stat().st_size,
                          "sha256": headers.sha256(path), "mode": 0o644,
                          "uid": path.stat().st_uid, "gid": path.stat().st_gid})
            path.chmod(0o777)
        dirs = []
        for name in (".", "usr", "usr/share", "usr/share/doc", "usr/share/doc/linux-libc-dev"):
            path = self.root / name
            path.chmod(0o755 if name in (".", "usr") else 0o777)
            dirs.append({"path": name, "uid": path.stat().st_uid, "gid": path.stat().st_gid, "mode": 0o755})
        return files, dirs

    def test_hardening_changes_exactly_five_paths_and_is_idempotent(self):
        files, dirs = self.hardening_fixture()
        neighbor = self.root / "usr/share/unrelated"
        neighbor.write_bytes(b"untouched")
        neighbor.chmod(0o777)
        actions = []
        headers.harden_hosted_metadata(self.root, files, dirs, actions)
        expected = {f["path"] for f in files} | {d["path"] for d in dirs if d["path"] not in (".", "usr")}
        self.assertEqual({a["path"] for a in actions if a["changed"]}, expected)
        self.assertEqual(stat.S_IMODE(neighbor.stat().st_mode), 0o777)
        self.assertEqual(neighbor.read_bytes(), b"untouched")
        again = []
        headers.harden_hosted_metadata(self.root, files, dirs, again)
        self.assertFalse(any(a["changed"] for a in again))

    def test_hardening_rejects_unreviewed_ancestor_modes(self):
        files, dirs = self.hardening_fixture()
        for name, mode in ((".", 0o777), ("usr", 0o777), ("usr/share", 0o775)):
            path = self.root / name
            original = stat.S_IMODE(path.stat().st_mode)
            path.chmod(mode)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "starting mode"):
                headers.harden_hosted_metadata(self.root, files, dirs, [])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            path.chmod(original)

    def test_hardening_rejects_wrong_owner_and_doc_bytes_before_chmod(self):
        files, dirs = self.hardening_fixture()
        for field in ("uid", "gid"):
            wrong = copy.deepcopy(dirs)
            next(d for d in wrong if d["path"] == "usr/share")[field] += 1
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "ownership"):
                headers.harden_hosted_metadata(self.root, files, wrong, [])
            self.assertEqual(stat.S_IMODE((self.root / "usr/share").stat().st_mode), 0o777)
        notice = self.root / files[0]["path"]
        notice.write_bytes(b"x" * files[0]["size"])
        with self.assertRaisesRegex(ValueError, "bytes"):
            headers.harden_hosted_metadata(self.root, files, dirs, [])
        self.assertEqual(stat.S_IMODE(notice.stat().st_mode), 0o777)

    def test_hardening_never_follows_substituted_directory(self):
        files, dirs = self.hardening_fixture()
        path = self.root / "usr/share"
        moved = self.root / "moved"
        path.rename(moved)
        path.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OSError):
            headers.harden_hosted_metadata(self.root, files, dirs, [])
        self.assertEqual(stat.S_IMODE(moved.stat().st_mode), 0o777)

    def test_hardening_rejects_hardlinked_notice_without_changing_alias(self):
        files, dirs = self.hardening_fixture()
        notice = self.root / files[0]["path"]
        alias = self.root / "outside-notice-alias"
        os.link(notice, alias)
        self.assertEqual(notice.stat().st_nlink, 2)
        with self.assertRaisesRegex(ValueError, "hard link"):
            headers.harden_hosted_metadata(self.root, files, dirs, [])
        self.assertEqual(stat.S_IMODE(alias.stat().st_mode), 0o777)
        self.assertEqual(stat.S_IMODE(notice.stat().st_mode), 0o777)

    def test_hardening_rejects_link_count_drift_during_chmod(self):
        files, dirs = self.hardening_fixture()
        notice = self.root / files[0]["path"]
        alias = self.root / "concurrent-alias"
        original_fchmod = os.fchmod

        def link_during_chmod(fd, mode):
            if mode == 0o644 and not alias.exists():
                os.link(notice, alias)
            original_fchmod(fd, mode)

        actions = []
        with patch.object(headers.os, "fchmod", side_effect=link_during_chmod), \
                self.assertRaisesRegex(ValueError, "hard link appeared during"):
            headers.harden_hosted_metadata(self.root, files, dirs, actions)
        self.assertEqual(notice.stat().st_nlink, 2)
        self.assertTrue(actions[-1]["changed"])
        self.assertNotIn("after_mode", actions[-1])

    def test_hardening_uses_verified_fd_if_file_path_is_replaced(self):
        files, dirs = self.hardening_fixture()
        notice = self.root / files[0]["path"]
        outside = self.root / "outside"
        outside.write_bytes(b"must not chmod")
        outside.chmod(0o777)
        original_fchmod = os.fchmod
        replaced = False

        def replace_then_chmod(fd, mode):
            nonlocal replaced
            if mode == 0o644 and not replaced:
                notice.rename(notice.with_name("original"))
                notice.symlink_to(outside)
                replaced = True
            original_fchmod(fd, mode)

        with patch.object(headers.os, "fchmod", side_effect=replace_then_chmod):
            headers.harden_hosted_metadata(self.root, files, dirs, [])
        self.assertTrue(replaced)
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o777)
        self.assertEqual(outside.read_bytes(), b"must not chmod")
        owned = ["/" + entry["path"] for entry in [*dirs, *files]]
        with self.assertRaisesRegex(ValueError, "Unexpected installed header payload"):
            headers.verify_installed(self.root, files, owned, dirs)


if __name__ == "__main__":
    unittest.main()
