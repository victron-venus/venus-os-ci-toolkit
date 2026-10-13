"""Generated consumers retain mandatory provenance semantics without the toolkit tree."""

import copy
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("provenance_installer", SCRIPTS / "install_release.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class ProvenanceGeneratorTests(unittest.TestCase):
    def test_rendered_required_pair_is_enforced_and_suites_are_self_contained(self):
        policy = {"repository": "example/project", "mode": "release", "sign_releases": True,
                  "validation_workflows": ["ci.yml"], "container_assets": {"container.oci.tar": "ghcr.io/example/project"},
                  "detached_provenance": [{"image": "container.oci.tar", "sidecar": "container.provenance.json",
                                           "platforms": ["linux/amd64", "linux/arm64"]}],
                  "versioning": {"schema": 1, "promotion": "final-build",
                                 "files": [{"path": "version", "format": "text", "value": "package"}],
                                 "artifacts": [{"path": "container.oci.tar", "format": "oci",
                                                "field": ["config", "Labels", "org.opencontainers.image.version"], "value": "package"}]}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            adapter = root / ".github/workflows/release-build.yml"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("on: {workflow_call: {}}\njobs: {}\n")
            files = installer.release_files(root, policy)
            for name, content in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            self.assertIn("REQUIRED_PAIRS = ({", files["scripts/release_provenance.py"])
            for name in ("version_receipt", "stage_release_assets"):
                self.assertIn("DETACHED_PROVENANCE_REQUIRED = True", files[f"scripts/{name}.py"])
            self.assertIn(".github/release-tests/test_release_provenance.py", files)
            removed = copy.deepcopy(policy)
            removed.pop("detached_provenance")
            for supplied, success in ((policy, True), (removed, False)):
                code = "import sys; sys.path.insert(0, 'scripts'); import release_provenance as p; p.policy_pairs(" + repr(supplied) + ")"
                checked = subprocess.run([sys.executable, "-I", "-c", code], cwd=root, capture_output=True, text=True)
                self.assertEqual(checked.returncode == 0, success, checked.stderr)
            checked = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", ".github/release-tests",
                                      "-p", "test_release_provenance.py", "-v"], cwd=root, capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
            self.assertIn("test_semantics_reject_replacement_after_inventory_hashing", checked.stderr)
            self.assertIn("test_policy_cannot_remove_or_change_embedded_requirement", checked.stderr)
            self.assertNotIn("NO TESTS RAN", checked.stderr)

    def test_invalid_policy_is_rejected_at_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            policy = {"repository": "example/project", "detached_provenance": [{"image": "bad"}]}
            with self.assertRaises(ValueError):
                installer.validate_policy(Path(temporary), policy)


if __name__ == "__main__":
    unittest.main()
