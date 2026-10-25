"""Execute the image's readonly-input guard without changing local identities."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "The image preflight requires a POSIX shell")
class GoBuildPreflightTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("Real test -r/-w predicates require an unprivileged test user")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "inputs"
        self.nested = self.inputs / "nested"
        self.nested.mkdir(parents=True)
        self.file = self.nested / "reviewed-lock"
        self.file.write_text("synthetic reviewed input\n")
        self.nodes = [self.inputs, self.nested, self.file]
        self.addCleanup(self.restore_permissions)
        for path in self.nodes:
            path.chmod(0o555 if path.is_dir() else 0o444)

        source = (ROOT / "deploy/runner-reserve/Dockerfile").read_text().replace("\\\n", " ")
        matches = [shlex.split(line[4:]) for line in source.splitlines()
                   if line.startswith("RUN ")
                   and "find /opt/victron-go-tools-inputs -exec" in line]
        self.assertEqual(len(matches), 1, "Extract the actual image-stage input guard")
        words = matches[0]
        start = words.index("find")
        self.find_command = words[start:start + 9]
        self.assertEqual(self.find_command[:5],
                         ["find", "/opt/victron-go-tools-inputs", "-exec", "sh", "-ec"])
        self.assertEqual(self.find_command[6:], ["sh", "{}", "+"])
        self.guard = self.find_command[5]

        # Fake only the root owner report. Read/write/link/type checks and find
        # remain real, so no chown, root account or production path is needed.
        binaries = self.root / "bin"
        binaries.mkdir()
        stat = binaries / "stat"
        stat.write_text("#!/bin/sh\n"
                        "test \"$#\" = 3 && test \"$1\" = -c "
                        "&& test \"$2\" = '%u:%g' || exit 94\n"
                        "printf '%s\\n' \"${FIXTURE_OWNER:-0:0}\"\n")
        stat.chmod(0o700)
        self.env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                    "FIXTURE_OWNER": "0:0", "LC_ALL": "C"}

    def restore_permissions(self):
        for path in self.nodes:
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)

    def run_guard(self, *paths, owner="0:0"):
        return subprocess.run(["sh", "-ec", self.guard, "sh", *map(str, paths)],
                              env={**self.env, "FIXTURE_OWNER": owner},
                              capture_output=True, text=True, timeout=5, check=False)

    def test_accepts_readonly_files_and_searchable_directories(self):
        result = self.run_guard(*self.nodes)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_writable_file_before_a_valid_input(self):
        self.file.chmod(0o644)
        result = self.run_guard(self.file, self.inputs)
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_rejects_writable_directory_before_a_valid_input(self):
        self.nested.chmod(0o755)
        result = self.run_guard(self.nested, self.file)
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_rejects_symlinks_even_to_valid_readonly_inputs(self):
        for index, target in enumerate((self.file, self.nested)):
            with self.subTest(target=target.name):
                link = self.root / ("link-" + str(index))
                link.symlink_to(target)
                result = self.run_guard(link, self.inputs)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_rejects_unreadable_file_before_a_valid_input(self):
        self.file.chmod(0o000)
        result = self.run_guard(self.file, self.inputs)
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_rejects_nonroot_owner(self):
        result = self.run_guard(self.file, owner="10001:10001")
        self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_find_exec_plus_propagates_guard_failure(self):
        command = [*self.find_command]
        command[1] = str(self.inputs)
        accepted = subprocess.run(command, env=self.env, capture_output=True,
                                  text=True, timeout=5, check=False)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.file.chmod(0o644)
        rejected = subprocess.run(command, env=self.env, capture_output=True,
                                  text=True, timeout=5, check=False)
        self.assertNotEqual(rejected.returncode, 0, rejected.stderr)
