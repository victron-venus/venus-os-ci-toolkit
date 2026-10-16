"""The downstream signer builder rejects unsafe inputs before execution."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import build_release_signer as builder


class BuildSignerTests(unittest.TestCase):
    def test_environment_requires_verification_and_limits_workers(self):
        with patch.dict(os.environ, {"GONOSUMDB": "*", "GOPROXY": "http://example.invalid", "GODEBUG": "fips140=off"}):
            env = builder.build_environment()
        self.assertEqual(env["GOSUMDB"], "sum.golang.org")
        self.assertEqual(env["GONOSUMDB"], "")
        self.assertEqual(env["GOVCS"], "*:off")
        self.assertEqual(env["GOTOOLCHAIN"], "local")
        self.assertEqual(env["GODEBUG"], "fips140=on")
        self.assertEqual(env["GOMAXPROCS"], "2")
        self.assertEqual(env["GOFLAGS"], "-p=2")

    def test_bad_source_and_toolchain_never_execute_build(self):
        for responses, reason in [(["wrong"], "source commit"),
                                  ([builder.SOURCE, " M main.go"], "must be clean"),
                                  ([builder.SOURCE, "", "go1.26.4"], "Go 1.27.2")]:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                (root / "src").mkdir()
                with patch.object(builder, "run", side_effect=responses), \
                     patch.object(builder.subprocess, "run") as execute, \
                     self.assertRaisesRegex(ValueError, reason):
                    builder.build(root / "src", root / "out/cosign")
                execute.assert_not_called()

    def test_failed_build_restores_original_module_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / "source"
            source.mkdir()
            for name in ("go.mod", "go.sum"):
                (source / name).write_text("original " + name)
            with patch.object(builder, "run", side_effect=[builder.SOURCE, "", builder.GO_VERSION]), \
                 patch.object(builder.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "go")), \
                 self.assertRaises(subprocess.CalledProcessError):
                builder.build(source, root / "out/cosign")
            for name in ("go.mod", "go.sum"):
                self.assertEqual((source / name).read_text(), "original " + name)

    def test_output_inside_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaisesRegex(ValueError, "outside"):
            builder.build(Path(d), Path(d) / "cosign")


if __name__ == "__main__":
    unittest.main()
