import copy
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import arc_recovery_payload as m


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stage = self.root / "stage"
        self.data = {
            "first": b"abcdefgh" * 11,
            "second": b"abcdefgh" * 13,
            "empty": b"",
        }
        self.inputs = []
        for name, data in self.data.items():
            p = self.root / name
            p.write_bytes(data)
            self.inputs.append(
                {
                    "name": name,
                    "path": str(p),
                    "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        self.limit = sum(map(len, self.data.values()))

    def prepared(self):
        return m.prepare(self.inputs, self.stage, 13, self.limit)

    def packed(self, catalog):
        out = io.BytesIO()
        m.pack(self.stage, catalog, out, self.limit)
        return out.getvalue()

    def test_unsupported_preparation_fails_before_directory_creation(self):
        with patch.object(m.os, "name", "nt"), \
                self.assertRaisesRegex(ValueError, "requires POSIX"):
            self.prepared()
        self.assertFalse(self.stage.exists())

    def test_repository_cli_roundtrip_with_trusted_external_manifest(self):
        source = self.root / "inputs.json"
        source.write_text(json.dumps(self.inputs))
        helper = Path(m.__file__).resolve()
        def cli(operation, manifest, **kwargs):
            return subprocess.run(
                [sys.executable, str(helper), operation, "--manifest", str(manifest),
                 "--directory", str(self.stage), "--max-bytes", str(self.limit),
                 "--chunk-bytes", "13"], capture_output=True, check=True,
                timeout=10, **kwargs)
        cli("prepare", source)
        manifest = self.stage / "manifest.json"
        packed = cli("pack", manifest).stdout
        checked = cli("verify", manifest, input=packed)
        self.assertTrue(json.loads(checked.stderr)["verified"])
        self.assertEqual(checked.stdout, b"")

    def test_roundtrip_interleaving_and_standard_manual_concatenation(self):
        catalog = self.prepared()
        raw = self.packed(catalog)
        self.assertTrue(m.verify(io.BytesIO(raw), catalog, self.limit)["verified"])
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            self.assertEqual(
                archive.getnames(),
                ["manifest.json"] + [p["name"] for _, p in m.interleaved(catalog)],
            )
            for original in catalog["files"]:
                manual = b"".join(
                    archive.extractfile(p["name"]).read() for p in original["parts"]
                )
                self.assertEqual(manual, self.data[original["name"]])

    def test_zstd_standard_stream_roundtrip(self):
        binary = shutil.which("zstd")
        if not binary:
            self.skipTest("zstd unavailable")
        c = self.prepared()
        raw = self.packed(c)
        compressed = subprocess.run(
            [binary, "-q", "-c"], input=raw, capture_output=True, check=True, timeout=10
        ).stdout
        restored = subprocess.run(
            [binary, "-q", "-d", "-c"],
            input=compressed,
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        self.assertTrue(m.verify(io.BytesIO(restored), c, self.limit)["verified"])

    def test_unknown_or_link_staging_rejected(self):
        c = self.prepared()
        extra = self.stage / "unexpected"
        extra.write_text("x")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            self.packed(c)
        extra.unlink()
        target = self.stage / c["files"][0]["parts"][0]["name"]
        target.unlink()
        target.symlink_to(self.root / "first")
        with self.assertRaisesRegex(ValueError, "Unexpected"):
            self.packed(c)

    def test_corrupt_part_cannot_be_packed(self):
        c = self.prepared()
        p = self.stage / c["files"][0]["parts"][0]["name"]
        p.write_bytes(b"x" * p.stat().st_size)
        with self.assertRaisesRegex(ValueError, "identity differs"):
            self.packed(c)

    def test_wrong_input_identity_leaves_no_manifest(self):
        self.inputs[0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "hash differs"):
            self.prepared()
        self.assertFalse((self.stage / "manifest.json").exists())
        self.assertEqual((self.root / "first").read_bytes(), self.data["first"])

    def test_budget_and_bad_sizes_fail_before_creation(self):
        for size in (True, -1, 10**30):
            entries = copy.deepcopy(self.inputs)
            entries[0]["bytes"] = size
            with self.assertRaises(ValueError):
                m.prepare(entries, self.stage, 13, self.limit)
            self.assertFalse(self.stage.exists())
        with self.assertRaisesRegex(ValueError, "budget"):
            m.prepare(self.inputs, self.stage, 13, self.limit - 1)
        self.assertFalse(self.stage.exists())

    def test_symlink_source_and_existing_destination_preserved(self):
        p = self.root / "first"
        p.unlink()
        p.symlink_to(self.root / "second")
        with self.assertRaisesRegex(ValueError, "regular"):
            self.prepared()
        self.assertTrue(p.is_symlink())
        marker = self.stage / "keep"
        marker.write_text("keep")
        with self.assertRaises(FileExistsError):
            self.prepared()
        self.assertEqual(marker.read_text(), "keep")

    def test_truncated_and_extra_streams_rejected(self):
        c = self.prepared()
        raw = self.packed(c)
        for bad in (raw[:-1], raw + b"x", raw + b"\0" * 10240):
            with self.assertRaises((ValueError, tarfile.TarError)):
                m.verify(io.BytesIO(bad), c, self.limit)

    def test_altered_tar_bytes_rejected(self):
        c = self.prepared()
        raw = bytearray(self.packed(c))
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            offset = archive.getmembers()[1].offset_data
        raw[offset] ^= 1
        with self.assertRaisesRegex(ValueError, "identity"):
            m.verify(io.BytesIO(raw), c, self.limit)

    def test_link_and_unknown_member_rejected(self):
        c = self.prepared()
        raw = self.packed(c)
        for kind in ("link", "unknown"):
            out = io.BytesIO()
            with (
                tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as old,
                tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as new,
            ):
                for index, entry in enumerate(old):
                    data = old.extractfile(entry).read()
                    if index == 1:
                        if kind == "link":
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = "/tmp/forbidden"
                            entry.size = 0
                            data = b""
                        else:
                            entry.name = "unknown"
                    new.addfile(entry, io.BytesIO(data))
            with self.assertRaisesRegex(ValueError, "Unexpected tar member"):
                m.verify(io.BytesIO(out.getvalue()), c, self.limit)

    def test_disk_reserve_refuses_before_part_write(self):
        with patch.object(
            m.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(1, 1, 0)
        ), self.assertRaisesRegex(ValueError, "Disk reserve"):
            self.prepared()
        self.assertFalse((self.stage / "manifest.json").exists())

    def test_manifest_schema_and_part_order_are_strict(self):
        c = self.prepared()
        for field, value in [("schema", True), ("chunk_bytes", False)]:
            bad = copy.deepcopy(c)
            bad[field] = value
            with self.assertRaises(ValueError):
                m.check_catalog(bad, self.limit)
        bad = copy.deepcopy(c)
        bad["files"][0]["parts"][0]["name"] = "../escape"
        with self.assertRaises(ValueError):
            m.check_catalog(bad, self.limit)

    def test_metadata_reserve_checked_before_directory_creation(self):
        with patch.object(m.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(1, 1, m.METADATA_CUSHION - 1)), \
                self.assertRaisesRegex(ValueError, "Disk reserve"):
            self.prepared()
        self.assertFalse(self.stage.exists())

    def test_allocation_rounding_is_charged(self):
        quantum = max(4096, m.os.statvfs(self.root).f_frsize)
        free = m.METADATA_CUSHION + quantum - 1
        with patch.object(m.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(free, 0, free)), \
                self.assertRaisesRegex(ValueError, "Disk reserve"):
            m.reserve_gate(self.root, 0, 1)

    def test_manifest_postwrite_floor_failure_leaves_only_pending(self):
        original = m.reserve_gate
        def gate(path, reserve, pending=0):
            if (self.stage / "manifest.json.pending").exists() and pending == 0:
                raise ValueError("Disk reserve exhausted")
            return original(path, reserve, pending)
        with patch.object(m, "reserve_gate", side_effect=gate), \
                self.assertRaisesRegex(ValueError, "Disk reserve"):
            self.prepared()
        self.assertFalse((self.stage / "manifest.json").exists())
        self.assertTrue((self.stage / "manifest.json.pending").exists())

    def test_manifest_final_floor_failure_revokes_catalog(self):
        original = m.reserve_gate
        def gate(path, reserve, pending=0):
            if (self.stage / "manifest.json").exists():
                raise ValueError("Disk reserve exhausted")
            return original(path, reserve, pending)
        with patch.object(m, "reserve_gate", side_effect=gate), \
                self.assertRaisesRegex(ValueError, "Disk reserve"):
            self.prepared()
        self.assertFalse((self.stage / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
