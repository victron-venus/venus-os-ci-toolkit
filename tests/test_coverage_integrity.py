"""Exercise the pinned uploader's integrity boundary without external traffic."""

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/codecov-v7.1.1/codecov.sh"
ACTION = "codecov/codecov-action@303a32d7a59b442fa8d48b6a1cc6825c09c847a5"
WRAPPER_SHA256 = "1603474143632611c1913295378b412ed9a8deceabcd8bbd4ccd71ce7ac77790"


class CoverageIntegrityTests(unittest.TestCase):
    """Keep optional publication separate from verification and test failures."""

    def setUp(self):
        self.workflow = yaml.safe_load(
            (ROOT / ".github/workflows/python-ci.yml").read_text()
        )
        self.job = self.workflow["jobs"]["ci"]
        self.upload = next(
            step for step in self.job["steps"] if step["name"] == "Upload coverage"
        )

    def test_only_publication_is_optional_and_fixture_matches_pinned_action(self):
        self.assertEqual(self.upload["uses"], ACTION)
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), WRAPPER_SHA256)
        self.assertIs(self.upload["with"]["fail_ci_if_error"], True)
        self.assertIs(self.upload["continue-on-error"], True)
        self.assertEqual(
            self.upload["if"],
            "${{ inputs.run-tests && inputs.coverage-artifact-name == '' }}",
        )
        self.assertEqual(self.upload["with"]["files"], "./coverage.xml")
        self.assertFalse(self.job.get("continue-on-error", False))
        for step in self.job["steps"]:
            if step is not self.upload:
                self.assertFalse(step.get("continue-on-error", False), step["name"])
        test = next(step for step in self.job["steps"] if step["name"] == "Run Tests")
        self.assertIn("--cov-fail-under=${{ inputs.coverage-threshold }}", test["run"])

    def run_wrapper(self, failure):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            bin_directory = work / "bin"
            bin_directory.mkdir()
            stubs = {
                "curl": r'''#!/bin/sh
output=
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then shift; output=$1; fi
  shift
done
if [ -z "$output" ]; then
  printf '%s\n' '{"version":"offline-test"}'
elif [ "$output" = ./codecov ]; then
  cat > "$output" <<'UPLOADER'
#!/bin/sh
printf executed > "$EXECUTION_MARKER"
[ "$FAILURE" != upload ]
UPLOADER
else
  printf fixture > "$output"
fi
''',
                "gpg": r'''#!/bin/sh
case " $* " in
  *" --import "*) cat >/dev/null; [ "$FAILURE" != import ] ;;
  *" --verify "*) [ "$FAILURE" != signature ] ;;
  *) exit 99 ;;
esac
''',
                "shasum": '#!/bin/sh\n[ "$FAILURE" != checksum ]\n',
                "sha256sum": '#!/bin/sh\n[ "$FAILURE" != checksum ]\n',
                "sleep": "#!/bin/sh\nexit 0\n",
            }
            for name, source in stubs.items():
                target = bin_directory / name
                target.write_text(source)
                target.chmod(0o700)
            marker = work / "executed"
            # A minimal environment prevents inherited uploader overrides or tokens.
            environment = {
                "PATH": str(bin_directory) + os.pathsep + os.defpath,
                "CC_OS": "linux",
                "CC_VERSION": "offline-test",
                "CC_FAIL_ON_ERROR": str(self.upload["with"]["fail_ci_if_error"]).lower(),
                "FAILURE": failure,
                "EXECUTION_MARKER": str(marker),
            }
            result = subprocess.run(
                ["bash", str(FIXTURE)],
                cwd=work,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return result, marker.exists()

    def test_verification_failures_never_execute_downloaded_binary(self):
        for failure in ("import", "signature", "checksum"):
            with self.subTest(failure=failure):
                result, executed = self.run_wrapper(failure)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(executed, result.stdout + result.stderr)

    def test_verified_uploader_failure_remains_a_failed_step(self):
        result, executed = self.run_wrapper("upload")
        self.assertTrue(executed, result.stdout + result.stderr)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_verified_success_can_execute(self):
        result, executed = self.run_wrapper("none")
        self.assertTrue(executed, result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
