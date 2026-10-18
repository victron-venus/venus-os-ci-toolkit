"""Local validation-only tests: no Docker, Bazel, network, or runtime execution."""

import importlib.util
import io
import json
from pathlib import Path
import shutil
import struct
import tarfile
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build_gvisor_vfs_candidate.py"
SPEC = importlib.util.spec_from_file_location("gvisor_candidate", SCRIPT)
candidate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(candidate)


def elf(interpreter=False):
    data = bytearray(128)
    data[:7] = b"\x7fELF\x02\x01\x01"
    struct.pack_into("<HH", data, 16, 2, 62)
    struct.pack_into("<Q", data, 32, 64)
    struct.pack_into("<HH", data, 54, 56, 1)
    struct.pack_into("<I", data, 64, 3 if interpreter else 1)
    return bytes(data)


class CandidateValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def archive(self, alteration=None):
        path = self.path / "bundle.tar.bz2"
        with tarfile.open(path, "w:bz2") as output:
            for name in sorted(candidate.MEMBERS):
                member = tarfile.TarInfo(name)
                member.mode = 0o755
                data = elf()
                if alteration:
                    data = alteration(member, data)
                member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        return path

    def test_valid_complete_bundle(self):
        result = candidate.validate_bundle(self.archive())
        self.assertEqual(set(result), candidate.MEMBERS)
        self.assertTrue(all(item["type"] == 2 for item in result.values()))

    def test_rejects_interpreter(self):
        with self.assertRaises(candidate.BuildError):
            candidate.elf_metadata(elf(interpreter=True))

    def test_rejects_invalid_architecture(self):
        data = bytearray(elf())
        struct.pack_into("<H", data, 18, 183)
        with self.assertRaises(candidate.BuildError):
            candidate.elf_metadata(data)

    def test_rejects_dynamic_library(self):
        data = bytearray(144)
        data[:128] = elf()
        struct.pack_into("<I", data, 64, 2)
        struct.pack_into("<Q", data, 72, 128)
        struct.pack_into("<Q", data, 96, 16)
        struct.pack_into("<qQ", data, 128, 1, 0)
        with self.assertRaises(candidate.BuildError):
            candidate.elf_metadata(data)

    def test_rejects_traversal(self):
        def alter(member, data):
            member.name = "../" + member.name
            return data
        with self.assertRaises(candidate.BuildError):
            candidate.validate_bundle(self.archive(alter))

    def test_rejects_links(self):
        def alter(member, data):
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            return b""
        with self.assertRaises(candidate.BuildError):
            candidate.validate_bundle(self.archive(alter))

    def test_rejects_non_executable_member(self):
        def alter(member, data):
            member.mode = 0o644
            return data
        with self.assertRaises(candidate.BuildError):
            candidate.validate_bundle(self.archive(alter))

    def test_missing_and_duplicate_bundle_members_fail(self):
        path = self.path / "bad.tar.bz2"
        for names in (["runsc"], ["runsc", "runsc"]):
            with tarfile.open(path, "w:bz2") as archive:
                for name in names:
                    member = tarfile.TarInfo(name)
                    member.mode, member.size = 0o755, len(elf())
                    archive.addfile(member, io.BytesIO(elf()))
            with self.assertRaises(candidate.BuildError):
                candidate.validate_bundle(path)

    def test_test_xml_requires_executed_non_skipped_success(self):
        path = self.path / "test.xml"
        path.write_text('<testsuite tests="2" failures="0" errors="0"/>')
        self.assertEqual(candidate.validate_test_xml(path), 2)
        for xml in ('<testsuites></testsuites>', '<testsuite tests="0"/>', '<testsuite tests="2" skipped="1"/>',
                    '<testsuite tests="2"><testcase><failure/></testcase></testsuite>',
                    '<testsuite tests="2"><testcase><skipped/></testcase></testsuite>'):
            path.write_text(xml)
            with self.assertRaises(candidate.BuildError):
                candidate.validate_test_xml(path)

    def test_rules_go_named_passing_cases_are_accepted(self):
        path = self.path / "test.xml"
        path.write_text('<testsuites><testsuite name="vfs.TestMount" tests="2" '
                        'failures="0" errors="0" skipped="0">'
                        '<testcase name="TestMount"/>'
                        '<testcase name="TestMount/lookup"/>'
                        '</testsuite></testsuites>')
        self.assertEqual(candidate.validate_test_xml(path), 2)

    def event(self):
        env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
               "GITHUB_EVENT_NAME": "workflow_dispatch", "RUNNER_ARCH": "X64",
               "GITHUB_REPOSITORY": "example/public", "GITHUB_REF": "refs/heads/main",
               "GITHUB_SHA": "a" * 40, "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
        return env, {"repository": {"private": False, "full_name": "example/public", "default_branch": "main"}}

    def test_admission_rejects_other_event_and_runner_context(self):
        env, event = self.event()
        candidate.validate_event(env, event)
        for field, bad in (("GITHUB_EVENT_NAME", "pull_request_target"),
                           ("RUNNER_ENVIRONMENT", "self-hosted"),
                           ("GITHUB_REF", "refs/heads/other"), ("RUNNER_ARCH", "ARM64"),
                           ("GITHUB_RUN_ID", "../bad"), ("GITHUB_SHA", "main")):
            with self.subTest(field=field), self.assertRaises(candidate.BuildError):
                candidate.validate_event(dict(env, **{field: bad}), event)
        event["repository"]["private"] = True
        with self.assertRaises(candidate.BuildError):
            candidate.validate_event(env, event)

    def test_clean_environment_does_not_inherit_secrets(self):
        with mock.patch.dict("os.environ", {"SECRET": "not-public", "GITHUB_TOKEN": "not-public"}):
            clean = candidate.clean_env(self.path)
        self.assertNotIn("SECRET", clean)
        self.assertNotIn("GITHUB_TOKEN", clean)
        self.assertEqual(clean["GIT_CONFIG_GLOBAL"], "/dev/null")

    def test_analysis_metadata_override_is_limited_to_two_mnemonics(self):
        flags = candidate.build_flags()
        modifiers = [arg for arg in flags if arg.startswith("--modify_execution_info=")]
        expected = "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox"
        self.assertEqual(modifiers, [expected])
        expression, change = modifiers[0].removeprefix("--modify_execution_info=").rsplit("=", 1)
        self.assertEqual(change, "-no-sandbox")
        for mnemonic in ("GoStandardLibraryAnalysis", "GoStaticAnalysis"):
            self.assertIsNotNone(candidate.re.fullmatch(expression, mnemonic))
        for mnemonic in ("Genrule", "GoCompilePkg", "GoLink", "TestRunner", "RunNogo",
                         "GoStaticAnalysisExtra", "OtherGoStaticAnalysis", "GoStandardLibraryAnalysisExtra"):
            self.assertIsNone(candidate.re.fullmatch(expression, mnemonic))
        self.assertEqual([arg for arg in flags if arg.startswith("--spawn_strategy=")],
                         ["--spawn_strategy=processwrapper-sandbox"])
        self.assertEqual([arg for arg in flags if arg.startswith("--strategy=")],
                         ["--strategy=Genrule=processwrapper-sandbox"])
        self.assertFalse(any("legacy_local_fallback" in arg for arg in flags))

    def test_owned_tmpdir_reaches_bazel_before_any_command(self):
        source, build = self.path / "source", self.path / "build"
        source.mkdir()
        build.mkdir()
        public = SCRIPT.parent.parent
        shutil.copyfile(public / "patches/gvisor/LICENSE", source / "LICENSE")
        paths = {"/workspace": source, "/build": build,
                 "/control/vfs-final-symlink.patch": public / "patches/gvisor/vfs-final-symlink.patch"}
        answers = iter(["f" * 40, candidate.VERSION, "bazel 8.8.1", None])
        seen = []

        def command(argv, **_kwargs):
            self.assertTrue((build / "tmp").is_dir())
            self.assertEqual((build / "tmp").stat().st_mode & 0o777, 0o700)
            seen.append(argv)
            if len(seen) == 5:
                raise candidate.BuildError("stop before mocked tests")
            return next(answers)

        supervisor = mock.Mock()
        supervisor.run.side_effect = command
        with (
            mock.patch.dict(candidate.os.environ, {"BUILD_IN_CONTAINER": "1",
                            "TMPDIR": "/untrusted", "GOTMPDIR": "/untrusted",
                            "GITHUB_TOKEN": "not-public"}, clear=True),
            mock.patch.object(candidate.os, "getuid", return_value=1001),
            mock.patch.object(candidate, "Path", side_effect=lambda value: paths.get(str(value), Path(value))),
            mock.patch.object(candidate, "Supervisor", return_value=supervisor) as factory,
            mock.patch.object(candidate.subprocess, "run") as run,
            mock.patch.object(candidate.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(candidate.BuildError, "stop before mocked tests"):
                candidate.inside()
        env = factory.call_args.args[0]
        self.assertEqual(env["TMPDIR"], str(build / "tmp"))
        self.assertEqual(env["GOMAXPROCS"], "2")
        self.assertNotIn("GOTMPDIR", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertEqual(seen[-2], candidate.bazel("build", *candidate.build_flags(), *candidate.BUILD_TARGETS))
        self.assertIn("--repo_env=TMPDIR=/build/tmp", seen[-2])
        self.assertFalse(any(arg.startswith(("--action_env=", "--test_env=")) for arg in seen[-2]))
        self.assertEqual(seen[-1], candidate.bazel(
            "test", *candidate.build_flags(), "--nocache_test_results", "--test_output=errors",
            "--test_env=GO_TEST_WRAP_TESTV=1", "--local_test_jobs=1", "--test_timeout=300",
            *candidate.TEST_TARGETS))
        self.assertEqual([arg for arg in seen[-1] if arg.startswith("--test_env=")],
                         ["--test_env=GO_TEST_WRAP_TESTV=1"])
        run.assert_not_called()
        popen.assert_not_called()

    def test_owned_tmpdir_rejects_existing_directory_and_symlink(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink):
                build = self.path / ("symlink-case" if symlink else "directory-case")
                build.mkdir()
                temporary = build / "tmp"
                if symlink:
                    temporary.symlink_to(self.path, target_is_directory=True)
                else:
                    temporary.mkdir()
                with (
                    mock.patch.dict(candidate.os.environ, {"BUILD_IN_CONTAINER": "1"}, clear=True),
                    mock.patch.object(candidate.os, "getuid", return_value=1001),
                    mock.patch.object(candidate, "Path", side_effect=lambda value: build if str(value) == "/build" else Path(value)),
                    mock.patch.object(candidate, "Supervisor") as factory,
                ):
                    with self.assertRaises(FileExistsError):
                        candidate.inside()
                factory.assert_not_called()

    def test_fixed_container_restrictions(self):
        args = candidate.container_args("owned", "nonce", "/owned/source", "/owned/build",
                                        "/owned/script", "/owned/patch", 1001, 1001)
        for forbidden in ("--privileged", "--cap-add", "--pid", "--device", "seccomp=unconfined"):
            self.assertNotIn(forbidden, args)
        self.assertNotIn("docker.sock", " ".join(args))
        self.assertIn("no-new-privileges:true", args)
        self.assertEqual(args[args.index("--memory") + 1], "12000000000")
        self.assertEqual(args[args.index("--memory-swap") + 1], "12000000000")
        self.assertEqual(args[args.index("--user") + 1], "1001:1001")
        self.assertEqual(args[args.index("--tmpfs") + 1],
                         "/tmp:rw,nosuid,nodev,size=1073741824,mode=1777")
        self.assertEqual(args[args.index("--cpus") + 1], "3")
        self.assertEqual(args[args.index("--pids-limit") + 1], "1024")
        self.assertIn("--read-only", args)
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        self.assertIn(candidate.IMAGE, args)
        self.assertIn("type=bind,source=/owned/patch,target=/control/vfs-final-symlink.patch,readonly", args)
        with self.assertRaises(candidate.BuildError):
            candidate.container_args("owned", "nonce", "/source", "/build", "/script", "/patch", 0, 0)

    def test_cleanup_rejects_foreign_container_before_removal(self):
        info = [{"Id": "a" * 64, "Name": "/owned",
                 "Config": {"Labels": {candidate.LABEL: "foreign"}}}]
        response = mock.Mock(returncode=0, stdout=json.dumps(info))
        with mock.patch.object(candidate.subprocess, "run", return_value=response) as run:
            with self.assertRaises(candidate.BuildError):
                candidate.cleanup_container({}, "owned", "nonce")
        self.assertEqual(run.call_count, 1)

    def test_cleanup_uses_only_verified_id_and_checks_absence(self):
        cid = "a" * 64
        info = [{"Id": cid, "Name": "/owned", "Config": {"Labels": {candidate.LABEL: "nonce"}}}]
        replies = [mock.Mock(returncode=0, stdout=json.dumps(info)), mock.Mock(returncode=0),
                   mock.Mock(returncode=0, stdout="")]
        with mock.patch.object(candidate.subprocess, "run", side_effect=replies) as run:
            candidate.cleanup_container({}, "owned", "nonce")
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "--force", cid])
        self.assertEqual(run.call_count, 3)

    def test_disk_and_deadline_fail_closed(self):
        supervisor = candidate.Supervisor({}, [self.path])
        with mock.patch.object(candidate.shutil, "disk_usage", return_value=mock.Mock(free=1)):
            with self.assertRaises(candidate.BuildError):
                supervisor.check()
        supervisor.deadline = 0
        with self.assertRaises(candidate.BuildError):
            supervisor.check()

    def test_deadline_during_command_terminates_owned_process_group(self):
        supervisor = candidate.Supervisor({}, [self.path])
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        with mock.patch.object(supervisor, "check", side_effect=[None, candidate.BuildError("deadline")]), \
             mock.patch.object(candidate.subprocess, "Popen", return_value=process), \
             mock.patch.object(candidate.os, "killpg") as kill:
            with self.assertRaises(candidate.BuildError):
                supervisor.run(["unused-mocked-command"])
        kill.assert_called_once_with(12345, candidate.signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=5)

    def test_deadline_cleanup_escalates_only_the_owned_process_group(self):
        supervisor = candidate.Supervisor({}, [self.path])
        process = mock.Mock(pid=12345)
        process.poll.return_value = None
        process.wait.side_effect = [candidate.subprocess.TimeoutExpired("mock", 5), 0]
        with mock.patch.object(supervisor, "check", side_effect=[None, candidate.BuildError("deadline")]), \
             mock.patch.object(candidate.subprocess, "Popen", return_value=process), \
             mock.patch.object(candidate.os, "killpg") as kill:
            with self.assertRaises(candidate.BuildError):
                supervisor.run(["unused-mocked-command"])
        self.assertEqual(kill.call_args_list, [mock.call(12345, candidate.signal.SIGTERM),
                                              mock.call(12345, candidate.signal.SIGKILL)])

    def test_generated_stamp_requires_exact_single_version(self):
        path = self.path / "stable-status.txt"
        path.write_text("BUILD_EMBED_LABEL \nSTABLE_VERSION " + candidate.VERSION + "\n")
        result = candidate.validate_stamp(path, candidate.VERSION)
        self.assertEqual(result["stable_version"], candidate.VERSION)
        self.assertEqual(result["file_sha256"], candidate.digest(path))
        bad = ["", "STABLE_VERSION release-20261012.0\n", "STABLE_VERSION\n",
               "STABLE_VERSION " + candidate.VERSION + "-dirty\n",
               ("STABLE_VERSION " + candidate.VERSION + "\n") * 2]
        for content in bad:
            with self.subTest(content=content):
                path.write_text(content)
                with self.assertRaises(candidate.BuildError):
                    candidate.validate_stamp(path, candidate.VERSION)

    def test_generated_stamp_missing_file_fails(self):
        with self.assertRaises(candidate.BuildError):
            candidate.validate_stamp(self.path / "absent", candidate.VERSION)

    def test_bundle_checks_marker_in_runsc_without_execution(self):
        def mark_runsc(member, data):
            return data + candidate.VERSION.encode() if member.name == "runsc" else data
        path = self.archive(mark_runsc)
        result = candidate.validate_bundle(path, expected_version=candidate.VERSION)
        marker = result["runsc"]["candidate_version_marker"]
        self.assertEqual(marker["byte_occurrences"], 1)
        self.assertEqual(marker["expected"], candidate.VERSION)
        with self.assertRaises(candidate.BuildError):
            candidate.validate_bundle(path, expected_version="vfs-candidate-wrong")

    def test_missing_or_other_member_marker_does_not_qualify_runsc(self):
        for alteration in [None, lambda member, data: data + candidate.VERSION.encode()
                           if member.name != "runsc" else data]:
            with self.subTest(alteration=alteration), self.assertRaises(candidate.BuildError):
                candidate.validate_bundle(self.archive(alteration), expected_version=candidate.VERSION)

    def provenance(self):
        public = SCRIPT.parent.parent
        for filename in ("LICENSE", "vfs-final-symlink.patch"):
            shutil.copyfile(public / "patches/gvisor" / filename, self.path / filename)
        (self.path / "MODIFICATIONS.txt").write_text(candidate.MODIFICATION_NOTICE)

    def test_exact_license_patch_and_added_modification_notice(self):
        self.provenance()
        result = candidate.validate_provenance_files(self.path)
        self.assertEqual(result["LICENSE"], candidate.LICENSE_SHA)
        self.assertEqual(result["vfs-final-symlink.patch"], candidate.PATCH_SHA)
        notice = (self.path / "MODIFICATIONS.txt").read_text()
        self.assertIn(candidate.BASE, notice)
        self.assertIn(candidate.PATCH_SHA, notice)
        self.assertIn("not an upstream NOTICE file", notice)

    def test_provenance_rejects_missing_changed_or_symlinked_files(self):
        for name in ("LICENSE", "vfs-final-symlink.patch", "MODIFICATIONS.txt"):
            for mode in ("missing", "changed", "symlink"):
                with self.subTest(name=name, mode=mode):
                    self.provenance()
                    path = self.path / name
                    if mode == "missing":
                        path.unlink()
                    elif mode == "changed":
                        path.write_bytes(path.read_bytes() + b"changed")
                    else:
                        target = self.path / "target"
                        shutil.copyfile(path, target)
                        path.unlink()
                        path.symlink_to(target)
                    with self.assertRaises(candidate.BuildError):
                        candidate.validate_provenance_files(self.path)
                    if path.is_symlink():
                        path.unlink()

    def test_exact_scope_and_patch(self):
        self.assertEqual(candidate.TEST_TARGETS, ["//pkg/sentry/vfs:vfs_test",
                         "//runsc/prewarmer:prewarmer_single_page_test"])
        self.assertEqual(len(candidate.BUILD_TARGETS), 3)
        self.assertIn("//test/syscalls/linux:open_test", candidate.BUILD_TARGETS)
        patch = SCRIPT.parent.parent / "patches/gvisor/vfs-final-symlink.patch"
        self.assertEqual(candidate.digest(patch), candidate.PATCH_SHA)
        self.assertNotIn("local", candidate.build_flags())


if __name__ == "__main__":
    unittest.main()
