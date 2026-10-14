"""Replay committed parser seeds; hosted RC jobs run the actual Atheris engine."""

import contextlib
import runpy
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class ToolkitFuzzSeedTests(unittest.TestCase):
    def test_seed_replay_uses_the_owned_parsers(self):
        shim = types.SimpleNamespace(instrument_imports=contextlib.nullcontext)
        for target, corpus, module in (
            ("fuzz_oci_metadata", "oci-metadata", "oci"),
            ("fuzz_version_policy", "version-policy", "version_plan"),
        ):
            with self.subTest(target=target), patch.dict(sys.modules, atheris=shim):
                loaded = runpy.run_path(str(ROOT / "fuzz" / f"{target}.py"))
                self.assertEqual(Path(loaded[module].__file__).resolve().parent, ROOT / "scripts")
                for seed in sorted((ROOT / "fuzz/corpus" / corpus).iterdir()):
                    loaded["test_one_input"](seed.read_bytes())
                for data in (b"", b"\0", b"\xff" * 4096):
                    loaded["test_one_input"](data)


if __name__ == "__main__":
    unittest.main()
