"""The npm dependency patch is restricted to verified upstream package bytes."""
import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "install_runner_npm", Path(__file__).resolve().parents[1] / "scripts/install_runner_npm.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RunnerNpmTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.npm_files = {
            "package.json": b'{"name":"npm","version":"11.21.1"}',
            "bin/npm-cli.js": b"unchanged cli",
            "node_modules/undici/package.json": b'{"name":"undici","version":"6.29.0"}',
            "node_modules/undici/old.js": b"old implementation",
            "node_modules/node-gyp/package.json": b'{"dependencies":{"undici":"^6.25.0"}}',
            "node_modules/ip-address/package.json": b'{"name":"ip-address","version":"10.7.2"}',
            "node_modules/socks/package.json": b'{"dependencies":{"ip-address":"^10.1.1"}}',
        }
        self.undici_files = {
            "package.json": b'{"name":"undici","version":"6.29.1"}',
            "index.js": b"upstream fixed implementation",
        }
        self.ip_files = {"package.json": b'{"name":"ip-address","version":"10.7.4"}'}
        self.pins = {}
        self.write_archive("npm", self.npm_files, "11.21.1")
        self.write_archive("undici", self.undici_files, "6.29.1")
        self.write_archive("ip-address", self.ip_files, "10.7.4")
        final = {name: (value, 0o644) for name, value in self.npm_files.items()
                 if not name.startswith(("node_modules/undici/", "node_modules/ip-address/"))}
        final.update({"node_modules/undici/" + name: (value, 0o644)
                      for name, value in self.undici_files.items()})
        final.update({"node_modules/ip-address/" + name: (value, 0o644)
                      for name, value in self.ip_files.items()})
        self.pins["bundle_tree_sha256"] = hashlib.sha256(json.dumps(
            MODULE.inventory(final), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def write_archive(self, name, files, version, extras=()):
        path = self.root / (name + ".tgz")
        with tarfile.open(path, "w:gz") as bundle:
            for filename, data in list(files.items()) + list(extras):
                member = tarfile.TarInfo("package/" + filename)
                member.size = len(data)
                member.mode = 0o644
                bundle.addfile(member, io.BytesIO(data))
        data = path.read_bytes()
        self.pins[name] = {"name": name, "version": version,
                           "sha256": hashlib.sha256(data).hexdigest(),
                           "integrity": "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()}
        return path

    def archives(self):
        return {name: self.root / (name + ".tgz") for name in MODULE.PATCHES}

    def assemble(self):
        return MODULE.assemble(self.root / "npm.tgz", self.archives(), self.pins)

    def test_only_selected_dependencies_are_changed_and_inventory_locks_every_file(self):
        files, lock = self.assemble()
        self.assertEqual(files["bin/npm-cli.js"][0], b"unchanged cli")
        self.assertNotIn("node_modules/undici/old.js", files)
        self.assertEqual(files["node_modules/undici/index.js"][0], b"upstream fixed implementation")
        self.assertTrue(all(name.startswith(("node_modules/undici/", "node_modules/ip-address/")) for name in lock["changed_files"]))
        self.assertEqual(lock["installed_files"], MODULE.inventory(files))
        output = self.root / "installed"
        MODULE.install(self.root / "npm.tgz", self.archives(), output, self.pins)
        self.assertEqual(json.loads((output / "runner-bundle-lock.json").read_text()), lock)

    def test_hash_and_integrity_mismatch_fail(self):
        for key in ("sha256", "integrity"):
            with self.subTest(key=key):
                saved = self.pins["npm"][key]
                self.pins["npm"][key] = "wrong"
                with self.assertRaisesRegex(ValueError, "mismatch"):
                    self.assemble()
                self.pins["npm"][key] = saved

    def test_duplicate_and_traversal_are_rejected(self):
        for name in ("package.json", "../outside", "/absolute", "x//y"):
            with self.subTest(name=name):
                self.write_archive("npm", self.npm_files, "11.21.1", [(name, b"unexpected")])
                with self.assertRaises(ValueError):
                    self.assemble()

    def test_symbolic_archive_member_is_rejected(self):
        path = self.root / "npm.tgz"
        with tarfile.open(path, "w:gz") as bundle:
            member = tarfile.TarInfo("package/package.json")
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
            bundle.addfile(member)
        data = path.read_bytes()
        self.pins["npm"]["sha256"] = hashlib.sha256(data).hexdigest()
        self.pins["npm"]["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
        with self.assertRaisesRegex(ValueError, "regular"):
            self.assemble()

    def test_changed_consumer_constraint_is_not_silently_overridden(self):
        self.npm_files["node_modules/node-gyp/package.json"] = b'{"dependencies":{"undici":"^7"}}'
        self.write_archive("npm", self.npm_files, "11.21.1")
        with self.assertRaisesRegex(ValueError, "compatibility"):
            self.assemble()

    def test_ip_address_constraint_and_missing_replacement_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "replacement set"):
            MODULE.assemble(self.root / "npm.tgz", {"undici": self.root / "undici.tgz"}, self.pins)
        self.npm_files["node_modules/socks/package.json"] = b'{"dependencies":{"ip-address":"^11"}}'
        self.write_archive("npm", self.npm_files, "11.21.1")
        with self.assertRaisesRegex(ValueError, "compatibility"):
            self.assemble()

    def test_full_inventory_drift_is_rejected(self):
        self.npm_files["bin/npm-cli.js"] = b"unreviewed cli"
        self.write_archive("npm", self.npm_files, "11.21.1")
        with self.assertRaisesRegex(ValueError, "inventory"):
            self.assemble()

    def test_existing_file_and_symlink_are_preserved(self):
        output = self.root / "installed"
        for linked in (False, True):
            with self.subTest(linked=linked):
                if linked:
                    output.symlink_to(self.root / "missing")
                else:
                    output.write_text("keep")
                with self.assertRaisesRegex(ValueError, "exists"):
                    MODULE.install(self.root / "npm.tgz", self.archives(), output, self.pins)
                self.assertTrue(output.is_symlink() if linked else output.read_text() == "keep")
                output.unlink()


if __name__ == "__main__":
    unittest.main()
