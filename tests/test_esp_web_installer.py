"""Boundary tests; structural fixtures are not distributable firmware."""

import hashlib
import json
from pathlib import Path
import struct
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import esp_web_installer as installer


def image(boot_offset=0x1000, chip_id=0):
    data = bytearray(b"\xff" * 0x11000)
    data[boot_offset : boot_offset + 4] = b"\xe9\x01\x02\x20"
    struct.pack_into("<H", data, boot_offset + 12, chip_id)
    data[0x8000:0x8020] = struct.pack(
        "<HBBII16sI", 0x50AA, 0, 0, 0x10000, 0x100000, b"app", 0
    )
    data[0x10000:0x10004] = b"\xe9\x01\x02\x20"
    struct.pack_into("<H", data, 0x10000 + 12, chip_id)
    return bytes(data)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.binary = image()
        (self.inputs / "board.factory.bin").write_bytes(self.binary)
        self.doc = {
            "schema": 1,
            "name": "Example sensor",
            "version": "1.2.3-rc.1",
            "channel": "rc",
            "project_url": "https://github.com/example/sensor",
            "profiles": [
                {
                    "id": "devkit",
                    "label": "ESP32 DevKit",
                    "board": "ESP32-WROOM 4 MB",
                    "chip_family": "ESP32",
                    "provisioning": "improv",
                    "parts": [{"path": "board.factory.bin", "offset": 0}],
                }
            ],
        }
        self.output = self.root / "site"

    def generate(self, **kwargs):
        (self.inputs / "web-flasher.json").write_text(json.dumps(self.doc))
        return installer.generate(
            self.inputs, "web-flasher.json", self.output, **kwargs
        )

    def test_factory_padding_and_deterministic_complete_site(self):
        self.generate()
        manifest = json.loads((self.output / "manifests/devkit.json").read_text())
        self.assertTrue(manifest["new_install_prompt_erase"])
        self.assertEqual(manifest["builds"][0]["parts"][0]["offset"], 0)
        self.assertEqual(
            (self.output / "firmware/devkit/board.factory.bin").read_bytes(),
            self.binary,
        )
        for line in (self.output / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ")
            self.assertEqual(
                expected, hashlib.sha256((self.output / name).read_bytes()).hexdigest()
            )
        other = self.root / "site2"
        installer.generate(self.inputs, "web-flasher.json", other)
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertEqual(
                    path.read_bytes(),
                    (other / path.relative_to(self.output)).read_bytes(),
                )

    def test_chip_specific_offsets_and_ids(self):
        # Independent expected layouts cover both padded and non-padded families.
        for chip, offset, chip_id in (
            ("ESP32", 0x1000, 0),
            ("ESP32-S2", 0x1000, 2),
            ("ESP32-S3", 0, 9),
            ("ESP32-C3", 0, 5),
            ("ESP32-C5", 0x2000, 23),
            ("ESP32-P4", 0x2000, 18),
        ):
            with self.subTest(chip=chip):
                data = image(offset, chip_id)
                installer.validate_factory(data, chip, 0x8000)
                for header_offset in (offset, 0x10000):
                    damaged = bytearray(data)
                    struct.pack_into("<H", damaged, header_offset + 12, 255)
                    with self.assertRaisesRegex(ValueError, "chip ID"):
                        installer.validate_factory(damaged, chip, 0x8000)

    @unittest.skipUnless(
        shutil.which("node"), "Browser contract checks require Node.js"
    )
    def test_browser_contract(self):
        result = subprocess.run(
            [
                shutil.which("node"),
                "--test",
                str(Path(__file__).with_name("test_esp_web_installer_ui.mjs")),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_html_escaping(self):
        self.doc["name"] = '<img src=x onerror="bad()">'
        self.generate()
        self.assertNotIn("<img src=x", (self.output / "index.html").read_text())
        self.assertIn("&lt;img", (self.output / "index.html").read_text())
        self.assertNotIn("innerHTML", (self.output / "app.mjs").read_text())

    def test_bad_catalog_rejected_before_output(self):
        mutations = [
            lambda d: d.update(schema=2),
            lambda d: d.update(wifi_password="private"),
            lambda d: d.update(project_url="javascript:alert(1)"),
            lambda d: d.update(project_url="https://127.0.0.1/setup"),
            lambda d: d.update(project_url="https://device.local/setup"),
            lambda d: d.update(project_url="https://user:password@example.com"),
            lambda d: d["profiles"][0].update(chip_family="BK7231"),
            lambda d: d["profiles"].append(dict(d["profiles"][0])),
            lambda d: d["profiles"][0]["parts"][0].update(path="../outside.bin"),
            lambda d: d["profiles"][0]["parts"][0].update(path="/tmp/secret.bin"),
            lambda d: d["profiles"][0]["parts"][0].update(path="secrets/board.bin"),
            lambda d: d["profiles"][0]["parts"][0].update(path="missing.bin"),
            lambda d: d["profiles"][0]["parts"][0].update(sha256="0" * 64),
            lambda d: d["profiles"][0]["parts"][0].update(offset=0x1000),
            lambda d: d["profiles"][0]["parts"].append(
                {"path": "board.factory.bin", "offset": 0}
            ),
        ]
        original = json.dumps(self.doc)
        for change in mutations:
            with self.subTest(change=change):
                self.doc = json.loads(original)
                change(self.doc)
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.generate()
                self.assertFalse(self.output.exists())

    def test_symlinks_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "board.bin").write_bytes(self.binary)
        (self.inputs / "link").symlink_to(outside, target_is_directory=True)
        (self.inputs / "direct.bin").symlink_to(outside / "board.bin")
        for name in ("link/board.bin", "direct.bin"):
            self.doc["profiles"][0]["parts"][0]["path"] = name
            with self.assertRaisesRegex(ValueError, "symlinks"):
                self.generate()

    def test_empty_archive_ota_and_secret_rejected(self):
        for data in (
            b"",
            b"PK\x03\x04" + b"test" * 256,
            b"\xe9\x01\x02\x20" + b"\0" * 512,
            self.binary + b"-----BEGIN PRIVATE KEY-----",
        ):
            with self.subTest(size=len(data)):
                (self.inputs / "board.factory.bin").write_bytes(data)
                with self.assertRaises(ValueError):
                    self.generate()
                self.assertFalse(self.output.exists())

    def test_wrong_release_identity_rejected(self):
        with self.assertRaisesRegex(ValueError, "version differs"):
            self.generate(version="9.9.9")
        with self.assertRaisesRegex(ValueError, "channel differs"):
            self.generate(channel="stable")

    def test_existing_output_preserved(self):
        self.output.mkdir()
        marker = self.output / "user-data"
        marker.write_text("keep")
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.generate()
        self.assertEqual(marker.read_text(), "keep")

    def test_no_partial_output_on_write_failure(self):
        with mock.patch.object(Path, "write_bytes", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.generate()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".esp-installer-*")), [])


if __name__ == "__main__":
    unittest.main()
