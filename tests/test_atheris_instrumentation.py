"""Native Atheris controls; run in the locked fuzz environment during preflight."""
from pathlib import Path
import sys
import unittest

try:
    import atheris
except ImportError:
    atheris = None

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from fuzz_python_preflight import verify_instrumentation  # noqa: E402


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


if __name__ == "__main__":
    if atheris is None:
        raise SystemExit("Native Atheris is required for this explicit control run")
    unittest.main()
