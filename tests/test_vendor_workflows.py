"""Prove local reuse preserves immutable sources and refuses destructive drift."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import vendor_workflows as vendor


class VendorWorkflowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.source, self.consumer = root / "source", root / "consumer"
        for directory in (self.source, self.consumer):
            (directory / ".github/workflows").mkdir(parents=True)
        self.git("init", "--quiet")
        self.write_source("child.yml", "on: {workflow_call: {}}\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: victron-venus/venus-os-ci-toolkit/actions/setup-python@" + "a" * 40 + "\n")
        self.write_source("parent.yml", "on: {workflow_call: {}, push: {}}\njobs:\n  nested:\n    uses: ./.github/workflows/child.yml\n")
        self.revision = self.commit()
        self.caller = self.consumer / ".github/workflows/ci.yml"
        self.caller.write_text("on: push\njobs:\n  ci:\n    uses: " + vendor.UPSTREAM + "parent.yml@" + self.revision + "\n")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.source), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def write_source(self, name, text):
        (self.source / vendor.WORKFLOWS / name).write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                 "commit", "--quiet", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def apply(self):
        files, stale = vendor.render(self.consumer, self.source)
        for name, text in files.items():
            (self.consumer / name).write_text(text)
        for name in stale:
            (self.consumer / name).unlink()
        return files

    def test_complete_closure_and_idempotence(self):
        first = self.apply()
        lock = json.loads(first[vendor.LOCK])
        self.assertEqual(len(lock["files"]), 2)
        parent = first[vendor.WORKFLOWS + vendor.local_name("parent.yml", self.revision)]
        parsed = vendor.yaml_editor().load(parent)
        self.assertEqual(set(parsed["on"]), {"workflow_call"})
        self.assertEqual(parsed["jobs"]["nested"]["uses"], "./" + vendor.WORKFLOWS + vendor.local_name("child.yml", self.revision))
        child = first[vendor.WORKFLOWS + vendor.local_name("child.yml", self.revision)]
        self.assertIn("actions/setup-python@" + "a" * 40, child)
        for name, metadata in lock["files"].items():
            self.assertEqual(metadata["sha256"], hashlib.sha256(first[name].encode()).hexdigest())
        second, stale = vendor.render(self.consumer, self.source)
        self.assertFalse(stale)
        for name, text in second.items():
            self.assertEqual((self.consumer / name).read_text(), text)

    def test_same_stem_extensions_cannot_silently_replace_a_dependency(self):
        self.write_source("child.yaml", "on: {workflow_call: {}}\njobs: {}\n")
        self.write_source("parent.yml", "on: {workflow_call: {}}\njobs:\n  first:\n    uses: ./.github/workflows/child.yml\n  second:\n    uses: ./.github/workflows/child.yaml\n")
        revision = self.commit()
        self.caller.write_text(self.caller.read_text().replace(self.revision, revision))
        before = self.caller.read_text()
        with self.assertRaisesRegex(ValueError, "collision"):
            vendor.render(self.consumer, self.source)
        self.assertEqual(self.caller.read_text(), before)
        self.assertEqual(len(list(self.caller.parent.iterdir())), 1)

    def test_case_only_source_names_fail_before_reading_second_source(self):
        bundle = vendor.WorkflowBundle(self.source)
        bundle.add("child.yml", self.revision)
        with self.assertRaisesRegex(ValueError, "collision"):
            bundle.add("CHILD.yml", self.revision)

    def test_refuses_generated_edits(self):
        self.apply()
        path = self.consumer / vendor.WORKFLOWS / vendor.local_name("child.yml", self.revision)
        path.write_text(path.read_text() + "# manually changed\n")
        with self.assertRaisesRegex(ValueError, "drift"):
            vendor.render(self.consumer, self.source)

    def test_refuses_unowned_destination(self):
        path = self.consumer / vendor.WORKFLOWS / vendor.local_name("parent.yml", self.revision)
        path.write_text("on: workflow_dispatch\njobs: {}\n")
        with self.assertRaisesRegex(ValueError, "unowned"):
            vendor.render(self.consumer, self.source)

    def test_refuses_mutable_pin(self):
        self.caller.write_text(self.caller.read_text().replace(self.revision, "main"))
        with self.assertRaisesRegex(ValueError, "immutable"):
            vendor.render(self.consumer, self.source)

    def test_cycle_fails_without_mutation(self):
        self.write_source("child.yml", "on: {workflow_call: {}}\njobs:\n  nested:\n    uses: ./.github/workflows/parent.yml\n")
        revision = self.commit()
        self.caller.write_text(self.caller.read_text().replace(self.revision, revision))
        before = self.caller.read_text()
        with self.assertRaisesRegex(ValueError, "Cyclic"):
            vendor.render(self.consumer, self.source)
        self.assertEqual(self.caller.read_text(), before)

    def test_retired_calls_only_remove_owned_files(self):
        first = self.apply()
        self.caller.write_text("on: push\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: [{run: true}]\n")
        files, stale = vendor.render(self.consumer, self.source)
        self.assertEqual(stale, set(json.loads(first[vendor.LOCK])["files"]))
        self.assertEqual(json.loads(files[vendor.LOCK])["files"], {})


    def local_bundle(self):
        """Caller that references a generated bundle carrying a reviewed local patch."""
        patched = vendor.WORKFLOWS + "toolkit-local-patch-" + self.revision + ".yml"
        (self.consumer / patched).write_text("on: workflow_call\n# reviewed local correction\njobs: {}\n")
        caller = self.consumer / ".github/workflows/local.yml"
        caller.write_text("on: pull_request\njobs:\n  local:\n    uses: ./" + patched + "\n")
        return caller, patched

    def declare_local(self, job_id, patched, caller=".github/workflows/local.yml"):
        lock = json.loads((self.consumer / vendor.LOCK).read_text())
        lock["local"] = {caller: {job_id: patched}}
        (self.consumer / vendor.LOCK).write_text(json.dumps(lock))
        return lock

    def test_declared_local_bundle_is_left_untouched(self):
        self.apply()
        _, patched = self.local_bundle()
        declared = self.declare_local("local", patched)
        before = (self.consumer / patched).read_text()
        files, stale = vendor.render(self.consumer, self.source)
        self.assertEqual((self.consumer / patched).read_text(), before)
        self.assertEqual(stale, set())
        written = json.loads(files[vendor.LOCK])
        self.assertEqual(written["local"], declared["local"])
        self.assertNotIn(patched, written["files"])
        self.assertNotIn(".github/workflows/local.yml", written["calls"])

    def test_undeclared_local_bundle_still_fails(self):
        self.apply()
        self.local_bundle()
        with self.assertRaisesRegex(ValueError, "Untracked generated workflow reference"):
            vendor.render(self.consumer, self.source)

    def test_local_bundle_cannot_also_be_generated(self):
        self.apply()
        generated = vendor.WORKFLOWS + vendor.local_name("parent.yml", self.revision)
        self.declare_local("ci", generated, caller=".github/workflows/ci.yml")
        with self.assertRaisesRegex(ValueError, "cannot also be locally patched"):
            vendor.render(self.consumer, self.source)

    def test_local_bundle_must_exist(self):
        self.apply()
        self.declare_local("local", vendor.WORKFLOWS + "toolkit-missing-" + self.revision + ".yml")
        with self.assertRaisesRegex(ValueError, "Missing locally patched bundle"):
            vendor.render(self.consumer, self.source)


if __name__ == "__main__":
    unittest.main()
