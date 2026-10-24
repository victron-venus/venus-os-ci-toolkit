"""Offline archive-boundary tests; never execute a daemon or download an asset."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runner_containerd", ROOT / "scripts/install_runner_containerd.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(directory, architecture="amd64", extra=None):
    binary = bytearray(64)
    binary[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", binary, 18, {"amd64": 62, "arm64": 183}[architecture])
    archive = directory / "fixture.tgz"
    with tarfile.open(archive, "w:gz") as bundle:
        top = tarfile.TarInfo("docker")
        top.type = tarfile.DIRTYPE
        bundle.addfile(top)
        for name in sorted(module.MEMBERS):
            content = bytes(binary) if name == "docker/containerd" else b"unused"
            member = tarfile.TarInfo(name)
            member.size = len(content)
            bundle.addfile(member, io.BytesIO(content))
        if extra is not None:
            bundle.addfile(extra)
    pins = {"artifacts": {architecture: {
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "binary_sha256": hashlib.sha256(binary).hexdigest(), "binary_bytes": len(binary),
    }}}
    return archive, pins, bytes(binary)


class RunnerContainerdTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_both_architectures_install_exact_bytes_without_execution(self):
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture):
                archive, pins, binary = fixture(self.directory, architecture)
                output = self.directory / architecture
                module.install(archive, output, pins, architecture)
                self.assertEqual(output.read_bytes(), binary)
                self.assertEqual(output.stat().st_mode & 0o777, 0o755)

    def test_archive_or_binary_digest_mismatch_writes_nothing(self):
        archive, pins, _ = fixture(self.directory)
        for key in ("archive_sha256", "binary_sha256"):
            bad = copy.deepcopy(pins)
            bad["artifacts"]["amd64"][key] = "0" * 64
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                module.install(archive, self.directory / "output", bad, "amd64")
            self.assertFalse((self.directory / "output").exists())

    def test_cross_architecture_binary_rejected_even_with_matching_hash(self):
        archive, pins, _ = fixture(self.directory)
        pins["artifacts"]["arm64"] = pins["artifacts"].pop("amd64")
        with self.assertRaisesRegex(ValueError, "ELF architecture"):
            module.extract(archive, pins, "arm64")

    def test_extra_traversal_and_duplicate_entries_rejected(self):
        for name in ("../../escape", "docker/containerd"):
            with self.subTest(name=name):
                archive, pins, _ = fixture(self.directory, extra=tarfile.TarInfo(name))
                with self.assertRaisesRegex(ValueError, "inventory|Duplicate"):
                    module.extract(archive, pins, "amd64")
        self.assertFalse((self.directory.parent / "escape").exists())

    def test_link_archive_entries_rejected(self):
        archive, pins, _ = fixture(self.directory)
        # Repack the exact inventory but replace an unused member with a symlink.
        replacement = self.directory / "link.tgz"
        with tarfile.open(archive) as original, tarfile.open(replacement, "w:gz") as output:
            for member in original:
                if member.name == "docker/runc":
                    member.type = tarfile.SYMTYPE
                    member.linkname = "/outside"
                    member.size = 0
                output.addfile(member, original.extractfile(member) if member.isfile() else None)
        pins["artifacts"]["amd64"]["archive_sha256"] = hashlib.sha256(replacement.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "links"):
            module.extract(replacement, pins, "amd64")

    def test_existing_file_and_dangling_symlink_preserved(self):
        archive, pins, _ = fixture(self.directory)
        for symlink in (False, True):
            output = self.directory / ("link" if symlink else "existing")
            if symlink:
                output.symlink_to(self.directory / "absent")
            else:
                output.write_bytes(b"original")
            with self.assertRaisesRegex(ValueError, "already exists"):
                module.install(archive, output, pins, "amd64")
            self.assertTrue(output.is_symlink() if symlink else output.read_bytes() == b"original")

    def test_publication_race_does_not_overwrite_and_cleans_temporary(self):
        archive, pins, _ = fixture(self.directory)
        output = self.directory / "raced"
        original_link = os.link

        def race(source, destination):
            Path(destination).write_bytes(b"other writer")
            original_link(source, destination)

        with patch.object(module.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                module.install(archive, output, pins, "amd64")
        self.assertEqual(output.read_bytes(), b"other writer")
        self.assertEqual(list(self.directory.glob(".containerd-*")), [])

    def test_unsupported_architecture_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            module.extract(self.directory / "missing", {}, "armv7")

    def test_manifest_urls_match_dockerfile_and_pin_real_artifact_boundaries(self):
        pins = json.loads((ROOT / ".github/containers/runner-containerd.json").read_text())
        source = (ROOT / "deploy/runner-reserve/Dockerfile").read_text()
        self.assertIn("docker-" + pins["docker_version"] + ".tgz", source)
        self.assertEqual(set(pins["artifacts"]), {"amd64", "arm64"})
        for architecture, platform in (("amd64", "x86_64"), ("arm64", "aarch64")):
            entry = pins["artifacts"][architecture]
            self.assertEqual(entry["url"], "https://download.docker.com/linux/static/stable/" + platform
                             + "/docker-" + pins["docker_version"] + ".tgz")
            for key in ("archive_sha256", "binary_sha256"):
                self.assertRegex(entry[key], r"^[a-f0-9]{64}$")
            self.assertGreater(entry["binary_bytes"], 1000000)


if __name__ == "__main__":
    unittest.main()
