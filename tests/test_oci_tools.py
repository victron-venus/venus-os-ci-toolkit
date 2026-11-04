"""Local adversarial tests; no network, Go compilation, root or daemons."""

import copy
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_oci_tools as build  # noqa: E402
import qualify_oci_tools as runtime  # noqa: E402
import oci_vulnerability as vulnerability  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
