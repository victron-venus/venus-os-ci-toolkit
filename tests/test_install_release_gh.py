"""The release client installer accepts only the reviewed native artifact."""
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
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
            member = zipfile.ZipInfo(name)
            if symlink:
                member.create_system = 3
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(member, b"../../outside" if symlink else data)
            if duplicate:
                archive.writestr(member, b"duplicate")
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
    def test_committed_producer_sdks_match_the_renderer(self):
        rendered = {"jobs": {"prepare": {"steps": [{"uses": "actions/checkout@fixture"}]}}}
        release_gh_workflow.apply(rendered, {"actions/setup-go": "actions/setup-go@fixture"})
        expected = rendered["jobs"]["prepare"]["steps"][1]["with"]["go-version"]
        workflow = yaml.safe_load((ROOT / ".github/workflows/release-pipeline.yml").read_text())
        producers = []
        for name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            if any("scripts/install_release_gh.py" in step.get("run", "") for step in steps):
                versions = [step["with"]["go-version"] for step in steps
                            if step.get("uses", "").startswith("actions/setup-go@")]
                self.assertEqual(versions, [expected], name)
                producers.append(name)
        self.assertEqual(len(producers), 4)

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
        for kind in ("tar", "zip"):
            for option in ({"duplicate": True}, {"symlink": True}):
                raw, artifact, _ = fixture(kind, **option)
                with self.subTest(kind=kind, **option), self.assertRaisesRegex(ValueError, "member"):
                    installer.binary_from_archive(raw, artifact)

    def test_member_size_limit_applies_to_zip_and_tar_bytes(self):
        for kind in ("tar", "zip"):
            raw, artifact, _ = fixture(kind)
            with self.subTest(kind=kind), patch.object(installer, "MAX_BINARY", 4), \
                    self.assertRaisesRegex(ValueError, "member.*oversized|executable differs"):
                installer.binary_from_archive(raw, artifact)

    def test_truncated_archives_fail_closed(self):
        for kind in ("tar", "zip"):
            raw, artifact, _ = fixture(kind)
            truncated = raw[: max(1, len(raw) // 2)]
            invalid = dict(artifact, archive_sha256=installer.digest(truncated))
            with self.subTest(kind=kind), self.assertRaises((ValueError, OSError, tarfile.TarError, zipfile.BadZipFile)):
                installer.binary_from_archive(truncated, invalid)

    def test_archive_member_must_be_canonical_relative_posix_path(self):
        raw, artifact, _ = fixture()
        for member in ("../gh", "/release/bin/gh", "release\\..\\gh", "release//bin/gh"):
            with self.subTest(member=member):
                invalid = dict(artifact, member=member)
                with self.assertRaisesRegex(ValueError, "canonical and relative"):
                    installer.binary_from_archive(raw, invalid)

    def test_cli_paths_reject_parent_traversal_before_file_operations(self):
        raw, artifact, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "without parent traversal"):
                installer.verify(root / ".." / "gh", artifact, "test")
            with patch.object(installer, "download", return_value=raw) as download, \
                    self.assertRaisesRegex(ValueError, "without parent traversal"):
                installer.install(root / ".." / "gh", {"version": "test"}, artifact)
            download.assert_not_called()

    def test_install_rejects_symlink_escape_and_existing_symlink_output(self):
        raw, artifact, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = Path.home() / "codex-installer-must-not-create"
            escaped_parent = root / "outside"
            escaped_parent.symlink_to(Path.home(), target_is_directory=True)
            with patch.object(installer, "download", return_value=raw) as download, \
                    self.assertRaisesRegex(ValueError, "inside the checkout or runner temp"):
                installer.install(escaped_parent / "gh", {"version": "test"}, artifact)
            download.assert_not_called()
            self.assertFalse(outside.exists())

            real_output = root / "real-gh"
            real_output.write_bytes(b"preserve")
            output_link = root / "gh"
            output_link.symlink_to(real_output)
            with patch.object(installer, "download", return_value=raw) as download, \
                    self.assertRaisesRegex(ValueError, "cannot replace a symlink"):
                installer.install(output_link, {"version": "test"}, artifact)
            download.assert_not_called()
            self.assertEqual(real_output.read_bytes(), b"preserve")

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

            def verify(candidate, actual_artifact, version, *, allow_temporary=False):
                self.assertEqual(output.read_bytes(), b"old")
                self.assertEqual(candidate.read_bytes(), binary)
                self.assertEqual(candidate.stat().st_mode & 0o777, 0o755)
                self.assertEqual(actual_artifact, artifact)
                self.assertEqual(version, "test")
                self.assertTrue(allow_temporary)

            with patch.object(installer, "download") as download, \
                    patch.object(installer, "verify", side_effect=verify):
                installer.install(output, {"version": "test"}, artifact, archive)
                download.assert_not_called()
            self.assertEqual(output.read_bytes(), binary)
            self.assertEqual(list(Path(directory).glob(".gh-*")), [])

    def test_staging_write_and_close_failures_remove_temp_and_preserve_output(self):
        raw, artifact, _ = fixture()
        real_named_temporary_file = tempfile.NamedTemporaryFile
        for failure in ("write", "close"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "download.tar.gz"
                archive.write_bytes(raw)
                output = root / "gh"
                output.write_bytes(b"old")

                def failing_temporary_file(*args, **kwargs):
                    handle = real_named_temporary_file(*args, **kwargs)

                    class FailingWriter:
                        name = handle.name

                        def __enter__(self):
                            return self

                        def write(self, data):
                            if failure == "write":
                                handle.write(data[:1])
                                raise OSError("synthetic staging write failure")
                            return handle.write(data)

                        def __exit__(self, exc_type, exc, traceback):
                            result = handle.__exit__(exc_type, exc, traceback)
                            if failure == "close" and exc_type is None:
                                raise OSError("synthetic staging close failure")
                            return result

                    return FailingWriter()

                with patch.object(installer.tempfile, "NamedTemporaryFile", side_effect=failing_temporary_file), \
                        self.assertRaisesRegex(OSError, "staging (write|close) failure"):
                    installer.install(output, {"version": "test"}, artifact, archive)
                self.assertEqual(output.read_bytes(), b"old")
                self.assertEqual(list(root.glob(".gh-*")), [])

    def test_local_archive_requires_trusted_regular_nonsymlink_file(self):
        raw, _, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allowed = root / "allowed"
            allowed.mkdir()
            archive = allowed / "download.tar.gz"
            archive.write_bytes(raw)
            link = allowed / "archive-link.tar.gz"
            link.symlink_to(archive)
            folder = allowed / "directory"
            folder.mkdir()
            outside = root / "outside.tar.gz"
            outside.write_bytes(raw)
            with patch.object(installer, "_trusted_path_roots", return_value=[allowed]):
                self.assertEqual(installer._read_local_archive(archive), raw)
                for invalid in (link, folder, outside, allowed / ".." / outside.name):
                    with self.subTest(path=invalid), self.assertRaises(ValueError):
                        installer._read_local_archive(invalid)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO files are POSIX-specific")
    def test_local_fifo_archive_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            fifo = Path(directory) / "release.tar.gz"
            os.mkfifo(fifo)
            script = (
                "import sys; from pathlib import Path; "
                f"sys.path.insert(0, {str(ROOT / 'scripts')!r}); "
                "import install_release_gh; "
                "install_release_gh._read_local_archive(Path(sys.argv[1]))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script, str(fifo)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Release CLI archive must be a regular file", result.stderr)

    def test_local_archive_size_limit_preserves_installed_client(self):
        raw, artifact, _ = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "download.tar.gz"
            archive.write_bytes(raw)
            output = root / "gh"
            output.write_bytes(b"old")
            with patch.object(installer, "MAX_ARCHIVE", len(raw) - 1), \
                    self.assertRaisesRegex(ValueError, "archive exceeded its size limit"):
                installer.install(output, {"version": "test"}, artifact, archive)
            self.assertEqual(output.read_bytes(), b"old")

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

    def test_symlinked_runner_temp_uses_resolved_roots_for_staged_verification(self):
        raw, artifact, binary = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            physical = root / "runner-temp-real"
            physical.mkdir()
            runner_alias = root / "runner-temp-link"
            runner_alias.symlink_to(physical, target_is_directory=True)
            unrelated_temp = root / "unrelated-system-temp"
            unrelated_temp.mkdir()
            archive = physical / "archive.tar.gz"
            archive.write_bytes(raw)
            output = runner_alias / "nested" / "gh"
            with patch.dict(os.environ, {"RUNNER_TEMP": str(runner_alias), "GODEBUG": "fips140=on"}), \
                    patch.object(installer.tempfile, "gettempdir", return_value=str(unrelated_temp)), \
                    patch.object(
                        installer, "run_process",
                        return_value=SimpleNamespace(stdout=b"gh version test (fixture)\n"),
                    ) as run:
                installer.install(output, {"version": "test"}, artifact, archive)
            self.assertEqual(output.resolve(), (physical / "nested" / "gh").resolve())
            self.assertEqual(output.read_bytes(), binary)
            run.assert_called_once()

    def test_system_image_install_allows_only_root_owned_exact_destination(self):
        expected = Path("/usr/local/bin/gh")
        with patch.object(installer.os, "geteuid", return_value=0):
            self.assertEqual(installer._install_destination(expected, allow_system_image=True), expected)
            for unsafe in (Path("/gh"), Path("/usr/local/gh")):
                with self.subTest(path=unsafe), self.assertRaisesRegex(ValueError, "inside the checkout or runner temp"):
                    installer._install_destination(unsafe, allow_system_image=True)
            with self.assertRaisesRegex(ValueError, "must name gh"):
                installer._install_destination(Path("/usr/local/bin/other"), allow_system_image=True)
            with self.assertRaisesRegex(ValueError, "inside the checkout or runner temp"):
                installer._install_destination(Path("/usr/local/bin/gh"))
        with patch.object(installer.os, "geteuid", return_value=1001), \
                self.assertRaisesRegex(ValueError, "inside the checkout or runner temp"):
            installer._install_destination(expected, allow_system_image=True)

    def test_validated_system_image_stage_verifies_without_path_membership(self):
        raw, artifact, binary = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            executable = root / "gh"
            untrusted_check = root / ".gh-untrusted-check"
            untrusted_check.write_bytes(binary)

            def check_private_copy(command, **kwargs):
                self.assertEqual(Path(command[0]).read_bytes(), binary)
                return SimpleNamespace(stdout=b"gh version test (fixture)\n")

            with patch.object(installer, "SYSTEM_IMAGE_EXECUTABLE", executable), \
                    patch.object(installer, "_trusted_path_roots", return_value=[]), \
                    patch.object(installer.os, "geteuid", return_value=0), \
                    patch.dict(os.environ, {"PATH": "/usr/bin", "GODEBUG": "fips140=on"}), \
                    patch.object(installer, "_release_archive", return_value=raw), \
                    patch.object(installer, "run_process", side_effect=check_private_copy) as run:
                installer.install(executable, {"version": "test"}, artifact, allow_system_image=True)
                self.assertEqual(executable.read_bytes(), binary)
                self.assertNotIn(str(root), os.environ["PATH"])
                with self.assertRaisesRegex(ValueError, "trusted executable directory"):
                    installer.verify(untrusted_check, artifact, "test", allow_temporary=True)
            run.assert_called_once()

    def test_verified_bytes_are_the_bytes_executed_if_source_changes_after_hash(self):
        _, artifact, binary = fixture()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "gh"
            source.write_bytes(binary)

            def replace_source_then_report_version(command, **kwargs):
                invoked = Path(command[0])
                self.assertNotEqual(invoked, source)
                self.assertEqual(invoked.read_bytes(), binary)
                source.write_bytes(b"unqualified replacement")
                return SimpleNamespace(stdout=b"gh version test (fixture)\n")

            with patch.dict(os.environ, {"GODEBUG": "fips140=on"}), \
                    patch.object(installer, "run_process", side_effect=replace_source_then_report_version) as run:
                installer.verify(source, artifact, "test")
            run.assert_called_once()
            self.assertEqual(source.read_bytes(), b"unqualified replacement")

    def test_install_creates_missing_directory_inside_allowed_temp_root(self):
        raw, artifact, binary = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "download.tar.gz"
            archive.write_bytes(raw)
            output = root / "new" / "nested" / "gh"
            with patch.object(installer, "verify") as verify:
                installer.install(output, {"version": "test"}, artifact, archive)
            verify.assert_called_once()
            self.assertTrue(output.is_file())
            self.assertEqual(output.read_bytes(), binary)

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
            with patch.object(installer, "run_process") as run:
                with patch.dict(os.environ, {"GODEBUG": "fips140=off"}), self.assertRaisesRegex(ValueError, "GODEBUG"):
                    installer.verify(path, artifact, "test")
                path.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, "qualified native build"):
                    installer.verify(path, artifact, "test")
                run.assert_not_called()

    def test_downloader_requires_exact_toolchain_before_network(self):
        for version in ("go1.27.3", "go1.27.40", "go1.28.0", "devel go1.27.4"):
            with self.subTest(version=version), \
                    patch.object(installer, "resolve_executable", return_value="/tools/go"), \
                    patch.object(installer, "run_process") as run:
                run.return_value = SimpleNamespace(stdout=f"go version {version} linux/amd64\n".encode())
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
            if command[1:] == ["version"]:
                self.assertEqual(kwargs["timeout"], 15)
                return SimpleNamespace(stdout=b"go version go1.27.4 linux/amd64\n")
            self.assertEqual(command[1:4], ["run", str(ROOT / "scripts/download_release_gh.go"),
                                             "https://example.invalid/test"])
            self.assertEqual(kwargs["timeout"], 180)
            self.assertEqual(Path(command[4]), Path(kwargs["cwd"]) / "artifact")
            self.assertEqual(Path(kwargs["cwd"]), Path(command[4]).parent)
            Path(command[4]).write_bytes(b"bounded synthetic archive")
            return SimpleNamespace()

        with patch.dict(os.environ, {"GOTOOLCHAIN": "auto", "GODEBUG": "fips140=off", "GOPROXY": "private"}), \
                patch.object(installer, "resolve_executable", return_value="/tools/go"), \
                patch.object(installer, "run_process", side_effect=run) as process_run:
            self.assertEqual(installer.download("https://example.invalid/test"), b"bounded synthetic archive")
        self.assertEqual(process_run.call_count, 2)
        self.assertFalse(Path(process_run.call_args.args[0][4]).parent.exists())

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
