"""Exercise the actual admission block offline, without host or Docker changes."""
import base64
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "deploy/runner-reserve/start-runner.sh").read_text()
ADMISSION = SOURCE.split("# The node must acknowledge", 1)[1].split(
    "\nmkdir -p /home/runner/_work", 1)[0]
ADMISSION = "# The node must acknowledge" + ADMISSION


class GuardBootstrapTests(unittest.TestCase):
    def run_admission(self, outcomes, *, optimized=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binaries = root / "bin"
            binaries.mkdir()
            temporary = root / "tmp"
            temporary.mkdir()
            config = root / "outcomes.json"
            config.write_text(json.dumps([
                {"code": code, "body": base64.b64encode(body).decode()}
                for code, body in outcomes
            ]))
            curl = binaries / "curl"
            curl.write_text("#!" + sys.executable + "\n" + '''
import base64, json, os, pathlib, sys
root = pathlib.Path(os.environ['GUARD_TEST_ROOT'])
log = root / 'requests.jsonl'
previous = log.read_text().splitlines() if log.exists() else []
outcomes = json.loads((root / 'outcomes.json').read_text())
outcome = outcomes[min(len(previous), len(outcomes) - 1)]
args = sys.argv[1:]
output = pathlib.Path(args[args.index('--output') + 1])
with log.open('a') as handle:
    handle.write(json.dumps({'args': args, 'mode': output.stat().st_mode & 0o777}) + '\\n')
output.write_bytes(base64.b64decode(outcome['body']))
sys.exit(outcome['code'])
''')
            parser = binaries / "python3"
            parser.write_text("#!/bin/bash\n"
                              'printf "parse\\n" >> "$GUARD_TEST_ROOT/parser.log"\n'
                              "exec " + shlex.quote(sys.executable) + ' "$@"\n')
            sleeper = binaries / "sleep"
            sleeper.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$GUARD_TEST_ROOT/sleep.log"\n')
            for executable in (curl, parser, sleeper):
                executable.chmod(0o700)
            env = dict(os.environ, PATH=str(binaries) + ":/usr/bin:/bin",
                       GUARD_TEST_ROOT=str(root), TMPDIR=str(temporary),
                       RUNNER_RESERVE_NODE_IP="192.0.2.10",
                       RUNNER_RESERVE_POD_UID="synthetic-private-pod-uid")
            if optimized:
                env["PYTHONOPTIMIZE"] = "2"
            result = subprocess.run(
                ["/bin/bash", "-c", "set -euo pipefail\numask 077\n" + ADMISSION +
                 '\nprintf "startup\\n" > "$GUARD_TEST_ROOT/startup"\n'],
                env=env, capture_output=True, text=True, timeout=30)
            requests = [json.loads(line) for line in (root / "requests.jsonl").read_text().splitlines()]
            parses = (root / "parser.log").read_text().splitlines() if (root / "parser.log").exists() else []
            sleeps = (root / "sleep.log").read_text().splitlines() if (root / "sleep.log").exists() else []
            started = (root / "startup").exists()
            self.assertEqual(list(temporary.iterdir()), [], "Admission response must be removed")
            for request in requests:
                args = request["args"]
                self.assertEqual(request["mode"], 0o600)
                self.assertIn("--fail", args)
                self.assertIn("--silent", args)
                self.assertNotIn("--insecure", args)
                self.assertNotIn("-k", args)
                self.assertEqual(args[args.index("--connect-timeout") + 1], "2")
                self.assertEqual(args[args.index("--max-time") + 1], "3")
                self.assertEqual(args[args.index("--cacert") + 1], "/opt/victron-ci-reserve/guard-ca.crt")
                self.assertEqual(args[-1], "https://192.0.2.10:19999/ready/synthetic-private-pod-uid")
            self.assertEqual(result.stdout, "")
            for forbidden in ("Traceback", "JSONDecodeError", "192.0.2.10", "synthetic-private-pod-uid", "private-body-marker"):
                self.assertNotIn(forbidden, result.stderr)
            return result, len(requests), len(parses), sleeps, started

    def test_transport_http_and_tls_failures_never_parse_or_start(self):
        for code in (7, 22, 60):
            with self.subTest(curl_exit=code):
                result, requests, parses, sleeps, started = self.run_admission(
                    [(code, b'{"admitted":true,"secret":"private-body-marker"}')])
                self.assertEqual(result.returncode, 1)
                self.assertEqual((requests, parses, started), (60, 0, False))
                self.assertEqual(sleeps, ["2"] * 59)
                self.assertEqual(len(result.stderr.splitlines()), 7)
                self.assertIn("after 60 attempts (request-failed)", result.stderr)

    def test_only_literal_true_admits_and_invalid_payloads_can_recover(self):
        rejected = [b"", b"private-body-marker", b"\xff", b"[]", b"null", b"true",
                    b"{}", b'{"admitted":false}', b'{"admitted":"true"}',
                    b'{"admitted":1}', b'{"admitted":null}', b'{"admitted":true}\0',
                    b"[" * 2000 + b"]" * 2000]
        for body in rejected:
            with self.subTest(body=body[:40]):
                result, requests, parses, sleeps, started = self.run_admission(
                    [(0, body), (0, b'{"admitted":true}')], optimized=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual((requests, parses, sleeps, started), (2, 2, ["2"], True))

    def test_literal_true_starts_without_sleep(self):
        result, requests, parses, sleeps, started = self.run_admission([(0, b'{"admitted":true}')])
        self.assertEqual(result.returncode, 0)
        self.assertEqual((requests, parses, sleeps, started), (1, 1, [], True))

    def test_denial_exhausts_budget_without_startup(self):
        result, requests, parses, sleeps, started = self.run_admission([(0, b'{"admitted":false}')])
        self.assertEqual(result.returncode, 1)
        self.assertEqual((requests, parses, started), (60, 60, False))
        self.assertEqual(sleeps, ["2"] * 59)
        self.assertEqual(len(result.stderr.splitlines()), 7)
        self.assertIn("after 60 attempts (not-admitted)", result.stderr)

    def test_invalid_json_exhausts_budget_without_tracebacks(self):
        result, requests, parses, sleeps, started = self.run_admission([(0, b"private-body-marker")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual((requests, parses, started), (60, 60, False))
        self.assertEqual(sleeps, ["2"] * 59)
        self.assertEqual(len(result.stderr.splitlines()), 7)
        self.assertIn("after 60 attempts (invalid-response)", result.stderr)


if __name__ == "__main__":
    unittest.main()
