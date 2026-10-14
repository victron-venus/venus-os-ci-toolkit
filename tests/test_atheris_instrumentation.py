"""Native Atheris controls; run in the locked fuzz environment during preflight."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

try:
    import atheris
except ImportError:
    atheris = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from fuzz_python_preflight import verify_instrumentation  # noqa: E402
import release_fuzz as fuzz  # noqa: E402


def instrumented(value):
    return value == b"valid"


def uninstrumented(value):
    return value == b"missing"


@unittest.skipIf(atheris is None, "Run in the hash-locked fuzz environment for native Atheris")
class NativeInstrumentationControls(unittest.TestCase):
    def test_native_instrumentation_is_accepted(self):
        atheris.instrument_func(instrumented)
        proof = verify_instrumentation([{"module": __name__, "functions": ["instrumented"]}], ROOT, atheris)
        self.assertEqual(proof[0]["function"], "instrumented")

    def test_native_missing_instrumentation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Missing production instrumentation"):
            verify_instrumentation([{"module": __name__, "functions": ["uninstrumented"]}], ROOT, atheris)

    def test_native_setup_without_fuzz_does_not_pass_seed_replay(self):
        for calls_fuzz in (False, True):
            with self.subTest(calls_fuzz=calls_fuzz), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root / ".github").mkdir()
                (root / "seeds").mkdir()
                (root / "seeds/valid").write_bytes(b"valid")
                (root / "owned.py").write_text("def parse(data):\n    return data == b'valid'\n")
                harness = root / "harness.py"
                harness.write_text(
                    "import sys, atheris\nwith atheris.instrument_imports():\n    import owned\n"
                    "def test_input(data):\n    owned.parse(data)\n"
                    "atheris.Setup(sys.argv, test_input)\n" + ("atheris.Fuzz()\n" if calls_fuzz else ""))
                target = {"name": "parse", "engine": "atheris", "seconds": 1, "corpus": "seeds",
                          "command": [sys.executable, str(harness)],
                          "instrumentation": [{"module": "owned", "functions": ["parse"]}]}
                (root / fuzz.MANIFEST).write_text(json.dumps({"targets": [target]}))
                output = root / "output"
                output.mkdir()
                def native_execute(command, cwd, log, *_args):
                    # This control already runs inside the isolated preflight.
                    with log.open("w") as stream:
                        result = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT,
                                                timeout=20, check=False)
                    return result.returncode, 0.0
                with patch.object(fuzz, "bounded_preflight", side_effect=native_execute):
                    deadline = time.monotonic() + 30
                    if calls_fuzz:
                        proof = fuzz.replay_python_target(root, output, target, deadline)
                        self.assertGreater(proof["executions"], 0)
                        self.assertEqual(proof, json.loads((output / "parse/instrumentation.json").read_text()))
                    else:
                        with self.assertRaisesRegex(ValueError, "positive engine execution"):
                            fuzz.replay_python_target(root, output, target, deadline)
                        self.assertTrue((output / "parse/instrumentation.json").is_file())


if __name__ == "__main__":
    if atheris is None:
        raise SystemExit("Native Atheris is required for this explicit control run")
    unittest.main()
