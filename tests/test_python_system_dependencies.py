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
if name == "dpkg":
    print(os.environ.get("HOST_ARCH", "amd64"))
if name == "apt-get":
    if any(arg.startswith("Dir::Etc::sourcelist=") for arg in args):
        source = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("Dir::Etc::sourcelist=")))
        lists = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("Dir::State::lists=")))
        with open(os.environ["COMMAND_LOG"], "a") as log:
            log.write(json.dumps({"command": "configuration", "args": [],
                                  "source": str(source), "sources": source.read_text(),
                                  "mirrors": (source.parent / "mirrors.txt").read_text(),
                                  "lists": str(lists), "listed": sorted(x.name for x in lists.iterdir()),
                                  "directory_mode": source.parent.stat().st_mode & 0o777,
                                  "lists_mode": lists.stat().st_mode & 0o777,
                                  "source_mode": source.stat().st_mode & 0o777,
                                  "mirror_mode": (source.parent / "mirrors.txt").stat().st_mode & 0o777}) + "\n")
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
        for command in ("sudo", "timeout", "apt-get", "ldconfig", "pkg-config", "dpkg"):
            path = self.bin / command
            path.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
            path.chmod(0o755)
        self.os_release = self.root / "os-release"
        self.os_release.write_text("ID=ubuntu\nVERSION_CODENAME=noble\n")
        self.keyring = self.root / "ubuntu-archive-keyring.gpg"
        self.keyring.write_bytes(b"fixture keyring must remain unchanged")
        self.env = os.environ | {
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "COMMAND_LOG": str(self.log),
            "PKG_CONFIG_PATH": "",
            "RUNNER_TEMP": str(self.root),
            "RUNNER_ENVIRONMENT": "github-hosted",
        }

    def execute(self, script=None, **env):
        script = (script or STEP["run"]).replace("/etc/os-release", str(self.os_release)).replace("/usr/share/keyrings/ubuntu-archive-keyring.gpg", str(self.keyring))
        return subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
            cwd=self.root, env=self.env | env,
            capture_output=True, text=True, timeout=8, check=False,
        )

    def calls(self, command):
        if not self.log.exists():
            return []
        return [row["args"] for row in map(json.loads, self.log.read_text().splitlines())
                if row["command"] == command]

    def options(self):
        first = self.calls("apt-get")[0]
        self.assertEqual(first[0], "-o")
        self.assertTrue(first[1].startswith("Dir::Etc::sourcelist="))
        self.assertEqual(first[2:4], ["-o", "Dir::Etc::sourceparts=-"])
        self.assertEqual(first[4], "-o")
        self.assertTrue(first[5].startswith("Dir::State::lists="))
        return first[:6] + OPTIONS


    def test_fresh_isolated_https_sources_match_for_update_and_install(self):
        result = self.execute("umask 077\n" + STEP["run"])
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [row for row in map(json.loads, self.log.read_text().splitlines())
                   if row["command"] == "configuration"]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0], records[1])
        record = records[0]
        self.assertEqual(record["mirrors"].splitlines(), [
            "https://archive.ubuntu.com/ubuntu/\tpriority:1",
            "https://security.ubuntu.com/ubuntu/\tpriority:2",
        ])
        self.assertEqual(record["sources"].splitlines(), [
            "Types: deb", "URIs: mirror+file:" + str(Path(record["source"]).parent / "mirrors.txt"),
            "Suites: noble noble-updates noble-backports noble-security",
            "Components: main restricted universe multiverse", "Architectures: amd64",
            "Signed-By: " + str(self.keyring),
        ])
        self.assertEqual(record["listed"], [])
        self.assertEqual(record["directory_mode"], 0o755)
        self.assertEqual(record["lists_mode"], 0o755)
        self.assertEqual(record["source_mode"], 0o644)
        self.assertEqual(record["mirror_mode"], 0o644)
        self.assertEqual(Path(record["lists"]).parent, Path(record["source"]).parent)
        self.assertFalse(Path(record["source"]).parent.exists())
        self.assertEqual(self.keyring.read_bytes(), b"fixture keyring must remain unchanged")
        for forbidden in ("trusted=yes", "allow-unauthenticated", "Verify-Peer=false", "AllowInsecureRepositories=true"):
            self.assertNotIn(forbidden, STEP["run"])


    def test_private_runner_temp_does_not_hide_mirror_from_apt(self):
        """Use a traversable system parent without changing runner-owned permissions."""
        private = self.root / "private-runner-temp"
        private.mkdir(mode=0o700)
        result = self.execute("umask 077\n" + STEP["run"], RUNNER_TEMP=str(private))
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [row for row in map(json.loads, self.log.read_text().splitlines())
                   if row["command"] == "configuration"]
        self.assertEqual(len(records), 2)
        for record in records:
            parent = Path(record["source"]).parent
            self.assertEqual(parent.parent.resolve(), Path("/tmp").resolve())
            self.assertFalse(parent.is_relative_to(private))
            self.assertEqual(record["directory_mode"], 0o755)
            self.assertEqual(record["mirror_mode"], 0o644)
            self.assertFalse(parent.exists())
        self.assertEqual(private.stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(private.iterdir()), [])
        self.assertNotIn("${RUNNER_TEMP", STEP["run"])

    def test_self_hosted_runner_retains_operator_sources(self):
        self.os_release.write_text("ID=debian\nVERSION_CODENAME=trixie\n")
        self.keyring.unlink()
        result = self.execute(RUNNER_ENVIRONMENT="self-hosted", HOST_ARCH="arm64")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("apt-get"), [
            OPTIONS + ["update", "--error-on=any"],
            OPTIONS + ["install", "-y"] + PACKAGES,
        ])
        self.assertFalse(list(self.root.glob("host-apt.*")))


    def test_unexpected_host_or_missing_keyring_stops_before_apt(self):
        for release, architecture, keyring in (
            ("ID=debian\nVERSION_CODENAME=noble\n", "amd64", True),
            ("ID=ubuntu\nVERSION_CODENAME=jammy\n", "amd64", True),
            ("ID=ubuntu\nVERSION_CODENAME=noble\n", "arm64", True),
            ("ID=ubuntu\nVERSION_CODENAME=noble\n", "amd64", False),
        ):
            with self.subTest(release=release, architecture=architecture, keyring=keyring):
                self.log.unlink(missing_ok=True)
                self.os_release.write_text(release)
                if not keyring:
                    self.keyring.unlink()
                result = self.execute(HOST_ARCH=architecture)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls("apt-get"), [])
                self.assertFalse(list(self.root.glob("host-apt.*")))


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
            self.options() + ["update", "--error-on=any"],
            self.options() + ["install", "-y"] + PACKAGES,
        ])
        self.assertEqual(self.calls("timeout"), [
            ["--signal=TERM", "--kill-after=15s", "180s", "apt-get"] + self.options() + ["update", "--error-on=any"],
            ["--signal=TERM", "--kill-after=15s", "360s", "apt-get"] + self.options() + ["install", "-y"] + PACKAGES,
        ])
        self.assertEqual(self.calls("ldconfig"), [[]])
        self.assertIn(["--exists", "dbus-1"], self.calls("pkg-config"))
        self.assertIn("SUCCESS: All dependencies found by pkg-config", result.stdout)
        for path, content in fixtures.items():
            self.assertEqual(path.read_bytes(), content)

    def test_failed_or_partial_index_refresh_stops_before_install(self):
        result = self.execute(UPDATE_MODE="fail")
        self.assertEqual(result.returncode, 100, result.stderr)
        self.assertEqual(self.calls("apt-get"), [self.options() + ["update", "--error-on=any"]])
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

    def test_hosted_image_matches_noble_guard_without_changing_self_hosted_routing(self):
        """Pin the hosted fallback while retaining trusted self-hosted routing."""
        expected = '${{ fromJSON((vars.CI_RUNNER_MODE == \'self-hosted\' || vars.CI_RUNNER_MODE == \'k3s\') && ((github.event_name == \'pull_request\' && github.event.pull_request.head.repo.full_name == github.repository) || (contains(fromJSON(\'["push","schedule","workflow_dispatch"]\'), github.event_name) && github.ref == format(\'refs/heads/{0}\', github.event.repository.default_branch))) && (vars.CI_RUNNER_LABELS || \'[]\') || \'["ubuntu-24.04"]\') }}'
        self.assertEqual(JOB["runs-on"], expected)
        self.assertIn('test "$ID:$VERSION_CODENAME" = \'ubuntu:noble\'', STEP["run"])
        self.assertIn('test "$(dpkg --print-architecture)" = \'amd64\'', STEP["run"])

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
