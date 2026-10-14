"""Source candidates preserve the declared inputs and reject malformed archives."""

import io
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from scripts import toolkit_source, version_plan


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
