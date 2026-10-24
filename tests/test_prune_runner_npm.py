"""Pruning keeps action runtimes and refuses unreviewed package layouts."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "prune_runner_npm", Path(__file__).resolve().parents[1] / "scripts/prune_runner_npm.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RunnerNpmTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for runtime, version in MODULE.VERSIONS.items():
            base = self.root / "externals" / runtime
            (base / "bin").mkdir(parents=True)
            (base / "bin/node").write_bytes(b"action-engine-" + runtime.encode())
            (base / "bin/corepack").write_bytes(b"retain")
            npm = base / "lib/node_modules/npm"
            npm.mkdir(parents=True)
            (npm / "package.json").write_text(json.dumps({"name": "npm", "version": version}))
            for cli in ("npm", "npx"):
                (base / "bin" / cli).symlink_to(f"../lib/node_modules/npm/bin/{cli}-cli.js")

    def test_keeps_engines_and_other_tools(self):
        MODULE.prune(self.root)
        for runtime in MODULE.VERSIONS:
            base = self.root / "externals" / runtime
            self.assertEqual((base / "bin/node").read_bytes(), b"action-engine-" + runtime.encode())
            self.assertEqual((base / "bin/corepack").read_bytes(), b"retain")
            self.assertFalse((base / "lib/node_modules/npm").exists())
            self.assertFalse((base / "bin/npm").is_symlink())
            self.assertFalse((base / "bin/npx").is_symlink())

    def test_version_drift_fails_before_any_removal(self):
        npm = self.root / "externals/node24/lib/node_modules/npm/package.json"
        npm.write_text('{"name":"npm","version":"99.0.0"}')
        with self.assertRaisesRegex(ValueError, "version"):
            MODULE.prune(self.root)
        self.assertTrue((self.root / "externals/node20/bin/npm").is_symlink())

    def test_unexpected_launcher_fails(self):
        link = self.root / "externals/node24/bin/npm"
        link.unlink()
        link.symlink_to("/usr/local/bin/npm")
        with self.assertRaisesRegex(ValueError, "launcher"):
            MODULE.prune(self.root)

    def test_linked_runtime_fails(self):
        runtime = self.root / "externals/node20"
        moved = self.root / "other"
        runtime.rename(moved)
        runtime.symlink_to(moved)
        with self.assertRaisesRegex(ValueError, "directory"):
            MODULE.prune(self.root)


if __name__ == "__main__":
    unittest.main()
