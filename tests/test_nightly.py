"""Exercise local nightly isolation and reports using disposable Git repositories."""

import io
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
# Import the checkout's runner after explicitly adding its location.
# pylint: disable=wrong-import-position
import run_nightly as nightly

# pylint: enable=wrong-import-position


@contextmanager
def temporary_workspace():
    """Keep fixture lifetime tied to the unittest cleanup context."""
    with tempfile.TemporaryDirectory() as directory:
        yield Path(directory).resolve()


class LocalNightlyTests(unittest.TestCase):
    """No test fetches source, contacts GitHub or runs real project workloads."""

    def setUp(self):
        self.directory = self.enterContext(temporary_workspace())
        self.root = self.directory / "checkouts"
        self.root.mkdir()
        self.logs = self.directory / "reports"
        self.inventory = self.directory / "fleet.json"
        self.items = []

    @staticmethod
    def git(directory, *args):
        """Run local fixture setup independently of developer signing and hooks."""
        return subprocess.run(
            [
                "git",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Nightly Test",
                "-c",
                "user.email=nightly@example.invalid",
                "-c",
                "core.hooksPath=" + str(directory / ".git/no-fixture-hooks"),
                *args,
            ],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def checkout(self, name, command="printf 'local check ran\\n'\nexit 0\n"):
        """Create a committed repository with a tiny real local check entrypoint."""
        directory = self.root / name
        (directory / "scripts").mkdir(parents=True)
        item = {
            "repository": "owner/" + name,
            "directory": name,
            "default_branch": "main",
        }
        (directory / ".release-policy.json").write_text(
            json.dumps({"repository": item["repository"], "mode": "validation-only"}),
            encoding="utf-8",
        )
        (directory / "scripts/release.py").write_text(
            "import subprocess, sys\n"
            "assert sys.argv[1:] == ['check']\n"
            "sys.exit(subprocess.run(['bash', 'scripts/ci.sh'], check=False).returncode)\n",
            encoding="utf-8",
        )
        (directory / "scripts/ci.sh").write_text(command, encoding="utf-8")
        self.git(directory, "init", "-q", "-b", "main")
        self.git(
            directory,
            "remote",
            "add",
            "origin",
            f"https://github.com/{item['repository']}.git",
        )
        self.git(directory, "add", ".")
        self.git(directory, "commit", "-qm", "fixture")
        self.items.append(item)
        return directory

    def invoke(self, *extra):
        """Invoke the CLI and keep its output available for assertion failures."""
        self.inventory.write_text(
            json.dumps({"schema": 1, "repositories": self.items}), encoding="utf-8"
        )
        args = [
            "run_nightly.py",
            "--root",
            str(self.root),
            "--logs",
            str(self.logs),
            "--inventory",
            str(self.inventory),
            *extra,
        ]
        with (
            mock.patch.object(sys, "argv", args),
            mock.patch.object(sys, "stdout", new_callable=io.StringIO) as out,
            mock.patch.object(sys, "stderr", new_callable=io.StringIO) as err,
        ):
            result = nightly.main()
        return result, out.getvalue() + err.getvalue()

    def summary(self):
        """Read the single persistent run report produced by this test."""
        paths = list(self.logs.glob("*/summary.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8")), paths[0].parent

    def test_failure_does_not_skip_next_repo_and_reports_are_durable(self):
        """A failed command returns nonzero while later repository checks still run."""
        first = self.checkout("first", "printf 'intentional failure\\n'\nexit 7\n")
        second = self.checkout("second")
        heads = [self.git(path, "rev-parse", "HEAD") for path in (first, second)]
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        summary, directory = self.summary()
        self.assertEqual(summary["status"], "failed")
        rows = summary["results"]
        self.assertEqual([row["status"] for row in rows], ["failed", "passed"])
        self.assertEqual([row["returncode"] for row in rows], [7, 0])
        self.assertEqual([row["head"] for row in rows], heads)
        self.assertLessEqual(rows[0]["finished_at"], rows[1]["started_at"])
        self.assertEqual(rows[1]["local_origin_sha"], None)
        self.assertIn("no fetch", rows[1]["remote_freshness"])
        self.assertIn(
            "local check ran", Path(rows[1]["log"]).read_text(encoding="utf-8")
        )
        self.assertEqual(
            [
                json.loads(line)
                for line in (directory / "results.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ],
            rows,
        )
        self.assertEqual(
            [self.git(path, "rev-parse", "HEAD") for path in (first, second)], heads
        )
        self.assertEqual(self.git(second, "status", "--porcelain"), "")

    def test_selected_clean_repo_passes_and_records_stale_local_origin(self):
        """Selection is exact and cached origin metadata does not imply freshness."""
        self.checkout("unused", "exit 99\n")
        selected = self.checkout("selected")
        previous = self.git(selected, "rev-parse", "HEAD")
        self.git(selected, "update-ref", "refs/remotes/origin/main", previous)
        (selected / "README.md").write_text("new local source\n", encoding="utf-8")
        self.git(selected, "add", ".")
        self.git(selected, "commit", "-qm", "local change")
        code, output = self.invoke("--repo", "owner/selected")
        self.assertEqual(code, 0, output)
        summary, _ = self.summary()
        self.assertEqual(len(summary["results"]), 1)
        row = summary["results"][0]
        self.assertFalse(row["matches_local_origin"])
        self.assertEqual(row["local_origin_sha"], previous)
        self.assertIn("unknown", row["remote_freshness"])

    def test_dirty_checkout_is_reported_without_reset_or_execution(self):
        """Uncommitted operator work is preserved and cannot be called a passed run."""
        directory = self.checkout("dirty")
        edited = directory / "scripts/ci.sh"
        edited.write_text("exit 42\n", encoding="utf-8")
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        summary, _ = self.summary()
        row = summary["results"][0]
        self.assertTrue(row["dirty"])
        self.assertNotIn("returncode", row)
        self.assertIn("uncommitted", row["error"])
        self.assertEqual(edited.read_text(encoding="utf-8"), "exit 42\n")

    def test_origin_and_policy_identity_must_match(self):
        """A similarly named directory cannot impersonate the selected repository."""
        origin = self.checkout("origin")
        self.git(
            origin, "remote", "set-url", "origin", "https://github.com/other/repo.git"
        )
        policy = self.checkout("policy")
        (policy / ".release-policy.json").write_text(
            '{"repository":"other/repo"}', encoding="utf-8"
        )
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        summary, _ = self.summary()
        self.assertIn("Origin identity", summary["results"][0]["error"])
        self.assertIn("Local policy", summary["results"][1]["error"])

    def test_symlinked_checkout_and_entrypoint_are_rejected(self):
        """Neither an external checkout nor external script may escape the root."""
        directory = self.checkout("outside")
        external = self.directory / "outside"
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
        script = self.checkout("script") / "scripts/release.py"
        script.unlink()
        script.symlink_to(external / "scripts/release.py")
        code, output = self.invoke()
        self.assertEqual(code, 1, output)
        summary, _ = self.summary()
        self.assertIn("escapes", summary["results"][0]["error"])
        self.assertIn("regular files", summary["results"][1]["error"])

    def test_timeout_terminates_child_work_and_continues(self):
        """A controlled timeout is durably reported without skipping later checks."""
        self.checkout("slow")
        self.checkout("next")
        with mock.patch.object(
            nightly,
            "run_check",
            side_effect=[subprocess.TimeoutExpired("local check", 2), 0],
        ) as check:
            code, output = self.invoke("--timeout", "2")
        self.assertEqual(code, 1, output)
        summary, _ = self.summary()
        self.assertEqual(
            [row["status"] for row in summary["results"]],
            ["timeout", "passed"],
            json.dumps(summary),
        )
        self.assertEqual(check.call_count, 2)
        self.assertEqual([call.args[2] for call in check.call_args_list], [2, 2])

    def test_timeout_stops_descendants_after_the_group_leader_exits(self):
        """An exited parent cannot leave a child that ignores SIGTERM behind."""
        process = mock.MagicMock(pid=12345)
        process.__enter__.return_value = process
        process.wait.side_effect = [subprocess.TimeoutExpired("local check", 2), 0, 0]
        with (
            mock.patch.object(
                nightly.subprocess, "Popen", return_value=process
            ) as spawn,
            mock.patch.object(nightly.os, "killpg") as kill,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            nightly.run_check(self.root, io.StringIO(), 2)
        self.assertEqual(
            spawn.call_args.args[0], [sys.executable, "scripts/release.py", "check"]
        )
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(
            kill.call_args_list,
            [mock.call(12345, signal.SIGTERM), mock.call(12345, signal.SIGKILL)],
        )
        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(timeout=2), mock.call(timeout=5), mock.call()],
        )

    def test_real_timeout_is_reported_and_the_next_check_runs(self):
        """Exercise an actual sleeping child with full preflight failure diagnostics."""
        self.checkout("real-slow", "sleep 60\n")
        self.checkout("real-next")
        code, output = self.invoke("--timeout", "2")
        self.assertEqual(code, 1, output)
        summary, _ = self.summary()
        diagnostics = (
            json.dumps(summary)
            + "\n"
            + "\n".join(
                Path(row["log"]).read_text(encoding="utf-8")
                for row in summary["results"]
            )
        )
        self.assertEqual(
            [row["status"] for row in summary["results"]],
            ["timeout", "passed"],
            diagnostics,
        )

    def test_unknown_excluded_and_unsafe_inventory_fail_before_checks(self):
        """Bad inventory selections cannot silently succeed or traverse directories."""
        self.checkout("active")
        cases = [
            ("owner/unknown", None),
            ("owner/active", {"excluded_reason": "vendor"}),
            ("owner/active", {"directory": "../outside"}),
        ]
        original = dict(self.items[0])
        for name, changes in cases:
            with self.subTest(name=name, changes=changes):
                self.items[0] = {**original, **(changes or {})}
                code, output = self.invoke("--repo", name)
                self.assertEqual(code, 2, output)
                self.assertFalse(self.logs.exists())

    def test_logs_inside_checkout_and_overlapping_runs_are_rejected(self):
        """Reports cannot dirty tested source, and one log root admits only one run."""
        directory = self.checkout("repo")
        code, output = self.invoke("--logs", str(directory / "reports"))
        self.assertEqual(code, 2, output)
        self.assertIn("outside", output)
        with nightly.locked_logs(self.logs):
            code, output = self.invoke()
        self.assertEqual(code, 2, output)
        self.assertIn("already owns", output)
        self.assertFalse(list(self.logs.glob("*/summary.json")))


if __name__ == "__main__":
    unittest.main()
