"""The release client installer accepts only the reviewed native artifact."""
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_release_gh as installer
import release_gh_workflow

ROOT = Path(__file__).resolve().parents[1]


def fixture(kind="tar", *, duplicate=False, symlink=False):
    data = b"synthetic executable"
    stream = io.BytesIO()
    name = "release/bin/gh"
    if kind == "zip":
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr(name, data)
    else:
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            if symlink:
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
            archive.addfile(member, io.BytesIO(data))
            if duplicate:
                archive.addfile(member, io.BytesIO(data))
    raw = stream.getvalue()
    artifact = {"asset": "release.zip" if kind == "zip" else "release.tar.gz", "member": name,
                "archive_sha256": installer.digest(raw), "binary_sha256": installer.digest(data)}
    return raw, artifact, data


class InstallReleaseGhTests(unittest.TestCase):
    def test_exact_tar_and_zip_and_hashes(self):
        for kind in ("tar", "zip"):
            raw, artifact, expected = fixture(kind)
            self.assertEqual(installer.binary_from_archive(raw, artifact), expected)
            with self.assertRaisesRegex(ValueError, "archive checksum"):
                installer.binary_from_archive(raw + b"tamper", artifact)
            artifact["binary_sha256"] = "0" * 64
            with self.assertRaisesRegex(ValueError, "executable differs"):
                installer.binary_from_archive(raw, artifact)

    def test_ambiguous_and_link_members_are_rejected(self):
        for option in ({"duplicate": True}, {"symlink": True}):
            raw, artifact, _ = fixture(**option)
            with self.assertRaisesRegex(ValueError, "member"):
                installer.binary_from_archive(raw, artifact)

    def test_tampering_preserves_previous_installed_client(self):
        raw, artifact, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "gh"
            output.write_bytes(b"old")
            with patch.object(installer, "download", return_value=raw + b"tamper"), self.assertRaises(ValueError):
                installer.install(output, {"version": "test"}, artifact)
            self.assertEqual(output.read_bytes(), b"old")

    def test_offline_archive_installs_atomically_without_downloader(self):
        raw, artifact, binary = fixture()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "download.tar.gz"
            archive.write_bytes(raw)
            output = Path(directory) / "gh"
            output.write_bytes(b"old")

            def verify(candidate, actual_artifact, version):
                self.assertEqual(output.read_bytes(), b"old")
                self.assertEqual(candidate.read_bytes(), binary)
                self.assertEqual(candidate.stat().st_mode & 0o777, 0o755)
                self.assertEqual(actual_artifact, artifact)
                self.assertEqual(version, "test")

            with patch.object(installer, "download") as download, \
                    patch.object(installer, "verify", side_effect=verify):
                installer.install(output, {"version": "test"}, artifact, archive)
                download.assert_not_called()
            self.assertEqual(output.read_bytes(), binary)
            self.assertEqual(list(Path(directory).glob(".gh-*")), [])

    def test_offline_corrupt_wrong_arch_and_oversize_never_fall_back(self):
        raw, artifact, _ = fixture()
        wrong_arch = dict(artifact, archive_sha256="0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "download.tar.gz"
            output = Path(directory) / "gh"
            output.write_bytes(b"old")
            cases = [(raw + b"corrupt", artifact, installer.MAX_ARCHIVE),
                     (raw, wrong_arch, installer.MAX_ARCHIVE), (raw, artifact, len(raw) - 1)]
            for data, selected_artifact, limit in cases:
                archive.write_bytes(data)
                with self.subTest(limit=limit, artifact=selected_artifact), \
                        patch.object(installer, "MAX_ARCHIVE", limit), \
                        patch.object(installer, "download") as download, \
                        patch.object(installer, "verify") as verify, self.assertRaises(ValueError):
                    installer.install(output, {"version": "test"}, selected_artifact, archive)
                download.assert_not_called()
                verify.assert_not_called()
                self.assertEqual(output.read_bytes(), b"old")

    def test_offline_runtime_verification_failure_preserves_old_output(self):
        raw, artifact, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "download.tar.gz"
            archive.write_bytes(raw)
            output = Path(directory) / "gh"
            output.write_bytes(b"old")
            with patch.object(installer, "verify", side_effect=ValueError("version mismatch")), \
                    self.assertRaisesRegex(ValueError, "version mismatch"):
                installer.install(output, {"version": "test"}, artifact, archive)
            self.assertEqual(output.read_bytes(), b"old")
            self.assertEqual(list(Path(directory).glob(".gh-*")), [])

    def test_offline_archive_never_extracts_other_member_paths(self):
        _, artifact, binary = fixture()
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            for name, data in [("../outside", b"bad"), ("/absolute", b"bad"),
                               (artifact["member"], binary)]:
                member = tarfile.TarInfo(name)
                member.size = len(data)
                tar.addfile(member, io.BytesIO(data))
        raw = stream.getvalue()
        artifact["archive_sha256"] = installer.digest(raw)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "download.tar.gz"
            archive.write_bytes(raw)
            output = Path(directory) / "gh"
            with patch.object(installer, "verify"):
                installer.install(output, {"version": "test"}, artifact, archive)
            self.assertEqual(output.read_bytes(), binary)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["download.tar.gz", "gh"])

    def test_archive_option_cannot_be_used_with_check(self):
        with patch.object(sys, "argv", ["install_release_gh.py", "--check", "/unused", "--archive", "/unused"]), \
                patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
            installer.main()
        self.assertEqual(error.exception.code, 2)

    def test_private_image_archive_url_matches_reviewed_manifest(self):
        dockerfile = (ROOT / "deploy/arc-private/Dockerfile").read_text()
        manifest = json.loads((ROOT / ".github/gh/release.json").read_text())
        urls = re.findall(r'https://github.com/cli/cli/releases/download/[^"\s]+', dockerfile)
        self.assertEqual(len(urls), 1)
        for arch in ("amd64", "arm64"):
            asset = manifest["artifacts"][f"linux_{arch}"]["asset"]
            expected = f"https://github.com/cli/cli/releases/download/v{manifest['version']}/{asset}"
            self.assertEqual(urls[0].replace("${arch}", arch), expected)

    def test_runtime_policy_and_binary_are_checked_before_execution(self):
        raw, artifact, binary = fixture()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gh"
            path.write_bytes(binary)
            with patch.object(installer.subprocess, "run") as run:
                with patch.dict(os.environ, {"GODEBUG": "fips140=off"}), self.assertRaisesRegex(ValueError, "GODEBUG"):
                    installer.verify(path, artifact, "test")
                path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "qualified native build"):
                    installer.verify(path, artifact, "test")
                run.assert_not_called()

    def test_downloader_requires_exact_toolchain_before_network(self):
        for version in ("go1.27.3", "go1.27.40", "go1.28.0", "devel go1.27.4"):
            with self.subTest(version=version), patch.object(installer.subprocess, "run") as run:
                run.return_value.stdout = f"go version {version} linux/amd64\n"
                with self.assertRaisesRegex(ValueError, "exactly Go 1.27.4"):
                    installer.download("https://example.invalid/test")
                self.assertEqual(run.call_count, 1)
                env = run.call_args.kwargs["env"]
                self.assertEqual(env["GOTOOLCHAIN"], "local")
                self.assertEqual(env["GO111MODULE"], "off")
                self.assertEqual(env["GODEBUG"], "fips140=on")

    def test_exact_downloader_toolchain_keeps_isolated_fips_profile(self):
        def run(command, **kwargs):
            env = kwargs["env"]
            expected = {"GOTOOLCHAIN": "local", "GO111MODULE": "off", "GOWORK": "off",
                        "GOENV": "off", "CGO_ENABLED": "0", "GODEBUG": "fips140=on",
                        "GOMAXPROCS": "2", "GOFLAGS": "-p=2"}
            self.assertEqual({key: value for key, value in env.items() if key.startswith(("GO", "CGO"))}, expected)
            if command == ["go", "version"]:
                self.assertEqual(kwargs["timeout"], 15)
                return Mock(stdout="go version go1.27.4 linux/amd64\n")
            self.assertEqual(command[:3], ["go", "run", str(ROOT / "scripts/download_release_gh.go")])
            self.assertEqual(command[3], "https://example.invalid/test")
            self.assertEqual(kwargs["timeout"], 180)
            self.assertEqual(Path(kwargs["cwd"]), Path(command[4]).parent)
            Path(command[4]).write_bytes(b"bounded synthetic archive")
            return Mock()

        with patch.dict(os.environ, {"GOTOOLCHAIN": "auto", "GODEBUG": "fips140=off", "GOPROXY": "private"}), \
                patch.object(installer.subprocess, "run", side_effect=run) as subprocess_run:
            self.assertEqual(installer.download("https://example.invalid/test"), b"bounded synthetic archive")
        self.assertEqual(subprocess_run.call_count, 2)
        self.assertFalse(Path(subprocess_run.call_args.args[0][4]).parent.exists())

    def test_downloader_runtime_and_all_direct_producers_use_one_sdk(self):
        for name in ("download_release_gh.go", "download_release_gh_test.go"):
            self.assertIn('runtime.Version() != "go1.27.4"', (ROOT / "scripts" / name).read_text())
        contracts = yaml.load((ROOT / ".github/workflows/contract-tests.yml").read_text(), Loader=yaml.BaseLoader)
        for job in ("release-gh-contract", "scorecard-contracts"):
            setup = [step for step in contracts["jobs"][job]["steps"]
                     if step.get("uses", "").startswith("actions/setup-go@")]
            self.assertEqual(len(setup), 1)
            self.assertEqual(setup[0]["with"]["go-version"], "1.27.4")
        self.assertRegex((ROOT / "tools/scorecard-scan/go.mod").read_text(), r"(?m)^go 1\.27\.4$")

    def test_every_publication_stage_installs_same_client_after_checkout(self):
        names = ("prepare", "candidate", "stable", "final", "closed_stable_resolve", "closed_existing_resolve")
        workflow = {"jobs": {name: {"steps": [{"uses": "actions/checkout@pin"}, {"run": "publish"}]} for name in names}}
        release_gh_workflow.apply(workflow, {"actions/setup-go": "actions/setup-go@pin"})
        for job in workflow["jobs"].values():
            self.assertEqual(job["steps"][1]["with"]["go-version"], "1.27.4")
            self.assertEqual(job["steps"][2], release_gh_workflow.installer())
            self.assertEqual(job["steps"][3], {"run": "publish"})


if __name__ == "__main__":
    unittest.main()
