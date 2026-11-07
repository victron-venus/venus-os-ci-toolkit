"""Source candidates preserve the declared inputs and reject malformed archives."""

import base64
import hashlib
import io
import json
import shutil
import sys
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import release_control, toolkit_source, version_plan


class ToolkitReleaseNotesTests(unittest.TestCase):
    def test_checkout_inventory_includes_runtime_harnesses_and_acceptance_tests(self):
        root = Path(__file__).resolve().parents[1]
        declared = set(toolkit_source.inventory(root))
        required = {
            str(path.relative_to(root))
            for directory in ("scripts", "tests", "fuzz", "tools")
            for path in (root / directory).rglob("*.py")
        }
        required.update({
            ".github/requirements-generator.txt",
            ".github/oci-tools/accepted-builder.json",
            "docs/ACCEPTED_BUILDER_CONSUMER.md",
            "deploy/arc-private/CAPACITY_PREFLIGHT.md",
            "deploy/arc-private/capacity_preflight.py",
            "tools/secret_control/requirements-github-lock.txt",
        })
        required.update(
            str(path.relative_to(root))
            for path in (root / "docs").glob("RUNNER_ADVISORY_REVIEW_*.json")
        )
        self.assertFalse(required - declared, f"Unshipped source or acceptance input: {sorted(required - declared)}")

    def test_source_archive_ships_admission_runtime_tests_and_operator_guide(self):
        root = Path(__file__).resolve().parents[1]
        declared = toolkit_source.inventory(root)
        required = {
            "scripts/release_push_scope.py",
            "tests/test_release_push_admission_generator.py",
            "tests/test_release_push_prepare.py",
            "tests/test_release_push_scope.py",
            "docs/automatic-release-test-paths.md",
        }
        self.assertFalse(required - set(declared), "Admission inputs missing from source archive")
        with tempfile.TemporaryDirectory() as temporary:
            work = Path(temporary)
            checkout = work / "checkout"
            for name in declared:
                target = checkout / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / name, target)
            # Model a source checkout even when this test runs inside a versioned candidate.
            base = (checkout / "SOURCE_VERSION").read_text().strip().split("-", 1)[0]
            (checkout / "SOURCE_VERSION").write_text(base + "\n")
            subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
            subprocess.run(["git", "add", "-f", "--", "."], cwd=checkout, check=True)
            archive = toolkit_source.build(checkout, work / "package", base, "beta")
            extracted = toolkit_source.extract_verified(checkout, archive, work / "extracted")
            for name in required:
                self.assertEqual((extracted / name).read_bytes(), (root / name).read_bytes())
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c",
                 "import sys; sys.path[:0] = ['scripts', 'tests']; "
                 "import install_release, release_push_scope; "
                 "import test_release_push_admission_generator, test_release_push_prepare, test_release_push_scope; "
                 "from pathlib import Path; "
                 "assert Path(install_release.__file__).resolve().is_relative_to(Path.cwd()); "
                 "assert 'automatic_release_test_paths' in Path('docs/automatic-release-test-paths.md').read_text()"],
                cwd=extracted, capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_checkout_changelog_supports_beta_and_rc(self):
        root = Path(__file__).resolve().parents[1]
        policy = json.loads((root / ".release-policy.json").read_text())
        base = (root / "SOURCE_VERSION").read_text().strip().split("-", 1)[0]
        raw = (root / "CHANGELOG.md").read_bytes()
        source = "a" * 40
        response = {
            "type": "file", "path": "CHANGELOG.md", "encoding": "base64",
            "content": base64.b64encode(raw).decode(), "size": len(raw),
            "sha": hashlib.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw,
                usedforsecurity=False,
            ).hexdigest(),
        }
        for channel in ("beta", "rc"):
            with self.subTest(channel=channel):
                github = Mock(spec=release_control.GitHub)
                github.api.return_value = response
                with patch.object(release_control, "source_policy_snapshot", return_value={"data": policy}):
                    notes = release_control.release_notes(github, f"v{base}-{channel}.1", source, "Source-bound build evidence.")
                github.api.assert_called_once_with(f"contents/CHANGELOG.md?ref={source}")
                self.assertIn(f"## Changes in {base}", notes)
                self.assertIn("### Upgrade", notes)
                self.assertIn("### Security", notes)
                self.assertIn("Source-bound build evidence.", notes)


class ToolkitSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "SOURCE_VERSION").write_text("0.1.1\n")
        (self.root / "tool.py").write_text("print('portable toolkit')\n")
        (self.root / "tool.py").chmod(0o755)
        (self.root / "source-files.json").write_text(json.dumps(["SOURCE_VERSION", "source-files.json", "tool.py"]))
        self.policy = {"versioning": {"schema": 1, "promotion": "final-build", "files": [
            {"path": "SOURCE_VERSION", "format": "text", "value": "package"}
        ], "artifacts": [{"path": "*.tar.gz", "format": "tar-text", "member": "venus-os-ci-toolkit/SOURCE_VERSION", "value": "package"}]}}
        (self.root / ".release-policy.json").write_text(json.dumps(self.policy))
        self.git("init", "-q")
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def test_acceptance_installs_shipped_secret_test_dependencies_before_discovery(self):
        source = self.root / "extracted"
        with (
            patch.object(toolkit_source, "extract_verified", return_value=source),
            patch.object(toolkit_source.subprocess, "run") as run,
        ):
            toolkit_source.acceptance(self.root, self.root / "source.tar.gz")
        commands = [call.args[0] for call in run.call_args_list]
        install_index = next(i for i, command in enumerate(commands) if "pip" in command)
        discover_index = next(i for i, command in enumerate(commands) if "discover" in command)
        install = commands[install_index]
        requirements = {install[i + 1] for i, value in enumerate(install) if value == "-r"}
        self.assertEqual(requirements, {
            str(source / ".github/requirements-generator.txt"),
            str(source / "tools/secret_control/requirements-github-lock.txt"),
        })
        self.assertIn("--require-hashes", install)
        self.assertIn("--only-binary=:all:", install)
        self.assertLess(install_index, discover_index)
        self.assertEqual(run.call_args_list[discover_index].kwargs["cwd"], source)
        self.assertTrue(all(call.kwargs["check"] for call in run.call_args_list))

    def test_beta_and_rc_versions_are_frozen_and_reproducible(self):
        for channel in ("beta", "rc"):
            with self.subTest(channel=channel):
                (self.root / "SOURCE_VERSION").write_text("0.1.1\n")
                plan = version_plan.create_plan("0.1.1", channel, 2, self.git("rev-parse", "HEAD"), self.policy)
                (self.root / ".release-plan.json").write_text(json.dumps(plan))
                version_plan.sync_versions(self.root, self.policy, plan)
                first = toolkit_source.build(self.root, self.root / f"{channel}-a", "0.1.1", channel)
                second = toolkit_source.build(self.root, self.root / f"{channel}-b", "0.1.1", channel)
                self.assertEqual(first.read_bytes(), second.read_bytes())
                result = toolkit_source.extract_verified(self.root, first, self.root / f"{channel}-out")
                self.assertEqual((result / "SOURCE_VERSION").read_text().strip(), f"0.1.1-{channel}.2")
                self.assertTrue((result / "tool.py").stat().st_mode & 0o111)
                with self.assertRaises(ValueError):
                    toolkit_source.build(self.root, self.root / "wrong", "0.1.1", "nightly")

    def test_rejects_untracked_and_symlinked_inputs(self):
        (self.root / "untracked").write_text("unreviewed")
        (self.root / "source-files.json").write_text(json.dumps(["SOURCE_VERSION", "untracked"]))
        with self.assertRaises(ValueError):
            toolkit_source.build(self.root, self.root / "out", "0.1.1", "beta")
        (self.root / "link").symlink_to(self.root / "tool.py")
        (self.root / "source-files.json").write_text(json.dumps(["link"]))
        with self.assertRaises(ValueError):
            toolkit_source.inventory(self.root)

    def test_rejects_archive_membership_type_mode_and_byte_changes(self):
        original = toolkit_source.build(self.root, self.root / "original", "0.1.1", "beta")
        for mutation in ("duplicate", "missing", "extra", "symlink", "mode", "content", "traversal"):
            with self.subTest(mutation=mutation):
                target = self.root / f"{mutation}.tar.gz"
                with tarfile.open(original, "r:gz") as source, tarfile.open(target, "w:gz") as output:
                    entries = source.getmembers()
                    for index, entry in enumerate(entries):
                        raw = source.extractfile(entry).read()
                        if index == 0:
                            if mutation == "missing":
                                continue
                            if mutation == "duplicate":
                                output.addfile(entry, io.BytesIO(raw))
                            if mutation == "symlink":
                                entry.type, entry.linkname, entry.size = tarfile.SYMTYPE, "/tmp/escape", 0
                            if mutation == "mode":
                                entry.mode = 0o777
                            if mutation == "content":
                                raw = b"x" * len(raw)
                            if mutation == "traversal":
                                entry.name = "../outside"
                        output.addfile(entry, io.BytesIO(raw))
                    if mutation == "extra":
                        output.addfile(tarfile.TarInfo("extra"), io.BytesIO())
                with self.assertRaises(ValueError):
                    toolkit_source.extract_verified(self.root, target, self.root / mutation)


if __name__ == "__main__":
    unittest.main()
