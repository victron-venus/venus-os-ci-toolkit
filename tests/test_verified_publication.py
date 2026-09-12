"""Offline contracts for stable assets, registry publication and image identity."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
# Import the checkout's scripts after explicitly adding their location.
# pylint: disable=wrong-import-position
import publish_verified as publisher
import verified_images
from release_control import ReleaseError
from test_release_control import SHA, FakeGitHub, policy, policy_snapshot, rc

# pylint: enable=wrong-import-position


class StableAssetVerificationTests(unittest.TestCase):
    """Verify registry admission against the original candidate source policy."""

    def github(self):
        """Return a published stable release backed by valid immutable RC evidence."""
        gh = FakeGitHub()
        gh.releases[10].update(tag_name="v1.2.3", prerelease=False)
        gh.refs["v1.2.3"] = {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "commit", "sha": SHA},
        }
        return gh

    def verify(self, gh):
        """Download and verify stable assets inside a disposable local directory."""
        with tempfile.TemporaryDirectory() as temp:
            return publisher.verified_assets(gh, "v1.2.3", Path(temp))

    def test_stable_assets_verify_policy_at_original_source_commit(self):
        """Stable assets verify policy at original source commit."""
        gh = self.github()
        result = self.verify(gh)
        self.assertEqual(result["source_policy"], policy_snapshot())
        self.assertEqual(gh.policy_reads, [SHA])
        self.assertEqual(gh.writes, [])

    def test_claimed_clean_snapshot_cannot_qualify_blocked_source(self):
        """Claimed clean snapshot cannot qualify blocked source."""
        for field in ("release_blockers", "stable_blockers"):
            with self.subTest(field=field):
                gh = self.github()
                gh.source_policies[SHA][field] = ["Missing required integration checks"]
                with self.assertRaisesRegex(ReleaseError, field):
                    self.verify(gh)
                self.assertEqual(gh.policy_reads, [SHA])
                self.assertEqual(gh.writes, [])

    def test_clean_snapshot_must_still_match_original_policy(self):
        """Clean snapshot must still match original policy."""
        gh = self.github()
        original = {**policy(), "validation_workflows": ["other-ci.yml"]}
        gh.source_policies[SHA] = original
        with self.assertRaisesRegex(ReleaseError, "policy snapshot differs"):
            self.verify(gh)

    def test_original_policy_must_declare_release_mode(self):
        """Original policy must declare release mode."""
        gh = self.github()
        gh.source_policies[SHA]["mode"] = "validation-only"
        with self.assertRaisesRegex(ReleaseError, "mode must be release"):
            self.verify(gh)

    def test_legacy_manifest_without_source_policy_is_rejected(self):
        """Legacy manifest without source policy is rejected."""
        gh = self.github()
        data = json.loads(gh.files[21])
        data.pop("source_policy")
        gh.files[21] = rc.json_bytes(data)
        gh.set_evidence(gh.files[21])
        with self.assertRaisesRegex(ReleaseError, "policy snapshot"):
            self.verify(gh)

    def test_rc_source_run_must_be_manual_and_have_exact_identity(self):
        """Rc source run must be manual and have exact identity."""
        for change in ({"event": "push"}, {"event": "schedule"}, {"id": 18}):
            with self.subTest(change=change):
                gh = self.github()
                gh.runs[17].update(change)
                with self.assertRaises(ReleaseError):
                    self.verify(gh)
                self.assertEqual(gh.writes, [])


class RegistryPublicationTests(unittest.TestCase):
    """Check registry destinations, unchanged bytes and digest-bound deployment."""

    def setUp(self):
        self.policy = {
            "repository": "owner/repo",
            "container_assets": {"image.oci.tar": "ghcr.io/owner/image"},
        }

    @staticmethod
    def assets(_gh, _tag, directory):
        """Provide approved local payloads and their original publication mappings."""
        (directory / "image.oci.tar").write_bytes(b"approved image")
        return {
            "version": "1.2.3",
            "tag": "v1.2.3-rc.1",
            "source_sha": "a" * 40,
            "source_policy": {
                "data": {
                    "container_assets": {"image.oci.tar": "ghcr.io/owner/image"},
                    "pypi_assets": ["*.whl"],
                }
            },
        }

    def test_publication_rejects_current_mapping_drift_before_registry_access(self):
        """Publication rejects current mapping drift before registry access."""
        cases = (
            (
                "containers",
                "container_assets",
                {"image.oci.tar": "ghcr.io/owner/other"},
            ),
            ("pypi", "pypi_assets", ["*"]),
        )
        for target, field, changed in cases:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                current = {**self.policy, field: changed}
                (root / ".release-policy.json").write_text(json.dumps(current))
                with (
                    mock.patch.object(publisher, "ROOT", root),
                    mock.patch.object(
                        publisher, "verified_assets", side_effect=self.assets
                    ),
                    mock.patch.object(publisher.subprocess, "run") as run,
                    mock.patch.object(
                        sys,
                        "argv",
                        ["publish_verified.py", target, "--tag", "v1.2.3", "--execute"],
                    ),
                ):
                    self.assertEqual(publisher.main(), 1)
                    run.assert_not_called()

    def test_deployment_rejects_current_image_mapping_drift(self):
        """Deployment rejects current image mapping drift."""
        current = {
            **self.policy,
            "container_assets": {"image.oci.tar": "ghcr.io/owner/other"},
        }
        with tempfile.TemporaryDirectory() as temp:
            with (
                mock.patch.object(
                    verified_images, "verified_assets", side_effect=self.assets
                ),
                mock.patch.object(verified_images.subprocess, "run") as run,
                self.assertRaisesRegex(ReleaseError, "container_assets differs"),
            ):
                verified_images.resolve(current, "v1.2.3", Path(temp))
            run.assert_not_called()

    def test_latest_uses_same_private_approved_archive(self):
        """Latest uses same private approved archive."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text(json.dumps(self.policy))
            with (
                mock.patch.object(publisher, "ROOT", root),
                mock.patch.object(
                    publisher, "verified_assets", side_effect=self.assets
                ),
                mock.patch.object(
                    publisher.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, '{"Tags":[]}', ""),
                ) as run,
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "publish_verified.py",
                        "containers",
                        "--tag",
                        "v1.2.3",
                        "--execute",
                        "--latest",
                    ],
                ),
            ):
                self.assertEqual(publisher.main(), 0)
            copies = [
                call.args[0] for call in run.call_args_list if call.args[0][1] == "copy"
            ]
            self.assertEqual(len(copies), 2)
            self.assertTrue(copies[0][-2].startswith("oci-archive:"))
            self.assertEqual(copies[0][-2], copies[1][-2])
            self.assertTrue(copies[1][-1].endswith(":latest"))

    def test_existing_registry_version_is_not_overwritten(self):
        """Existing registry version is not overwritten."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".release-policy.json").write_text(json.dumps(self.policy))
            with (
                mock.patch.object(publisher, "ROOT", root),
                mock.patch.object(
                    publisher, "verified_assets", side_effect=self.assets
                ),
                mock.patch.object(
                    publisher.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], 0, '{"Tags":["1.2.3"]}', ""
                    ),
                ) as run,
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "publish_verified.py",
                        "containers",
                        "--tag",
                        "v1.2.3",
                        "--execute",
                    ],
                ),
            ):
                self.assertEqual(publisher.main(), 1)
            self.assertEqual(run.call_count, 1)

    def test_deployment_uses_digest_and_rejects_registry_retag(self):
        """Deployment uses digest and rejects registry retag."""
        with tempfile.TemporaryDirectory() as temp:
            with (
                mock.patch.object(
                    verified_images, "verified_assets", side_effect=self.assets
                ),
                mock.patch.object(
                    verified_images.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, b"approved manifest"),
                        subprocess.CompletedProcess([], 0, b"approved manifest"),
                    ],
                ),
            ):
                result = verified_images.resolve(self.policy, "v1.2.3", Path(temp))
                self.assertIn("@sha256:", result["images"]["ghcr.io/owner/image"])
            with (
                mock.patch.object(
                    verified_images, "verified_assets", side_effect=self.assets
                ),
                mock.patch.object(
                    verified_images.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, b"approved manifest"),
                        subprocess.CompletedProcess([], 0, b"tampered manifest"),
                    ],
                ),
                self.assertRaises(ReleaseError),
            ):
                verified_images.resolve(self.policy, "v1.2.3", Path(temp))


if __name__ == "__main__":
    unittest.main()
