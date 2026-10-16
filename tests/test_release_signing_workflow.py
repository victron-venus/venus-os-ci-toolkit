"""The optional signer cannot bypass quality checks or publish an unsigned candidate."""

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_release
import release_control
import release_signing
import release_signing_workflow


class SigningWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.policy = {"repository": "example/project", "mode": "release", "sign_releases": True,
                       "versioning": {"schema": 1}, "validation_workflows": ["ci.yml"]}

    def test_opt_in_is_strict_and_requires_versioned_public_artifacts(self):
        self.assertFalse(release_signing_workflow.enabled({}))
        for update in ({"sign_releases": "true"}, {"versioning": None},
                       {"mode": "validation-only"}, {"visibility": "private"},
                       {"artifact_pattern": "other-*"}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                release_signing_workflow.enabled({**self.policy, **update})

    def test_signer_is_hosted_and_waits_for_validation_and_build(self):
        jobs = install_release.release(self.policy)["jobs"]
        self.assertEqual(jobs["sign"]["needs"], ["prepare", "checks", "build"])
        self.assertEqual(jobs["sign"]["runs-on"], "ubuntu-latest")
        self.assertEqual(jobs["sign"]["permissions"]["id-token"], "write")
        self.assertNotIn("id-token", jobs["build"]["permissions"])
        self.assertIn("sign", jobs["gate"]["needs"])
        self.assertIn("github.repository == 'example/project'", jobs["sign"]["if"])
        for name in ("sign", "candidate", "stable", "final"):
            steps = jobs[name]["steps"]
            checkout = next(s for s in steps if s.get("with", {}).get("repository") == "sigstore/cosign")
            self.assertEqual(checkout["with"]["ref"], release_signing_workflow.COSIGN_SOURCE)
            self.assertFalse(checkout["with"]["persist-credentials"])
            builder = next(s for s in steps if s.get("name") == "Build the reviewed patched release signer")
            self.assertIn('GODEBUG=fips140=on', builder["run"])
            self.assertFalse(any("cosign-installer" in s.get("uses", "") for s in steps))

    def test_generated_collection_executes_and_excludes_previous_signature_artifact(self):
        steps = install_release.release(self.policy)["jobs"]["sign"]["steps"]
        command = next(step["run"] for step in steps if step.get("name") == "Collect original build assets")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            shutil.copyfile(install_release.ROOT / "scripts/release.py", root / "scripts/release.py")
            (root / ".release-policy.json").write_text(json.dumps(self.policy))
            original = root / ".release-download/release-assets-linux"
            original.mkdir(parents=True)
            (original / "application.tar.gz").write_bytes(b"original reviewed build")
            previous = root / ".release-download/release-assets-signature"
            previous.mkdir()
            (previous / "release-signatures.json").write_text("stale retry")
            subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], cwd=root, check=True)
            assets = root / ".release-assets"
            self.assertEqual([path.name for path in assets.iterdir()], ["application.tar.gz"])
            self.assertEqual((assets / "application.tar.gz").read_bytes(), b"original reviewed build")

    def test_failed_or_skipped_signature_blocks_gate_except_version_required(self):
        script = install_release.release(self.policy)["jobs"]["gate"]["steps"][0]["run"]
        code = script.split("\n", 1)[1].rsplit("\nPY", 1)[0]
        results = {name: {"result": "success"} for name in ("prepare", "checks", "build", "sign")}
        with patch.dict(os.environ, {"RESULTS": json.dumps(results)}):
            exec(compile(code, "generated-gate", "exec"), {})
        for status in ("failure", "skipped", "cancelled"):
            broken = copy.deepcopy(results)
            broken["sign"]["result"] = status
            with patch.dict(os.environ, {"RESULTS": json.dumps(broken)}), self.assertRaises(SystemExit):
                exec(compile(code, "generated-gate", "exec"), {})
        results["prepare"]["outputs"] = {"status": "version-required"}
        results["build"]["result"] = results["sign"]["result"] = "skipped"
        with patch.dict(os.environ, {"RESULTS": json.dumps(results)}):
            exec(compile(code, "generated-gate", "exec"), {})

    def test_unsigned_legacy_behavior_is_unchanged(self):
        del self.policy["sign_releases"]
        self.assertNotIn("sign", install_release.release(self.policy)["jobs"])

    def test_publisher_refuses_invalid_signature_before_api_writes(self):
        class NoWrites:
            repo = "example/project"
            def api(self, *_args, **_kwargs):
                raise AssertionError("API must not be called")
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            Path(".release-policy.json").write_text(json.dumps(self.policy))
            with patch.object(release_control, "RELEASE_SIGNATURES_REQUIRED", True), \
                 patch.object(release_signing, "verify", side_effect=ValueError("bad signature")), \
                 self.assertRaisesRegex(ValueError, "bad signature"):
                release_control._publish_prepared(NoWrites(), "v1.2.3", "a" * 40, Path(directory), False, "notes")


if __name__ == "__main__":
    unittest.main()
