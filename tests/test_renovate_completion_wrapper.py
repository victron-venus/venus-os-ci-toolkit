"""Exercise the production shell control flow without a producer or credentials."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


class CompletionWrapperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="renovate-wrapper-test-")
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.roster = self.work / "roster.json"
        self.roster.write_text(json.dumps(["fixture/one", "fixture/two"]))
        self.fake = self.work / "fake-node"
        self.fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json,os,pathlib,signal,sys,time\n"
            "args=sys.argv[1:]\n"
            "if args[0].endswith('verify-renovate-completion.cjs'):\n"
            " os.execv(os.environ['REAL_NODE'],[os.environ['REAL_NODE'],*args])\n"
            "if args[-1] in ('--version','--help'):\n"
            " print('44.215.0');sys.exit(0)\n"
            "log=pathlib.Path(os.environ['LOG_FILE'])\n"
            "assert log.parent.stat().st_mode & 0o777 == 0o700\n"
            "assert os.environ['LOG_FILE_FORMAT']=='json'\n"
            "assert os.environ['LOG_FILE_LEVEL']=='info'\n"
            "assert 'NODE_OPTIONS' not in os.environ\n"
            "mode=os.environ.get('FIXTURE_MODE','success')\n"
            "if mode=='signal':\n"
            " def stop(sig,frame):\n"
            "  pathlib.Path(os.environ['SIGNAL_RECEIPT']).write_text(str(sig));sys.exit(143)\n"
            " signal.signal(signal.SIGTERM,stop)\n"
            " pathlib.Path(os.environ['PID_RECEIPT']).write_text(str(os.getpid()))\n"
            " log.write_text('private fixture data')\n"
            " while True:time.sleep(0.1)\n"
            "records=[]\n"
            "for repository in json.loads(pathlib.Path(os.environ['ROSTER']).read_text()):\n"
            " record=dict(name='renovate',v=0,level=30,time='2026-11-07T18:00:00Z',repository=repository)\n"
            " records.append(dict(record,msg='Repository started',renovateVersion='44.215.0'))\n"
            " if mode!='incomplete':records.append(dict(record,msg='Repository finished',result='done',status='activated',enabled=True,onboarded=True,exitCode=0))\n"
            "if mode=='artifact':records.append(dict(records[-1],msg='Artifact generation failed before PR decision',level=40,artifactErrorCount=1))\n"
            "if mode=='fatal':records.append(dict(records[-1],msg='PRIVATE_SENTINEL_DO_NOT_ECHO',level=60))\n"
            "log.write_text(''.join(json.dumps(record)+'\\n' for record in records))\n"
            "sys.exit(7 if mode=='process-failure' else 0)\n"
        )
        self.fake.chmod(0o755)
        init = self.work / "containerbase-cli"
        init.write_text("#!/bin/sh\nexit 0\n")
        init.chmod(0o755)
        source = (ROOT / "scripts/renovate-security-runner.sh").read_text()
        # Substitute only external executable/mount/temp locations in the real
        # shell; its process, verification, mode, signal and cleanup logic runs.
        source = source.replace("/usr/local/renovate/node", str(self.fake))
        source = source.replace("/github-action/renovate-repositories.json", str(self.roster))
        source = source.replace("/tmp/renovate-completion.XXXXXXXX", str(self.work / "evidence.XXXXXXXX"))
        source = source.replace("${compat_dir%/}/verify-renovate-completion.cjs", str(ROOT / "scripts/verify-renovate-completion.cjs"))
        self.wrapper = self.work / "runner.sh"
        self.wrapper.write_text(source)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("RENOVATE_", "LOG_", "NODE_OPTIONS"))}
        self.env.update(PATH=str(self.work) + os.pathsep + os.environ["PATH"],
                        REAL_NODE=NODE, ROSTER=str(self.roster),
                        PID_RECEIPT=str(self.work / "child-pid"),
                        SIGNAL_RECEIPT=str(self.work / "signal"))

    def run_wrapper(self, mode="success", args=(), dry_run=None):
        env = dict(self.env, FIXTURE_MODE=mode)
        if dry_run is not None:
            env["RENOVATE_DRY_RUN"] = dry_run
        result = subprocess.run(["bash", str(self.wrapper), *args], env=env,
                                text=True, capture_output=True, timeout=5)
        self.assertFalse(list(self.work.glob("evidence.*")), "private log was not cleaned")
        return result

    def test_exact_roster_after_exit_zero_earns_only_bounded_receipt(self):
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertTrue(receipt["qualified"])
        self.assertEqual(receipt["finishedCount"], 2)

    def test_incomplete_or_failed_artifact_cannot_become_successful_action(self):
        for mode, code in [("incomplete", "INCOMPLETE_ROSTER"), ("artifact", "ARTIFACT_ERRORS"),
                           ("fatal", "ERROR_OR_FATAL")]:
            with self.subTest(mode=mode):
                result = self.run_wrapper(mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(json.loads(result.stderr)["code"], code)
                self.assertNotIn("PRIVATE_SENTINEL", result.stderr)

    def test_original_process_failure_status_is_preserved(self):
        result = self.run_wrapper("process-failure")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stdout, "")

    def test_explicit_dry_run_cannot_earn_production_receipt(self):
        result = self.run_wrapper("incomplete", dry_run="full")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no production completion receipt", result.stdout)
        self.assertNotIn('"qualified"', result.stdout)

    def test_false_dry_run_value_is_verified_as_production(self):
        result = self.run_wrapper(dry_run="false")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["qualified"])

    def test_unexpected_cli_mode_override_fails_before_producer(self):
        result = self.run_wrapper(args=("--dry-run=full",))
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unexpected dry-run", result.stderr)

    def test_unknown_dry_run_mode_fails_closed(self):
        result = self.run_wrapper(dry_run="unsupported")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Unsupported dry-run", result.stderr)

    def test_credential_free_version_probe_remains_successful_without_receipt(self):
        result = self.run_wrapper(args=("--version",))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "44.215.0\n")

    def test_term_is_forwarded_and_private_log_removed(self):
        process = subprocess.Popen(["bash", str(self.wrapper)], env=dict(self.env, FIXTURE_MODE="signal"),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 4
            while not (self.work / "child-pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.work / "child-pid").exists())
            process.terminate()
            stdout, stderr = process.communicate(timeout=4)
            self.assertEqual(process.returncode, 143, stderr)
            self.assertEqual(stdout, "")
            self.assertEqual((self.work / "signal").read_text(), "15")
            self.assertFalse(list(self.work.glob("evidence.*")))
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)


if __name__ == "__main__":
    unittest.main()
