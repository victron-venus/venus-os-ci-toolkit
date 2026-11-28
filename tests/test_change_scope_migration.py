"""Narrow migration against real generated workflows and fail-closed variations.

The public fixture is victron-venus/dbus-event-log at
4aeb17e334351e0aaa1b839a33b54a8572a9d531, read from GitHub on 2026-11-06.
"""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import change_scope
import change_scope_migration as migration
import install_release as generator

FIXTURE = Path(__file__).parent / "fixtures/change_scope_migration"


class ChangeScopeMigrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.policy = json.loads((FIXTURE / "policy.json").read_text())
        for name in ("quality-gate.yml", "release-pipeline.yml"):
            self.write(".github/workflows/" + name, (FIXTURE / name).read_text())
        self.write(migration.CLASSIFIER, generator.consumer_python(
            (generator.ROOT / migration.CLASSIFIER).read_text()))
        self.policy["change_scope"] = {
            "documentation_paths": [], "required_paths": [],
            "always_validate_workflows": ["codeql.yml", "release-security.yml"],
        }
        self.save_policy()

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    def save_policy(self):
        self.write(".release-policy.json", json.dumps(self.policy))

    def render(self):
        return migration.render(self.root, generator)

    def yaml(self, name):
        return yaml.load((self.root / name).read_text(), Loader=yaml.BaseLoader)

    def mutate(self, name, callback):
        document = self.yaml(name)
        callback(document)
        self.write(name, migration.MARKER + yaml.safe_dump(document, sort_keys=False))

    def test_actual_public_workflows_only_change_selected_source_bytes(self):
        original = {name: (self.root / name).read_text() for name in
                    (migration.QUALITY, migration.RELEASE, migration.CLASSIFIER, ".release-policy.json")}
        result = self.render()
        self.assertEqual(set(result), {migration.QUALITY})
        self.assertEqual({name: (self.root / name).read_text() for name in original}, original)
        # A safe release entry point is already full and remains byte-identical.
        self.assertNotIn(migration.RELEASE, result)
        for name, source in result.items():
            self.write(name, source)
        self.assertEqual(self.render(), {})
        old = yaml.load(original[migration.QUALITY], Loader=yaml.BaseLoader)
        new = self.yaml(migration.QUALITY)
        self.assertEqual(old["on"], new["on"])
        self.assertEqual(old["concurrency"], new["concurrency"])
        self.assertEqual(old["permissions"], new["permissions"])
        for name, job in new["jobs"].items():
            if name not in {"scope", "check-1", "check-2", "workflow-contracts", "gate"}:
                self.assertEqual(old["jobs"][name], job)
        self.assertNotIn("if", new["jobs"]["check-1"])
        self.assertNotIn("if", new["jobs"]["check-2"])
        self.assertNotIn("if", new["jobs"]["workflow-contracts"])
        self.assertEqual(new["jobs"]["check-0"]["if"], generator.FULL_SCOPE)

    def test_known_classifier_formatting_revision_is_upgraded_once(self):
        canonical = (self.root / migration.CLASSIFIER).read_text()
        legacy = canonical.replace("import re\n\n# Subprocess", "import re\n# Subprocess", 1)
        transition = tuple(hashlib.sha256(source.encode()).hexdigest()
                           for source in (legacy, canonical))
        self.assertEqual(transition, migration.CLASSIFIER_FORMAT_UPGRADE)
        self.write(migration.CLASSIFIER, legacy)
        before = (self.root / migration.RELEASE).read_bytes()
        changes = self.render()
        self.assertEqual(changes[migration.CLASSIFIER], canonical)
        self.assertNotIn(migration.RELEASE, changes)
        for name, source in changes.items():
            self.write(name, source)
        self.assertEqual(self.render(), {})
        self.assertEqual((self.root / migration.RELEASE).read_bytes(), before)

    def test_legacy_upgrade_rejects_an_unreviewed_canonical_target(self):
        canonical = (generator.ROOT / migration.CLASSIFIER).read_text()
        legacy = canonical.replace("import re\n\n# Subprocess", "import re\n# Subprocess", 1)
        self.write(migration.CLASSIFIER, generator.consumer_python(legacy))
        target = self.root / "other-toolkit"
        (target / "scripts").mkdir(parents=True)
        (target / migration.CLASSIFIER).write_text(canonical + "\n")
        with patch.object(generator, "ROOT", target):
            with self.assertRaisesRegex(ValueError, "classifier revision"):
                self.render()

    def test_other_classifier_whitespace_or_comments_are_not_accepted(self):
        canonical = (self.root / migration.CLASSIFIER).read_text()
        legacy = canonical.replace("import re\n\n# Subprocess", "import re\n# Subprocess", 1)
        for changed in (legacy + "\n", legacy + "# Local customization\n", canonical + "\n"):
            with self.subTest(source=changed[-40:]):
                self.write(migration.CLASSIFIER, changed)
                with self.assertRaisesRegex(ValueError, "classifier revision"):
                    self.render()
                self.assertEqual((self.root / migration.CLASSIFIER).read_text(), changed)

    def test_unsafe_release_shortcut_is_repaired_without_changing_other_inputs(self):
        original = (self.root / migration.RELEASE).read_text()
        unsafe = original.replace("FORCE_FULL: 'true'", 'FORCE_FULL: "false"', 1)
        self.assertNotEqual(unsafe, original)
        self.write(migration.RELEASE, unsafe)
        result = self.render()
        self.assertEqual(result[migration.RELEASE], unsafe.replace(
            'FORCE_FULL: "false"', 'FORCE_FULL: "true"', 1))
        for name, source in result.items():
            self.write(name, source)
        self.assertEqual(self.render(), {})

    def test_actual_gate_rejects_missing_failed_and_unproven_results(self):
        quality = yaml.load(self.render()[migration.QUALITY], Loader=yaml.BaseLoader)
        command = quality["jobs"]["gate"]["steps"][-1]["run"]
        self.assertTrue(command.startswith("python3 - <<'PY'\n") and command.endswith("PY\n"))
        source = command[len("python3 - <<'PY'\n"):-len("PY\n")]
        names = set(quality["jobs"]) - {"gate"}
        always = {"scope", "check-1", "check-2", "workflow-contracts"}

        def success(results):
            return subprocess.run([sys.executable, "-c", source],
                env={**os.environ, "RESULTS": json.dumps(results)},
                capture_output=True, timeout=10, check=False).returncode == 0

        for docs in (False, True):
            baseline = {name: {"result": "success" if not docs or name in always else "skipped"}
                        for name in names}
            baseline["scope"]["outputs"] = {
                "run": "false" if docs else "true",
                "reason": "documentation-only" if docs else "non-documentation-change"}
            self.assertTrue(success(baseline))
            for name in names:
                for outcome in ("failure", "cancelled"):
                    with self.subTest(docs=docs, job=name, outcome=outcome):
                        bad = copy.deepcopy(baseline)
                        bad[name]["result"] = outcome
                        self.assertFalse(success(bad))
                missing = copy.deepcopy(baseline)
                del missing[name]
                self.assertFalse(success(missing))
            bad = copy.deepcopy(baseline)
            bad["scope"]["outputs"] = {"run": "false", "reason": "git-error"}
            self.assertFalse(success(bad))
        bad = copy.deepcopy(baseline)
        bad["check-1"]["result"] = "skipped"
        self.assertFalse(success(bad))

    def test_private_schedule_guard_and_all_release_nodes_are_preserved(self):
        old = copy.deepcopy(self.policy)
        old.pop("change_scope")
        old["visibility"] = "private"
        old.pop("sign_releases", None)
        old.pop("fuzzing", None)
        self.policy = copy.deepcopy(old)
        self.policy["change_scope"] = {"always_validate_workflows": ["codeql.yml", "release-security.yml"]}
        self.save_policy()
        self.write(migration.QUALITY, generator.dump(generator.quality(old)))
        self.write(migration.RELEASE, generator.dump(generator.release(old)))
        original_q, original_r = self.yaml(migration.QUALITY), self.yaml(migration.RELEASE)
        changes = self.render()
        new_q = yaml.load(changes[migration.QUALITY], Loader=yaml.BaseLoader)
        new_r = yaml.load(changes.get(migration.RELEASE, (self.root / migration.RELEASE).read_text()), Loader=yaml.BaseLoader)
        self.assertEqual(new_q["jobs"]["scope"]["if"], original_q["jobs"]["scope"]["if"])
        self.assertEqual(new_q["jobs"]["gate"]["if"], original_q["jobs"]["gate"]["if"])
        self.assertIn("NIGHTLY_CHECKS_ENABLED", new_q["jobs"]["check-2"]["if"])
        for name in set(original_r["jobs"]) - {"scope"}:
            self.assertEqual(new_r["jobs"][name], original_r["jobs"][name])
        self.assertEqual(new_r["on"], original_r["on"])
        self.assertEqual(new_r["jobs"]["checks"]["with"]["force-full"], "true")

    def test_unknown_graph_command_and_execution_overrides_fail_without_writes(self):
        mutations = [
            (migration.QUALITY, lambda x: x["jobs"].pop("scope")),
            (migration.QUALITY, lambda x: x["jobs"].update({"surprise": {"runs-on": "ubuntu-latest"}})),
            (migration.QUALITY, lambda x: x["jobs"]["check-0"].update({"if": "${{ true }}"})),
            (migration.QUALITY, lambda x: x["jobs"]["scope"]["steps"][-1].update({"run": "echo run=false"})),
            (migration.QUALITY, lambda x: x["jobs"]["scope"]["steps"][1]["with"].update({"fetch-depth": "1"})),
            (migration.QUALITY, lambda x: x["jobs"]["gate"]["steps"][-1].update({"run": "true"})),
            (migration.QUALITY, lambda x: x["jobs"]["check-0"].update({"continue-on-error": "true"})),
            (migration.QUALITY, lambda x: x.update({"defaults": {"run": {"shell": "python"}}})),
            (migration.RELEASE, lambda x: x["jobs"]["checks"]["with"].update({"force-full": "false"})),
            (migration.RELEASE, lambda x: x["jobs"]["build"].update({"needs": []})),
            (migration.RELEASE, lambda x: x["jobs"]["checks"].update({"if": "${{ always() }}"})),
        ]
        originals = {name: (self.root / name).read_text() for name in (migration.QUALITY, migration.RELEASE)}
        for name, callback in mutations:
            with self.subTest(file=name, mutation=callback):
                for path, content in originals.items():
                    self.write(path, content)
                self.mutate(name, callback)
                before = {p: (self.root / p).read_bytes() for p in originals}
                with self.assertRaises((ValueError, KeyError)):
                    self.render()
                self.assertEqual(before, {p: (self.root / p).read_bytes() for p in originals})

    def test_unknown_classifier_and_missing_policy_are_not_silently_accepted(self):
        source = (self.root / migration.CLASSIFIER).read_text()
        self.write(migration.CLASSIFIER, source + "\n# Local customization\n")
        with self.assertRaisesRegex(ValueError, "classifier revision"):
            self.render()
        self.write(migration.CLASSIFIER, source)
        self.policy.pop("change_scope")
        self.save_policy()
        with self.assertRaisesRegex(ValueError, "reviewed change_scope"):
            self.render()

    def test_duplicate_keys_symlinks_and_edited_aliases_fail(self):
        original = (self.root / migration.QUALITY).read_text()
        self.write(migration.QUALITY, original.replace("name: Quality gate", "name: Quality gate\nname: Duplicate", 1))
        with self.assertRaises(ValueError):
            self.render()
        self.write(migration.QUALITY, original)
        path = self.root / migration.CLASSIFIER
        content = path.read_text()
        path.unlink()
        target = self.root / "external.py"
        target.write_text(content)
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.render()
        path.unlink()
        path.write_text(content)
        changed = original.replace("FORCE_FULL: 'true'", "FORCE_FULL: &force 'true'", 1)
        changed += "alias_test: *force\n"
        self.write(migration.QUALITY, changed)
        with self.assertRaisesRegex(ValueError, "aliased"):
            self.render()

    def test_real_git_doc_source_manual_and_schedule_classification(self):
        repo = self.root / "git-fixture"
        repo.mkdir()

        def git(*args):
            return subprocess.check_output([
                "git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                "-C", str(repo), *args], stderr=subprocess.PIPE, text=True, timeout=10).strip()

        def commit():
            git("add", "--all")
            git("commit", "-m", "Fixture")
            return git("rev-parse", "HEAD")

        git("init", "--initial-branch=main")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (repo / "README.md").write_text("Original\n")
        base = commit()
        (repo / "README.md").write_text("Documentation\n")
        head = commit()
        for event, payload in [
            ("push", {"before": base, "after": head}),
            ("pull_request", {"pull_request": {"base": {"sha": base}, "head": {"sha": head}}}),
            ("merge_group", {"merge_group": {"base_sha": base, "head_sha": head}}),
        ]:
            self.assertFalse(change_scope.classify(repo, event, payload)["run"])
            self.assertTrue(change_scope.classify(repo, event, payload, force=True)["run"])
        for event in ("schedule", "workflow_dispatch", "workflow_call"):
            self.assertTrue(change_scope.classify(repo, event, {})["run"])
        (repo / "app.py").write_text("print('source')\n")
        head = commit()
        self.assertTrue(change_scope.classify(repo, "push", {"before": base, "after": head})["run"])


if __name__ == "__main__":
    unittest.main()
