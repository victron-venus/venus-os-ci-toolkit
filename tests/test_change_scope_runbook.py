"""Execute the documented regeneration guard without changing a live checkout."""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def read_snippet():
    document = (ROOT / "docs/CHANGE_SCOPE.md").read_text(encoding="utf-8")
    section = document.split("<!-- change-scope-safe-regeneration:start -->", 1)[1]
    section = section.split("<!-- change-scope-safe-regeneration:end -->", 1)[0]
    return re.search(r"```(?:sh|bash)\n(.*?)```", section, re.DOTALL).group(1)


PIN = re.search(r'test "\$toolkit_head" = ([0-9a-f]{40})', read_snippet()).group(1)
AVAILABLE = all(shutil.which(command) for command in ("sh", "git", "python3"))


def fixture_environment():
    """Isolate temporary repositories from inherited Git selectors and config."""
    return {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }


@unittest.skipUnless(AVAILABLE, "shell, git and python3 are required")
class SafeGeneratorSnippetTests(unittest.TestCase):
    def run_case(
        self,
        *,
        revision=PIN,
        status="",
        status_rc=0,
        generate_rc=0,
        revision_rc=0,
        index_flags="H 100644 abcd 0\tscripts/install_release.py\n",
        index_rc=0,
        root_rc=0,
        root_override=None,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            git = directory / "git"
            git.write_text("""#!/bin/sh
case "$3" in
  rev-parse) if [ "$4" = "--show-toplevel" ]; then printf '%s\\n' "$TEST_TOOLKIT_ROOT"; exit "$TEST_ROOT_RC"; fi; printf '%s\\n' "$TEST_REVISION"; exit "$TEST_REVISION_RC" ;;
  ls-files) printf '%s' "$TEST_INDEX_FLAGS"; exit "$TEST_INDEX_RC" ;;
  -c|status) printf '%s' "$TEST_STATUS"; exit "$TEST_STATUS_RC" ;;
  *) exit 81 ;;
esac
""")
            python = directory / "python3"
            python.write_text("""#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_CALLS"
case "$*" in *--check) exit 0 ;; *) exit "$TEST_GENERATE_RC" ;; esac
""")
            git.chmod(0o755)
            python.chmod(0o755)
            calls = directory / "calls"
            toolkit = directory / "toolkit"
            toolkit.mkdir()
            env = {
                **fixture_environment(),
                "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                "TEST_REVISION": revision,
                "TEST_REVISION_RC": str(revision_rc),
                "TEST_STATUS": status,
                "TEST_STATUS_RC": str(status_rc),
                "TEST_GENERATE_RC": str(generate_rc),
                "TEST_CALLS": str(calls),
                "TEST_INDEX_FLAGS": index_flags,
                "TEST_INDEX_RC": str(index_rc),
                "TEST_TOOLKIT_ROOT": root_override or str(toolkit),
                "TEST_ROOT_RC": str(root_rc),
            }
            snippet = read_snippet().replace(
                "/path/to/fixed/venus-os-ci-toolkit", str(toolkit)
            )
            run = subprocess.run(
                ["sh", "-c", snippet], env=env, capture_output=True, text=True
            )
            return (
                run.returncode,
                calls.read_text().splitlines() if calls.exists() else [],
            )

    def test_clean_exact_revision_runs_generation_and_check(self):
        code, calls = self.run_case()
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].endswith("--check"))

    def test_wrong_revision_never_starts_generator(self):
        code, calls = self.run_case(revision="0" * 40)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_revision_failure_with_correct_stdout_stops(self):
        code, calls = self.run_case(revision_rc=128)
        self.assertEqual(code, 128)
        self.assertEqual(calls, [])

    def test_revision_failure_with_empty_stdout_stops(self):
        code, calls = self.run_case(revision="", revision_rc=128)
        self.assertEqual(code, 128)
        self.assertEqual(calls, [])

    def test_modified_source_never_starts_generator(self):
        code, calls = self.run_case(status=" M scripts/install_release.py\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_staged_source_never_starts_generator(self):
        code, calls = self.run_case(status="M  scripts/change_scope_migration.py\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_untracked_source_never_starts_generator(self):
        code, calls = self.run_case(status="?? scripts/injected.py\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_failed_status_command_cannot_look_clean(self):
        code, calls = self.run_case(status_rc=128)
        self.assertEqual(code, 128)
        self.assertEqual(calls, [])

    def test_generator_failure_prevents_check(self):
        code, calls = self.run_case(generate_rc=23)
        self.assertEqual(code, 23)
        self.assertEqual(len(calls), 1)

    def test_assume_unchanged_entry_stops(self):
        code, calls = self.run_case(index_flags="h scripts/install_release.py\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_skip_worktree_entry_stops(self):
        code, calls = self.run_case(
            index_flags="H README.md\nS scripts/install_release.py\n"
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_combined_flags_stop(self):
        code, calls = self.run_case(index_flags="s scripts/install_release.py\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_failed_ls_files_cannot_look_safe(self):
        code, calls = self.run_case(index_flags="", index_rc=128)
        self.assertEqual(code, 128)
        self.assertEqual(calls, [])

    def test_symlink_index_entry_stops(self):
        code, calls = self.run_case(
            index_flags="H 120000 abcd 0\tscripts/install_release.py\n"
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_submodule_index_entry_stops(self):
        code, calls = self.run_case(index_flags="H 160000 abcd 0\tvendor\n")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_top_level_failure_with_correct_stdout_stops(self):
        code, calls = self.run_case(root_rc=128)
        self.assertEqual(code, 128)
        self.assertEqual(calls, [])

    def test_redirected_top_level_stops(self):
        code, calls = self.run_case(root_override="/")
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])


@unittest.skipUnless(AVAILABLE, "shell, git and python3 are required")
class RealGitGuardTests(unittest.TestCase):
    def fixture(self, change=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "toolkit"
            repo.mkdir()
            calls = root / "calls"
            env = {**fixture_environment(), "TEST_CALLS": str(calls)}

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(repo), *args],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )

            git("init", "-q")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (repo / "scripts").mkdir()
            script = repo / "scripts/install_release.py"
            script.write_text(
                "import os\nfrom pathlib import Path\np=Path(os.environ['TEST_CALLS'])\np.write_text((p.read_text() if p.exists() else '')+'run\\n')\n"
            )
            (repo / ".gitignore").write_text("__pycache__/\nshadow.py\n")
            git("add", ".")
            git("-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
            head = git("rev-parse", "HEAD").strip()
            injected = change(repo, git) or {} if change else {}
            status = git("status", "--porcelain=v1", "--untracked-files=all")
            snippet = (
                read_snippet()
                .replace("/path/to/fixed/venus-os-ci-toolkit", shlex.quote(str(repo)))
                .replace(PIN, head)
            )
            p = subprocess.run(
                ["sh", "-c", snippet],
                env={**env, **injected},
                capture_output=True,
                text=True,
            )
            return (
                p.returncode,
                calls.read_text().splitlines() if calls.exists() else [],
                status,
            )

    def test_fixtures_do_not_touch_inherited_outer_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            outer = Path(temporary) / "outer"
            outer.mkdir()
            clean_env = fixture_environment()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(outer), *args],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    env=clean_env,
                )

            git("init", "-q")
            git("config", "user.name", "Outer fixture")
            git("config", "user.email", "outer@example.invalid")
            (outer / "keep.txt").write_text("outer repository must remain unchanged\n")
            git("add", ".")
            git("-c", "commit.gpgsign=false", "commit", "-qm", "outer fixture")

            def snapshot():
                return {
                    str(path.relative_to(outer)): path.read_bytes()
                    for path in outer.rglob("*")
                    if path.is_file()
                }

            inherited = {
                "GIT_DIR": str(outer / ".git"),
                "GIT_WORK_TREE": str(outer),
                "GIT_INDEX_FILE": str(outer / ".git/index"),
                "GIT_COMMON_DIR": str(outer / ".git"),
                "GIT_OBJECT_DIRECTORY": str(outer / ".git/objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(outer / ".git/objects"),
                "GIT_PREFIX": "outer-prefix/",
                "GIT_CONFIG": str(outer / ".git/config"),
            }
            before = snapshot()
            with patch.dict(os.environ, inherited):
                with patch("subprocess.run", wraps=subprocess.run) as run:
                    code, calls, status = self.fixture()
                    fake_code, fake_calls = SafeGeneratorSnippetTests().run_case()
                self.assertEqual((code, len(calls), status), (0, 2, ""))
                self.assertEqual((fake_code, len(fake_calls)), (0, 2))
                for call in run.call_args_list:
                    self.assertFalse(
                        any(key.startswith("GIT_") for key in call.kwargs["env"])
                    )
                for key, value in inherited.items():
                    self.assertEqual(os.environ[key], value)
            self.assertEqual(snapshot(), before)

    def test_clean_regular_checkout(self):
        code, calls, status = self.fixture()
        self.assertEqual((code, len(calls), status), (0, 2, ""))

    def test_assume_unchanged_hidden_edit(self):
        def change(repo, git):
            git("update-index", "--assume-unchanged", "scripts/install_release.py")
            with (repo / "scripts/install_release.py").open("a") as f:
                f.write("# hidden edit\n")

        new = self.fixture(change)
        self.assertNotEqual(new[0], 0)
        self.assertEqual(new[1:], ([], ""))

    def test_skip_worktree_hidden_edit(self):
        def change(repo, git):
            git("update-index", "--skip-worktree", "scripts/install_release.py")
            with (repo / "scripts/install_release.py").open("a") as f:
                f.write("# hidden edit\n")

        new = self.fixture(change)
        self.assertNotEqual(new[0], 0)
        self.assertEqual(new[1:], ([], ""))

    def test_hidden_missing_file(self):
        def change(repo, git):
            git("update-index", "--skip-worktree", "scripts/install_release.py")
            (repo / "scripts/install_release.py").unlink()

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual((calls, status), ([], ""))

    def test_visible_missing_file(self):
        code, calls, status = self.fixture(
            lambda repo, git: (repo / "scripts/install_release.py").unlink()
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn(" D scripts/install_release.py", status)

    def test_ignored_shadow_source(self):
        def change(repo, git):
            (repo / "scripts/shadow.py").write_text("# shadow module\n")

        new = self.fixture(change)
        self.assertNotEqual(new[0], 0)
        self.assertEqual(new[1:], ([], ""))

    def test_submodule_even_with_ignore_all(self):
        def change(repo, git):
            git(
                "update-index",
                "--add",
                "--cacheinfo",
                "160000," + git("rev-parse", "HEAD").strip() + ",vendor",
            )
            git("config", "diff.ignoreSubmodules", "all")

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_symlink_replacing_regular_file(self):
        def change(repo, git):
            p = repo / "scripts/install_release.py"
            p.unlink()
            p.symlink_to("/tmp/not-used")

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_untracked_hidden_by_config(self):
        def change(repo, git):
            git("config", "status.showUntrackedFiles", "no")
            (repo / "scripts/new.py").write_text("# file\n")

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_environment_worktree_cannot_hide_modified_source(self):
        def change(repo, git):
            import shutil

            clean = repo.parent / "clean"
            shutil.copytree(repo, clean)
            with (repo / "scripts/install_release.py").open("a") as f:
                f.write("# modified actual source\n")
            return {"GIT_WORK_TREE": str(clean)}

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_config_worktree_redirection_rejected(self):
        def change(repo, git):
            import shutil

            clean = repo.parent / "clean"
            shutil.copytree(repo, clean)
            git("config", "core.worktree", str(clean))
            with (repo / "scripts/install_release.py").open("a") as f:
                f.write("# modified actual source\n")

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual((calls, status), ([], ""))

    def test_environment_git_dir_is_cleared(self):
        def change(repo, git):
            import shutil

            clean = repo.parent / "clean"
            shutil.copytree(repo, clean)
            with (repo / "scripts/install_release.py").open("a") as f:
                f.write("# modified actual source\n")
            return {"GIT_DIR": str(clean / ".git")}

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_environment_index_is_cleared(self):
        def change(repo, git):
            with (repo / "scripts/install_release.py").open("a") as f:
                f.write("# modified actual source\n")
            return {"GIT_INDEX_FILE": str(repo.parent / "missing-index")}

        code, calls, status = self.fixture(change)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])


class RunbookWorkflowIntegrationTests(unittest.TestCase):
    def test_supported_generator_pin_requires_explicit_source_review(self):
        self.assertEqual(
            PIN,
            "523765f36f4909b72e77dff29c073521fdfbf795",
            "Changing the supported generator requires source review and native validation",
        )

    def documented_test_step(self):
        import yaml

        workflow = yaml.load(
            (ROOT / ".github/workflows/quality-gate.yml").read_text(),
            Loader=yaml.BaseLoader,
        )
        job = workflow["jobs"]["workflow-contracts"]
        self.assertNotIn("if", job)
        steps = [
            step
            for step in job["steps"]
            if step.get("name") == "Verify the documented scope regeneration procedure"
        ]
        self.assertEqual(len(steps), 1)
        self.assertNotIn("if", steps[0])
        self.assertEqual(
            steps[0]["run"],
            "python3 -m unittest discover -s tests -p 'test_change_scope_runbook.py' -v",
        )
        return steps[0]

    def test_documented_procedure_is_checked_for_documentation_only_changes(self):
        self.documented_test_step()

    def test_full_generation_retains_toolkit_only_contract_steps(self):
        import yaml

        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import install_release as generator
        finally:
            sys.path.pop(0)
        policy = json.loads((ROOT / ".release-policy.json").read_text())
        workflow = generator.quality(policy, ROOT)
        actual = workflow["jobs"]["workflow-contracts"]
        stored = yaml.load(
            (ROOT / ".github/workflows/quality-gate.yml").read_text(),
            Loader=yaml.BaseLoader,
        )["jobs"]["workflow-contracts"]
        names = {
            "Validate the required source package inventory",
            "Verify the documented scope regeneration procedure",
        }
        self.assertNotIn("if", actual)
        for name in names:
            expected_steps = [
                step for step in stored["steps"] if step.get("name") == name
            ]
            actual_steps = [
                step for step in actual["steps"] if step.get("name") == name
            ]
            self.assertEqual(len(expected_steps), 1)
            self.assertEqual(actual_steps, expected_steps)
            self.assertNotIn("if", actual_steps[0])
        with tempfile.TemporaryDirectory() as temporary:
            for directory in (None, Path(temporary)):
                with self.subTest(directory=directory):
                    consumer = generator.workflow_contract_job(directory)
                    self.assertFalse(
                        names.intersection(
                            step.get("name") for step in consumer["steps"]
                        )
                    )

    def test_scoped_regeneration_preserves_existing_runbook_test_step(self):
        import yaml

        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import change_scope_migration as migration
            import install_release as generator
        finally:
            sys.path.pop(0)
        fixture = ROOT / "tests/fixtures/change_scope_migration"
        with tempfile.TemporaryDirectory() as temporary:
            consumer = Path(temporary)
            policy = json.loads((fixture / "policy.json").read_text())
            policy["change_scope"] = {
                "documentation_paths": [],
                "required_paths": [],
                "always_validate_workflows": ["codeql.yml", "release-security.yml"],
            }
            (consumer / ".release-policy.json").write_text(json.dumps(policy))
            for path, contents in {
                migration.QUALITY: (fixture / "quality-gate.yml").read_text(),
                migration.RELEASE: (fixture / "release-pipeline.yml").read_text(),
                migration.CLASSIFIER: generator.consumer_python(
                    (ROOT / migration.CLASSIFIER).read_text()
                ),
            }.items():
                target = consumer / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(contents)
            path = consumer / migration.QUALITY
            quality = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            step = self.documented_test_step()
            quality["jobs"]["workflow-contracts"]["steps"].append(step)
            original_steps = quality["jobs"]["workflow-contracts"]["steps"]
            path.write_text(migration.MARKER + yaml.safe_dump(quality, sort_keys=False))
            release_before = (consumer / migration.RELEASE).read_bytes()
            changes = migration.render(consumer, generator)
            self.assertIn(migration.QUALITY, changes)
            for name, contents in changes.items():
                (consumer / name).write_text(contents)
            actual = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
            self.assertEqual(
                actual["jobs"]["workflow-contracts"]["steps"], original_steps
            )
            self.assertEqual(
                (consumer / migration.RELEASE).read_bytes(), release_before
            )
            self.assertEqual(migration.render(consumer, generator), {})


if __name__ == "__main__":
    unittest.main()
