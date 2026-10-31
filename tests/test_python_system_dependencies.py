"""Exercise the shipped APT step without changing the host's packages or sources."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = yaml.load(
    (ROOT / ".github/workflows/python-ci.yml").read_text(), Loader=yaml.BaseLoader
)
JOB = WORKFLOW["jobs"]["ci"]
STEP = next(step for step in JOB["steps"] if step.get("name") == "Install system dependencies")
OPTIONS = [
    "-o", "Acquire::Retries=2",
    "-o", "Acquire::http::Timeout=20",
    "-o", "Acquire::https::Timeout=20",
    "-o", "DPkg::Lock::Timeout=60",
]
PACKAGES = [
    "libdbus-1-dev", "libglib2.0-dev", "libcairo2-dev",
    "libgirepository-2.0-dev", "libgirepository1.0-dev",
]
GNU_TIMEOUT = shutil.which("timeout") or shutil.which("gtimeout")

FAKE_COMMAND = r'''
import json
import os
import signal
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps({"command": name, "args": args}) + "\n")
if name == "sudo":
    os.execvp(args[0], args)
if name == "timeout":
    assert args[:2] == ["--signal=TERM", "--kill-after=15s"], args
    assert args[2] in {"180s", "360s"}, args
    os.execvp(args[3], args[3:])
if name == "apt-get":
    operation = "update" if "update" in args else "install"
    mode = os.environ.get(operation.upper() + "_MODE", "success")
    if mode == "fail":
        print("simulated APT error", file=sys.stderr)
        sys.exit(100)
    if mode == "stalled":
        time.sleep(60)
    if mode == "active-ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        while True:
            print("simulated continuing download", flush=True)
            time.sleep(0.02)
'''


class PythonSystemDependencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        for command in ("sudo", "timeout", "apt-get", "ldconfig", "pkg-config"):
            path = self.bin / command
            path.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
            path.chmod(0o755)
        self.env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "COMMAND_LOG": str(self.log),
            "PKG_CONFIG_PATH": "",
        }

    def execute(self, script=None, **env):
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script or STEP["run"]],
            cwd=self.root, env=self.env | env,
            capture_output=True, text=True, timeout=8, check=False,
        )

    def calls(self, command):
        return [row["args"] for row in map(json.loads, self.log.read_text().splitlines())
                if row["command"] == command]

    def test_success_preserves_packages_and_mirror_configuration(self):
        sources = self.root / "etc/apt"
        sources.mkdir(parents=True)
        fixtures = {
            sources / "apt-mirrors.txt": b"http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
                                         b"https://archive.ubuntu.com/ubuntu/\tpriority:2\n",
            sources / "sources.list": b"# operator-provisioned sources\n",
            sources / "sources.list.d/ubuntu.sources": (
                b"Types: deb\nURIs: mirror+file:/etc/apt/apt-mirrors.txt\n"
                b"Suites: noble noble-updates noble-backports\nComponents: main universe\n"
                b"Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
            ),
            sources / "sources.list.d/thirdparty.list": (
                b"deb [signed-by=/usr/share/keyrings/example.gpg] https://example.org/packages stable main\n"
            ),
            sources / "apt.conf.d/99-trust": (
                b'Acquire::AllowInsecureRepositories "false";\n'
                b'APT::Get::AllowUnauthenticated "false";\n'
                b'Acquire::https::Verify-Peer "true";\n'
            ),
        }
        for path, content in fixtures.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        # Redirect any future literal source access into the fixture tree.
        result = self.execute(STEP["run"].replace("/etc/apt", str(sources)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("apt-get"), [
            OPTIONS + ["update", "--error-on=any"],
            OPTIONS + ["install", "-y"] + PACKAGES,
        ])
        self.assertEqual(self.calls("timeout"), [
            ["--signal=TERM", "--kill-after=15s", "180s", "apt-get"] + OPTIONS + ["update", "--error-on=any"],
            ["--signal=TERM", "--kill-after=15s", "360s", "apt-get"] + OPTIONS + ["install", "-y"] + PACKAGES,
        ])
        self.assertEqual(self.calls("ldconfig"), [[]])
        self.assertIn(["--exists", "dbus-1"], self.calls("pkg-config"))
        self.assertIn("SUCCESS: All dependencies found by pkg-config", result.stdout)
        for path, content in fixtures.items():
            self.assertEqual(path.read_bytes(), content)

    def test_failed_or_partial_index_refresh_stops_before_install(self):
        result = self.execute(UPDATE_MODE="fail")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(self.calls("apt-get"), [OPTIONS + ["update", "--error-on=any"]])
        self.assertEqual(self.calls("ldconfig"), [])
        self.assertEqual(self.calls("pkg-config"), [])
        self.assertIn("APT update failed with exit 100", result.stderr)

    def test_failed_install_stops_before_library_and_test_checks(self):
        result = self.execute(INSTALL_MODE="fail")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(len(self.calls("apt-get")), 2)
        self.assertEqual(self.calls("ldconfig"), [])
        self.assertEqual(self.calls("pkg-config"), [])
        self.assertIn("APT install failed with exit 100", result.stderr)

    def test_step_is_required_linux_only_and_fits_inside_job_deadline(self):
        self.assertEqual(STEP["if"], "runner.os == 'Linux'")
        self.assertEqual(STEP["shell"], "bash")
        self.assertEqual(STEP["timeout-minutes"], "10")
        self.assertLess(int(STEP["timeout-minutes"]), int(JOB["timeout-minutes"]))
        self.assertNotIn("continue-on-error", STEP)

    @unittest.skipUnless(GNU_TIMEOUT, "GNU timeout is required for real deadline probes")
    def test_real_deadlines_stop_stalled_and_continuously_active_downloads(self):
        # Change only the durations to keep real process/signal checks fast.
        script = STEP["run"].replace("180s", "0.4s").replace("360s", "0.4s").replace("15s", "0.2s")
        timeout_command = self.bin / "timeout"
        timeout_command.unlink()
        timeout_command.symlink_to(GNU_TIMEOUT)
        for operation in ("update", "install"):
            for mode, status, diagnostic in (
                ("stalled", 124, "exceeded its 0.4s deadline"),
                ("active-ignore-term", 137, "was killed"),
            ):
                with self.subTest(operation=operation, mode=mode):
                    self.log.unlink(missing_ok=True)
                    started = time.monotonic()
                    result = self.execute(script, **{operation.upper() + "_MODE": mode})
                    self.assertEqual(result.returncode, status, result.stderr)
                    self.assertLess(time.monotonic() - started, 4)
                    self.assertIn(f"APT {operation} {diagnostic}", result.stderr)
                    self.assertEqual(len(self.calls("apt-get")), 1 if operation == "update" else 2)
                    self.assertEqual(self.calls("ldconfig"), [])
                    self.assertEqual(self.calls("pkg-config"), [])


if __name__ == "__main__":
    unittest.main()
