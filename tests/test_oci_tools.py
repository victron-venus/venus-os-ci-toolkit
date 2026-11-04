"""Local adversarial tests; no network, Go compilation, root or daemons."""

import copy
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_oci_tools as build  # noqa: E402
import qualify_oci_tools as runtime  # noqa: E402


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.hosted = {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux",
                       "RUNNER_ARCH": "X64", "RUNNER_ENVIRONMENT": "github-hosted"}

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


if __name__ == "__main__":
    unittest.main()
