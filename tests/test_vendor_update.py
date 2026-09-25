"""Offline delivery tests: real signed Git objects, fake GitHub, no remote writes."""

# Test names describe invariants; shared real-Git fixtures are cleaned by unittest.
# pylint: disable=missing-function-docstring,too-many-instance-attributes,consider-using-with

import argparse
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "vendor_update", SCRIPTS / "vendor_update.py"
)
vu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(vu)


def fixture_git(directory, *args):
    """Create local test repositories; production intentionally cannot init/reset."""
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=directory,
        capture_output=True,
        check=True,
        env=vu.clean_env(),
    ).stdout


def arguments(**changes):
    """Use a small bundle with the same receipt protocol as the real core."""
    result = argparse.Namespace(
        source_repo="example/core",
        target_repo="example/player",
        source_workflow=".github/workflows/core.yml",
        source_sha="a" * 40,
        source_tree="shared-core",
        source_dir="unused",
        target_dir="unused",
        run_id=17,
        run_attempt=2,
        artifact_id=29,
        artifact_digest="",
        manifest="core.manifest.json",
        manifest_name="example-core",
        artifact_file=["core.js", "core.jar", "core.manifest.json"],
        copy_file=["core.js", "core.manifest.json"],
        destination_prefix="vendor",
        qualifying_job=["portable", "browser-client", "wire-contracts"],
        signing_fingerprint="unused",
        stage_only=False,
    )
    for key, value in changes.items():
        setattr(result, key, value)
    return result


def bundle(args, mutate=None, extra=None):
    """Produce exact ZIP bytes without trusting them in the validator."""
    files = {"core.js": b"compiled js", "core.jar": b"compiled jar"}
    inputs = {"src/Core.kt": vu.digest(b"source\n")}
    manifest = {
        "schema": 1,
        "name": args.manifest_name,
        "version": "0.1.0",
        "source": {
            "files": inputs,
            "sha256": vu.digest(json.dumps(inputs, separators=(",", ":")).encode()),
        },
        "artifacts": {
            name: {"bytes": len(body), "sha256": vu.digest(body)}
            for name, body in files.items()
        },
    }
    files[args.manifest] = vu.json_bytes(manifest)
    if mutate:
        mutate(files, manifest)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, body in files.items():
            zipped.writestr(name, body)
        if extra:
            zipped.writestr(*extra)
    raw = output.getvalue()
    args.artifact_digest = "sha256:" + vu.digest(raw)
    return raw, files, manifest


def run_info(args):
    """An executing producer can deliver only after each qualifier succeeded."""
    return {
        "id": args.run_id,
        "run_attempt": args.run_attempt,
        "head_sha": args.source_sha,
        "repository": {"full_name": args.source_repo},
        "head_repository": {"full_name": args.source_repo},
        "head_branch": "main",
        "path": args.source_workflow,
        "event": "push",
        "workflow_id": 31,
        "status": "in_progress",
        "conclusion": None,
        "run_started_at": "2026-09-25T10:00:00Z",
    }


class FakeGitHub:
    """Only explicitly provisioned endpoints are available to the tested helper."""

    def __init__(self, repository):
        self.repo, self.token = repository, "test-token-never-log"
        self.values = {"": {"full_name": repository, "default_branch": "main"}}
        self.requests, self.pulls, self.writes = [], [], []

    def request(self, path, *, body=None, archive=False):
        self.requests.append((path, body, archive))
        if body is not None:
            self.writes.append((path, deepcopy(body)))
            if path == "pulls":
                branch = body["head"]
                pull = {
                    "html_url": "https://github.com/example/player/pull/9",
                    "state": "open",
                    "user": {"login": "delivery-bot"},
                    "head": {
                        "sha": self.values["git/ref/heads/" + branch]["object"]["sha"]
                    },
                    "base": {"ref": body["base"]},
                    "draft": body["draft"],
                }
                self.pulls.append(pull)
                return deepcopy(pull)
            raise AssertionError("Unexpected API mutation: " + path)
        if path not in self.values:
            raise AssertionError("Unexpected API read: " + path)
        return deepcopy(self.values[path])

    def optional(self, path):
        return deepcopy(self.values.get(path))

    def pages(self, path, field=None):
        if path.startswith("pulls?"):
            return deepcopy(self.pulls)
        return self.request(path)[field]


def qualified_source(args):
    """Populate complete current-attempt qualification metadata."""
    source = FakeGitHub(args.source_repo)
    source.values[f"actions/runs/{args.run_id}"] = run_info(args)
    source.values["actions/workflows/.github%2Fworkflows%2Fcore.yml"] = {
        "path": args.source_workflow,
        "id": 31,
    }
    source.values[f"actions/runs/{args.run_id}/attempts/{args.run_attempt}/jobs"] = {
        "jobs": [
            {
                "name": name,
                "status": "completed",
                "conclusion": "success",
                "run_id": args.run_id,
                "run_attempt": args.run_attempt,
            }
            for name in args.qualifying_job
        ]
    }
    return source


class ArchiveTests(unittest.TestCase):
    """Untrusted compressed bytes and self-reported manifest hashes are rejected."""

    def test_valid_bundle(self):
        args = arguments()
        raw, files, manifest = bundle(args)
        self.assertEqual(vu.validate_bundle(raw, args), (files, manifest))

    def test_tampering_both_archive_and_self_reported_payload(self):
        args = arguments()
        raw, _, _ = bundle(args)
        with self.assertRaisesRegex(vu.ReleaseError, "digest"):
            vu.validate_bundle(raw + b"changed", args)
        raw, _, _ = bundle(
            args, lambda files, _: files.update({"core.js": b"tampered"})
        )
        with self.assertRaisesRegex(vu.ReleaseError, "bytes differ"):
            vu.validate_bundle(raw, args)

    def test_missing_extra_duplicate_and_unsafe_paths(self):
        for extra in [
            ("../escape", b"bad"),
            ("/absolute", b"bad"),
            ("core.js", b"duplicate"),
            ("extra", b"bad"),
        ]:
            with (
                self.subTest(extra=extra),
                self.assertWarns(UserWarning)
                if extra[0] == "core.js"
                else nullcontext(),
            ):
                args = arguments()
                raw, _, _ = bundle(args, extra=extra)
                with self.assertRaisesRegex(vu.ReleaseError, "inventory"):
                    vu.validate_bundle(raw, args)
        args = arguments()
        raw, _, _ = bundle(args, lambda files, _: files.pop("core.jar"))
        with self.assertRaisesRegex(vu.ReleaseError, "inventory"):
            vu.validate_bundle(raw, args)

    def test_symlink_and_limits(self):
        args = arguments()
        _, files, _ = bundle(args)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            for name, body in files.items():
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipped.writestr(info, body)
        args.artifact_digest = "sha256:" + vu.digest(output.getvalue())
        unsafe_archive = output.getvalue()
        with self.assertRaisesRegex(vu.ReleaseError, "Unsafe archive"):
            vu.validate_bundle(unsafe_archive, args)
        raw, _, _ = bundle(args)
        with (
            patch.object(vu, "MAX_TOTAL", 1),
            self.assertRaisesRegex(vu.ReleaseError, "expansion"),
        ):
            vu.validate_bundle(raw, args)

    def test_schema_receipt_and_artifact_set(self):
        mutations = [
            lambda value: value.update(schema=True),
            lambda value: value.update(name="wrong"),
            lambda value: value["source"].update(sha256="0" * 64),
            lambda value: value["artifacts"].pop("core.jar"),
            lambda value: value["artifacts"]["core.js"].update(bytes=True),
        ]
        for mutation in mutations:
            args = arguments()

            def change(files, manifest, mutation=mutation, filename=args.manifest):
                mutation(manifest)
                files[filename] = vu.json_bytes(manifest)

            raw, _, _ = bundle(args, change)
            with self.subTest(mutation=mutation), self.assertRaises(vu.ReleaseError):
                vu.validate_bundle(raw, args)

    def test_duplicate_json_fields(self):
        args = arguments()
        raw, _, _ = bundle(
            args,
            lambda files, _: files.update({args.manifest: b'{"schema":1,"schema":1}'}),
        )
        with self.assertRaisesRegex(vu.ReleaseError, "Duplicate JSON"):
            vu.validate_bundle(raw, args)


class QualificationTests(unittest.TestCase):
    """A successful uploading step is insufficient publication authority."""

    def test_executing_and_successful_runs(self):
        args = arguments()
        source = qualified_source(args)
        self.assertEqual(vu.qualify(source, args)["status"], "in_progress")
        source.values["actions/runs/17"].update(
            status="completed", conclusion="success"
        )
        self.assertEqual(vu.qualify(source, args)["conclusion"], "success")

    def test_wrong_run_identity_and_untrusted_event(self):
        for key, value in [
            ("id", 18),
            ("run_attempt", 1),
            ("head_sha", "b" * 40),
            ("head_branch", "feature"),
            ("path", ".github/workflows/other.yml"),
            ("event", "pull_request"),
            ("event", "workflow_run"),
            ("head_repository", {"full_name": "foreign/core"}),
            ("status", "queued"),
            ("conclusion", "failure"),
        ]:
            args = arguments()
            source = qualified_source(args)
            source.values["actions/runs/17"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(vu.ReleaseError):
                vu.qualify(source, args)

    def test_missing_skipped_failed_duplicate_or_old_attempt_job(self):
        for mutation in [
            lambda jobs: jobs.pop(),
            lambda jobs: jobs.append(deepcopy(jobs[0])),
            lambda jobs: jobs[0].update(conclusion="skipped"),
            lambda jobs: jobs[0].update(conclusion="failure"),
            lambda jobs: jobs[0].update(run_attempt=1),
        ]:
            args = arguments()
            source = qualified_source(args)
            mutation(source.values["actions/runs/17/attempts/2/jobs"]["jobs"])
            with self.subTest(mutation=mutation), self.assertRaises(vu.ReleaseError):
                vu.qualify(source, args)

    def test_artifact_identity_expiry_and_attempt(self):
        args = arguments(artifact_digest="sha256:" + "a" * 64)
        meta = {
            "id": 29,
            "expired": False,
            "digest": args.artifact_digest,
            "size_in_bytes": 123,
            "workflow_run": {"id": 17, "head_sha": args.source_sha},
            "created_at": "2026-09-25T10:01:00Z",
        }
        vu.validate_artifact(meta, run_info(args), args)
        for key, value in [
            ("id", 30),
            ("expired", True),
            ("digest", "sha256:" + "b" * 64),
            ("workflow_run", {"id": 17, "head_sha": "b" * 40}),
            ("size_in_bytes", True),
            ("size_in_bytes", vu.MAX_ARCHIVE + 1),
            ("created_at", "2026-09-25T09:59:59Z"),
        ]:
            bad = meta | {key: value}
            run = run_info(args)
            with self.subTest(key=key), self.assertRaises(vu.ReleaseError):
                vu.validate_artifact(bad, run, args)


class TransportTests(unittest.TestCase):
    """Exercise CLI/API boundaries hidden by in-memory GitHub response fixtures."""

    def test_repository_endpoint_and_separate_token_environment(self):
        environment = {
            "SOURCE_TOKEN": "source-secret",
            "TARGET_TOKEN": "target-secret",
            "GITHUB_TOKEN": "ambient-secret",
            "GH_TOKEN": "ambient-secret",
            "GH_DEBUG": "api",
        }
        result = subprocess.CompletedProcess([], 0, b'{"default_branch":"main"}', b"")
        with (
            patch.dict(os.environ, environment),
            patch.object(vu.subprocess, "run", return_value=result) as command,
        ):
            self.assertEqual(
                vu.GitHub("example/core", "source-secret").request("")[
                    "default_branch"
                ],
                "main",
            )
        self.assertEqual(command.call_args.args[0][-1], "repos/example/core")
        actual = command.call_args.kwargs["env"]
        self.assertEqual(actual["GH_TOKEN"], "source-secret")
        self.assertFalse(set(environment) - {"GH_TOKEN"} & set(actual))
        self.assertNotIn("source-secret", " ".join(command.call_args.args[0]))

    def test_cli_isolated_from_current_directory_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "UNTRUSTED_IMPORT"
            (Path(temporary) / "release_control.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
                "raise RuntimeError('untrusted')\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "-I", str(SCRIPTS / "vendor_update.py"), "--help"],
                cwd=temporary,
                env=os.environ | {"PYTHONPATH": temporary},
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            self.assertIn(b"--signing-fingerprint", completed.stdout)
            self.assertFalse(marker.exists())

    def test_actions_zip_keeps_normal_rest_accept_header(self):
        result = subprocess.CompletedProcess([], 0, b"ZIP bytes", b"")
        with patch.object(vu.subprocess, "run", return_value=result) as command:
            body = vu.GitHub("example/core", "source-secret").request(
                "actions/artifacts/29/zip", archive=True
            )
        self.assertEqual(body, b"ZIP bytes")
        self.assertEqual(
            command.call_args.args[0][-1], "repos/example/core/actions/artifacts/29/zip"
        )
        self.assertNotIn("Accept: application/octet-stream", command.call_args.args[0])

    def test_unsafe_paths(self):
        for path in [
            "../vendor",
            "/vendor",
            "vendor/../x",
            ".Git/config",
            "vendor\\escape",
            "vendor/:evil",
        ]:
            with self.subTest(path=path), self.assertRaises(vu.ReleaseError):
                vu.safe_path(path)

    def test_cli_normalizes_relative_checkout_paths(self):
        args = arguments(
            source_dir="producer",
            target_dir="consumer",
            artifact_digest="sha256:" + "a" * 64,
        )
        argv = []
        for name in [
            "source_repo",
            "source_workflow",
            "source_sha",
            "source_dir",
            "target_repo",
            "target_dir",
            "manifest",
            "manifest_name",
            "destination_prefix",
            "artifact_digest",
            "signing_fingerprint",
            "run_id",
            "run_attempt",
            "artifact_id",
        ]:
            argv.extend(["--" + name.replace("_", "-"), str(getattr(args, name))])
        for name in ["artifact_file", "copy_file"]:
            for value in getattr(args, name):
                argv.extend(["--" + name.replace("_", "-"), value])
        parsed = vu.parse_args(argv)
        self.assertEqual(parsed.source_dir, Path.cwd() / "producer")
        self.assertEqual(parsed.target_dir, Path.cwd() / "consumer")

    def test_api_rejects_arbitrary_routes_methods_and_non_draft_mutations(self):
        github = vu.GitHub("example/core", "source-secret")
        for path in [
            "--hostname=evil.test",
            "../user",
            "https://evil.test",
            "git/refs",
            "actions/artifacts/29/zip?x=1",
        ]:
            with (
                self.subTest(path=path),
                patch.object(vu.subprocess, "run") as command,
                self.assertRaises(vu.ReleaseError),
            ):
                github.request(path)
            command.assert_not_called()
        draft = {
            "title": "Vendor update",
            "body": "Details",
            "head": "codex/vendor-" + "a" * 64,
            "base": "main",
            "draft": True,
        }
        for path, body in [
            ("releases", draft),
            ("pulls", draft | {"draft": False}),
            ("pulls", draft | {"base": "other"}),
            ("pulls", draft | {"head": "user-branch"}),
        ]:
            with (
                self.subTest(path=path, body=body),
                patch.object(vu.subprocess, "run") as command,
                self.assertRaises(vu.ReleaseError),
            ):
                github.request(path, body=body)
            command.assert_not_called()

    def test_git_rejects_extra_flags_remote_helpers_and_cwd_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            for args in [
                ("-c", "alias.x=!malicious", "x"),
                ("push", "--force", "origin", "HEAD"),
                ("fetch", "--no-tags", "ext::malicious", "a" * 40),
                (
                    "fetch",
                    "--upload-pack=malicious",
                    "https://github.com/example/core.git",
                    "a" * 40,
                ),
                ("config", "core.hooksPath", "/untrusted"),
            ]:
                with (
                    self.subTest(args=args),
                    patch.object(vu.subprocess, "run") as command,
                    self.assertRaises(vu.ReleaseError),
                ):
                    vu.git(temporary, *args)
                command.assert_not_called()
            with (
                patch.object(vu.subprocess, "run") as command,
                self.assertRaises(vu.ReleaseError),
            ):
                vu.git("--exec-path=/untrusted", "rev-parse", "HEAD")
            command.assert_not_called()
            result = subprocess.CompletedProcess([], 0, b"a" * 40, b"")
            with patch.object(vu.subprocess, "run", return_value=result) as command:
                vu.git(temporary, "rev-parse", "HEAD")
            self.assertNotIn(temporary, command.call_args.args[0])
            self.assertEqual(command.call_args.kwargs["cwd"], Path(temporary))


class GitDeliveryTests(unittest.TestCase):
    """Real SSH signatures and index isolation; network calls are never executed."""

    @classmethod
    def setUpClass(cls):
        cls.keys = tempfile.TemporaryDirectory()
        cls.key = Path(cls.keys.name) / "signer"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(cls.key)],
            check=True,
            capture_output=True,
        )
        cls.fingerprint = subprocess.check_output(
            ["ssh-keygen", "-lf", str(cls.key) + ".pub"], text=True
        ).split()[1]
        cls.allowed = Path(cls.keys.name) / "allowed"
        cls.allowed.write_text(
            "bot@example.test "
            + Path(str(cls.key) + ".pub").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls):
        cls.keys.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = arguments(
            source_dir=self.root / "source",
            target_dir=self.root / "target",
            signing_fingerprint=self.fingerprint,
        )
        self._repository(
            self.args.source_dir,
            self.args.source_repo,
            {"shared-core/src/Core.kt": b"source\n"},
        )
        self._repository(
            self.args.target_dir,
            self.args.target_repo,
            {
                "vendor/core.js": b"old",
                "vendor/core.manifest.json": b"old",
                "app.txt": b"unchanged",
            },
        )
        self.args.source_sha = vu.git_text(self.args.source_dir, "rev-parse", "HEAD")
        self.base = vu.git_text(self.args.target_dir, "rev-parse", "HEAD")
        self.source = qualified_source(self.args)
        subtree = vu.git_text(
            self.args.source_dir, "rev-parse", self.args.source_sha + ":shared-core"
        )
        self.source.values.update(
            {
                "git/ref/heads/main": {
                    "object": {"type": "commit", "sha": self.args.source_sha}
                },
                "git/commits/" + self.args.source_sha: {"tree": {"sha": "c" * 40}},
                "git/trees/" + "c" * 40: {
                    "truncated": False,
                    "tree": [{"path": "shared-core", "type": "tree", "sha": subtree}],
                },
            }
        )
        self.target = FakeGitHub(self.args.target_repo)
        self.target.values.update(
            {
                "git/ref/heads/main": {"object": {"sha": self.base}},
                "user": {"login": "delivery-bot"},
            }
        )
        _, self.files, self.manifest = bundle(self.args)
        self.manifest_hash = vu.digest(self.files[self.args.manifest])

    @staticmethod
    def _repository(directory, repository, files):
        directory.mkdir()
        fixture_git(directory, "init", "-b", "main")
        fixture_git(directory, "config", "user.name", "Delivery Bot")
        fixture_git(directory, "config", "user.email", "bot@example.test")
        fixture_git(directory, "config", "gpg.format", "ssh")
        fixture_git(directory, "config", "user.signingkey", str(GitDeliveryTests.key))
        fixture_git(
            directory,
            "config",
            "gpg.ssh.allowedSignersFile",
            str(GitDeliveryTests.allowed),
        )
        fixture_git(
            directory, "remote", "add", "origin", f"https://github.com/{repository}.git"
        )
        for name, body in files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        fixture_git(directory, "add", ".")
        fixture_git(directory, "commit", "-m", "initial", "--no-gpg-sign")

    def remote_git(self, original):
        """Handle fetch/push in memory; all other operations are real local Git."""

        def invoke(directory, *args, **kwargs):
            if args[0] == "fetch":
                return b""
            if args[0] == "push":
                self.assertNotIn("--force", args)
                revision, ref = args[-1].split(":", 1)
                self.target.values["git/ref/" + ref.removeprefix("refs/")] = {
                    "object": {"sha": revision}
                }
                self.target.values["commits/" + revision] = {
                    "sha": revision,
                    "commit": {"verification": {"verified": True}},
                }
                return b""
            return original(directory, *args, **kwargs)

        return invoke

    def test_source_receipt_and_stale_tree(self):
        vu.verify_source(self.source, self.args, self.manifest)
        self.source.values["git/trees/" + "c" * 40]["tree"][0]["sha"] = "d" * 40
        with self.assertRaisesRegex(vu.ReleaseError, "stale"):
            vu.verify_source(self.source, self.args, self.manifest)

    def test_self_consistent_receipt_cannot_forge_source(self):
        self.manifest["source"]["files"]["src/Core.kt"] = vu.digest(b"forged")
        with self.assertRaisesRegex(vu.ReleaseError, "source checkout"):
            vu.verify_source(self.source, self.args, self.manifest)

    def test_stage_real_signed_commit_leaves_checkout_and_index_unchanged(self):
        self.args.stage_only = True
        result = vu.deliver(
            self.source, self.target, self.args, self.files, self.manifest
        )
        self.assertEqual(result["status"], "staged")
        vu.signature(self.args.target_dir, result["commit"], self.fingerprint)
        self.assertEqual(
            vu.git_text(self.args.target_dir, "rev-parse", "HEAD"), self.base
        )
        self.assertEqual(
            vu.git_text(
                self.args.target_dir, "status", "--porcelain", "--untracked-files=all"
            ),
            "",
        )
        self.assertEqual(
            vu.git(self.args.target_dir, "show", result["commit"] + ":app.txt"),
            b"unchanged",
        )
        self.assertEqual(self.target.writes, [])

    def test_delivery_retry_reuses_pr_and_merged_files_noop(self):
        with patch.object(vu, "git", side_effect=self.remote_git(vu.git)):
            first = vu.deliver(
                self.source, self.target, self.args, self.files, self.manifest
            )
            second = vu.deliver(
                self.source, self.target, self.args, self.files, self.manifest
            )
        self.assertEqual(first, second)
        self.assertEqual(len(self.target.writes), 1)
        self.assertTrue(self.target.writes[0][1]["draft"])
        fixture_git(self.args.target_dir, "reset", "--hard", first["commit"])
        self.target.values["git/ref/heads/main"]["object"]["sha"] = first["commit"]
        self.assertEqual(
            vu.deliver(self.source, self.target, self.args, self.files, self.manifest)[
                "status"
            ],
            "noop",
        )

    def test_foreign_signed_branch_rejected_even_with_our_marker(self):
        revision = vu.prepare_commit(
            self.args, self.base, self.files, self.manifest_hash
        )
        branch = "codex/vendor-" + self.manifest_hash
        # A second commit would be foreign work and must never be overwritten.
        original_tree = (
            fixture_git(self.args.target_dir, "rev-parse", self.base + "^{tree}")
            .decode()
            .strip()
        )
        foreign = vu.git_text(
            self.args.target_dir,
            "commit-tree",
            original_tree,
            "-p",
            revision,
            "-S",
            data=f"foreign\n\nVendor-Update: v1\nVendor-Manifest: {self.manifest_hash}\n".encode(),
        )
        self.target.values["git/ref/heads/" + branch] = {"object": {"sha": foreign}}
        with (
            patch.object(vu, "git", side_effect=self.remote_git(vu.git)),
            self.assertRaises(vu.ReleaseError),
        ):
            vu.deliver(self.source, self.target, self.args, self.files, self.manifest)
        self.assertEqual(self.target.writes, [])

    def test_wrong_signer_and_github_unverified(self):
        self.args.signing_fingerprint = "SHA256:foreign"
        with self.assertRaisesRegex(vu.ReleaseError, "signing identity"):
            vu.prepare_commit(self.args, self.base, self.files, self.manifest_hash)
        self.args.signing_fingerprint = self.fingerprint
        original = self.target.request

        def request(path, **kwargs):
            value = original(path, **kwargs)
            if path.startswith("commits/"):
                value["commit"]["verification"]["verified"] = False
            return value

        with (
            patch.object(vu, "git", side_effect=self.remote_git(vu.git)),
            patch.object(self.target, "request", side_effect=request),
            self.assertRaisesRegex(vu.ReleaseError, "GitHub did not verify"),
        ):
            vu.deliver(self.source, self.target, self.args, self.files, self.manifest)
        self.assertEqual(self.target.writes, [])

    def test_qualification_rechecked_before_push(self):
        self.source.values["actions/runs/17"]["run_attempt"] += 1
        with (
            self.assertRaisesRegex(vu.ReleaseError, "identity"),
            patch.object(vu, "git", wraps=vu.git) as commands,
        ):
            vu.deliver(self.source, self.target, self.args, self.files, self.manifest)
        self.assertFalse(
            any(call.args[1] == "push" for call in commands.call_args_list)
        )

    def test_signed_foreign_path_and_pr_owner_are_rejected(self):
        files = self.files | {"app.txt": b"foreign change"}
        with patch.object(
            vu,
            "target_paths",
            return_value=vu.target_paths(self.args) | {"app.txt": "app.txt"},
        ):
            revision = vu.prepare_commit(
                self.args, self.base, files, self.manifest_hash
            )
        with self.assertRaisesRegex(vu.ReleaseError, "foreign paths"):
            vu.existing_commit(self.args, revision, self.files, self.manifest_hash)
        with patch.object(vu, "git", side_effect=self.remote_git(vu.git)):
            vu.deliver(self.source, self.target, self.args, self.files, self.manifest)
            self.target.pulls[0]["user"]["login"] = "other-author"
            with self.assertRaisesRegex(vu.ReleaseError, "foreign or closed"):
                vu.deliver(
                    self.source, self.target, self.args, self.files, self.manifest
                )
        self.assertEqual(len(self.target.writes), 1)

    def test_target_advance_and_source_advance_prevent_push(self):
        for source_advanced in [False, True]:
            with self.subTest(source_advanced=source_advanced):
                self.setUp()
                if source_advanced:
                    self.source.values["git/trees/" + "c" * 40]["tree"][0]["sha"] = (
                        "e" * 40
                    )
                else:
                    self.target.values["git/ref/heads/main"]["object"]["sha"] = "f" * 40
                with (
                    self.assertRaises(vu.ReleaseError),
                    patch.object(vu, "git", wraps=vu.git) as commands,
                ):
                    vu.deliver(
                        self.source, self.target, self.args, self.files, self.manifest
                    )
                self.assertFalse(
                    any(call.args[1] == "push" for call in commands.call_args_list)
                )
                self.assertEqual(self.target.writes, [])


if __name__ == "__main__":
    unittest.main()
