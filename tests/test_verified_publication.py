"""Offline contracts for stable assets, registry publication and image identity."""

import base64
import io
import json
import os
import shutil
# Only mocked publication calls and subprocess exception fixtures are used here.
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
# Import the checkout's scripts after explicitly adding their location.
# pylint: disable=wrong-import-position
import publish_verified as publisher
import verified_images
from release_control import ReleaseError
from test_release_control import SHA, FakeGitHub, manifest as rc_manifest, policy, policy_snapshot, rc

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

    def test_oversized_download_stops_before_registry_publication(self):
        """Trusted manifest bytes bound the transfer, not only its final digest."""
        gh = self.github()

        def oversize(path, output):
            output.write(gh.binary(path) + b"extra")

        with mock.patch.object(gh, "download_asset", side_effect=oversize):
            with self.assertRaisesRegex(ReleaseError, "exceeds expected size"):
                self.verify(gh)
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

    def test_legacy_stable_must_match_authenticated_parent_before_download(self):
        """A legacy stable must match its authenticated RC parent before payload reads."""
        gh = self.github()
        manifest = rc_manifest()
        manifest.update(
            channel="stable",
            tag="v1.2.3",
            derived_from_rc={
                "tag": "v1.2.3-rc.1",
                "manifest_sha256": "b" * 64,
                "source_sha": SHA,
                "run_id": 16,
            },
        )
        raw = rc.json_bytes(manifest)
        release = {"id": 10, "draft": False, "prerelease": False}
        ref = {"object": {"type": "commit", "sha": SHA}}
        assets = [
            {"id": 20, "name": "package.tar.gz"},
            {"id": 21, "name": rc.MANIFEST},
        ]
        with (
            mock.patch.object(
                publisher, "_read_stable_release",
                return_value=(release, ref, assets, raw, {"tag": "v1.2.3"}),
            ),
            mock.patch.object(publisher, "_read_versioned_stable_release", return_value=None),
            mock.patch.object(publisher, "validate_manifest", return_value=manifest),
            mock.patch.object(publisher, "verify_evidence"),
            mock.patch.object(
                publisher, "download_asset",
            ) as download,
            mock.patch("release_versioned.verified_rc", return_value=({}, {
                **manifest["derived_from_rc"], "manifest_sha256": "c" * 64,
            })),
            self.assertRaisesRegex(ReleaseError, "Final package RC provenance mismatch"),
        ):
            with tempfile.TemporaryDirectory() as temp:
                publisher.verified_assets(gh, "v1.2.3", Path(temp))
        download.assert_not_called()
        self.assertEqual(gh.writes, [])

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
        # Publication now preflights registry logins before verification.
        self.docker = tempfile.TemporaryDirectory()
        self.addCleanup(self.docker.cleanup)
        self.write_docker_config(
            Path(self.docker.name),
            {"auths": {"ghcr.io": {"auth": "b3duZXI6c2VjcmV0"}}},
        )
        environment = mock.patch.dict(
            os.environ, {"DOCKER_CONFIG": self.docker.name}
        )
        environment.start()
        self.addCleanup(environment.stop)
        # The patch restores any value the running shell exports.
        os.environ.pop("REGISTRY_AUTH_FILE", None)

    @staticmethod
    def write_docker_config(directory, data):
        """Write one docker client configuration for credential preflight tests."""
        (directory / "config.json").write_text(json.dumps(data))

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

    def test_each_target_writes_only_with_execute(self):
        """A dry run may read registry inventory but never copy or upload payloads."""
        for target in ("containers", "pypi"):
            for execute in (False, True):
                with (
                    self.subTest(target=target, execute=execute),
                    tempfile.TemporaryDirectory() as temp,
                ):
                    root = Path(temp)
                    current = {**self.policy, "pypi_assets": ["*.whl"]}
                    (root / ".release-policy.json").write_text(json.dumps(current))

                    def assets(gh, tag, directory):
                        manifest = self.assets(gh, tag, directory)
                        (directory / "approved.whl").write_bytes(b"approved wheel")
                        (directory / "unselected.tar.gz").write_bytes(b"source archive")
                        return manifest

                    argv = ["publish_verified.py", target, "--tag", "v1.2.3"]
                    if execute:
                        argv.append("--execute")
                    with (
                        mock.patch.object(publisher, "ROOT", root),
                        mock.patch.object(
                            publisher, "verified_assets", side_effect=assets
                        ),
                        mock.patch.object(
                            publisher.subprocess,
                            "run",
                            return_value=subprocess.CompletedProcess(
                                [], 0, '{"Tags":[]}', ""
                            ),
                        ) as run,
                        mock.patch.object(sys, "argv", argv),
                        mock.patch.object(
                            sys, "stdout", new_callable=io.StringIO
                        ) as out,
                    ):
                        self.assertEqual(publisher.main(), 0)
                    plan = json.loads(out.getvalue())
                    self.assertEqual(plan["execute"], execute)
                    issued = [call.args[0] for call in run.call_args_list]
                    if target == "containers":
                        self.assertEqual(issued.pop(0)[1], "list-tags")
                    else:
                        self.assertEqual(plan["commands"][0][3], "check")
                        self.assertEqual(plan["commands"][1][3], "upload")
                        self.assertEqual(len(plan["commands"][0]), 5)
                        self.assertTrue(
                            plan["commands"][0][-1].endswith("approved.whl")
                        )
                    self.assertEqual(issued, plan["commands"] if execute else [])

    def test_missing_registry_accepts_skopeo_rendered_diagnostic(self):
        """A new GHCR image can be published with current Skopeo diagnostics."""
        for error in (
            'time="2026-10-10T14:18:00-07:00" level=fatal msg="Error listing '
            'repository tags: fetching tags list: name unknown"\n',
            "NAME_UNKNOWN: repository name not known to registry\n",
            "NAME_UNKNOWN\n",
            "repository does not exist\n",
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(
                    publisher.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1, "", error),
                ),
            ):
                publisher.require_unpublished_version("ghcr.io/owner/new", "1.2.3")

    def test_registry_failures_do_not_authorize_publication(self):
        """Access, transport and ambiguous absence errors still fail closed."""
        for error in (
            "unauthorized: authentication required",
            "denied: permission denied",
            "repository does not exist or may require docker login",
            "NAME_UNKNOWN: authentication required",
            "name unknown: access denied",
            "certificate signed by unknown authority",
            "dial tcp: connection refused",
            "MANIFEST_UNKNOWN: manifest unknown",
            "unexpected EOF",
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(
                    publisher.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 1, "", error),
                ),
                self.assertRaisesRegex(
                    ReleaseError, "Cannot establish registry tag inventory"
                ),
            ):
                publisher.require_unpublished_version("ghcr.io/owner/new", "1.2.3")

    def test_all_versions_precede_latest_from_the_same_approved_archives(self):
        """Multiple images are preflighted before any version or latest write."""
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            manifest = self.assets(None, None, directory)
            (directory / "second.oci.tar").write_bytes(b"second approved image")
            mapping = {
                **self.policy["container_assets"],
                "second.oci.tar": "ghcr.io/owner/second",
            }
            current = {**self.policy, "container_assets": mapping}
            manifest["source_policy"]["data"]["container_assets"] = mapping
            with mock.patch.object(
                publisher.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, '{"Tags":[]}', ""),
            ) as run:
                commands = publisher.container_commands(
                    current, manifest, directory, latest=True
                )
            self.assertEqual(
                [call.args[0][1] for call in run.call_args_list], ["list-tags"] * 2
            )
            self.assertEqual(len(commands), 4)
            for index in range(2):
                version, latest = commands[index], commands[index + 2]
                self.assertEqual(version[:-1], latest[:-1])
                self.assertTrue(version[-1].endswith(":1.2.3"))
                self.assertTrue(latest[-1].endswith(":latest"))
            with (
                mock.patch.object(
                    publisher.subprocess,
                    "run",
                    side_effect=[
                        subprocess.CompletedProcess([], 0, '{"Tags":[]}', ""),
                        subprocess.CompletedProcess([], 1, "", "access denied"),
                    ],
                ) as run,
                self.assertRaisesRegex(
                    ReleaseError, "Cannot establish registry tag inventory"
                ),
            ):
                publisher.container_commands(current, manifest, directory, latest=True)
            self.assertEqual(
                [call.args[0][1] for call in run.call_args_list], ["list-tags"] * 2
            )

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


class VerifiedImageOutputTests(unittest.TestCase):
    """Selected output roots confine destinations without clobbering aliases."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        (self.checkout / ".release-policy.json").write_text("{}")
        self.destination = self.root / "separate-deployment-inputs"
        self.destination.mkdir()
        self.output = self.destination / "images.json"
        self.original = self.root / "original.json"
        self.original.write_bytes(b"outside sentinel\n")
        self.result = {
            "repository": "example/product",
            "tag": "v1.2.3",
            "source_sha": SHA,
            "images": {
                "ghcr.io/example/image": "ghcr.io/example/image@sha256:" + "a" * 64
            },
        }

    def run_main(self, output=None, output_root=None, resolver=None):
        argv = [
            "verified_images.py",
            "--tag",
            "v1.2.3",
            "--output",
            str(output or self.output),
        ]
        if output_root is not None:
            argv.extend(["--output-root", str(output_root)])
        with (
            mock.patch.object(verified_images, "ROOT", self.checkout),
            mock.patch.object(Path, "cwd", return_value=self.checkout),
            mock.patch.object(
                verified_images,
                "resolve",
                return_value=self.result,
                side_effect=resolver,
            ) as resolution,
            mock.patch.object(sys, "argv", argv),
            mock.patch("sys.stdout", new_callable=io.StringIO),
            mock.patch("sys.stderr", new_callable=io.StringIO),
        ):
            status = verified_images.main()
        self.resolution = resolution
        return status

    def test_explicit_external_directory_create_and_hardlink_overwrite(self):
        self.assertEqual(self.run_main(output_root=self.destination), 0)
        expected = (json.dumps(self.result, indent=2) + "\n").encode()
        self.assertEqual(self.output.read_bytes(), expected)
        self.output.unlink()
        os.link(self.original, self.output)
        self.assertEqual(self.run_main(output_root=self.destination), 0)
        self.assertEqual(self.output.read_bytes(), expected)
        self.assertEqual(self.original.read_bytes(), b"outside sentinel\n")
        self.assertNotEqual(self.output.stat().st_ino, self.original.stat().st_ino)
        self.assertEqual(list(self.destination.iterdir()), [self.output])

    def test_symlink_and_directory_output_fail_closed(self):
        self.output.symlink_to(self.original)
        self.assertEqual(self.run_main(output_root=self.destination), 1)
        self.resolution.assert_not_called()
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(self.original.read_bytes(), b"outside sentinel\n")
        self.output.unlink()
        self.output.mkdir()
        self.assertEqual(self.run_main(output_root=self.destination), 1)
        self.resolution.assert_not_called()
        self.assertTrue(self.output.is_dir())
        self.assertEqual(list(self.destination.iterdir()), [self.output])

    def test_relative_and_absolute_outputs_below_default_cwd(self):
        nested = Path("nested directory/..ordinary-name/images.json")
        (self.checkout / nested).parent.mkdir(parents=True)
        for requested in (Path("images.json"), self.checkout / "images.json", nested):
            with self.subTest(requested=requested):
                output = self.checkout / requested
                self.assertEqual(self.run_main(output=requested), 0)
                self.assertEqual(json.loads(output.read_bytes()), self.result)
                output.unlink()

    def test_parent_traversal_and_outside_absolute_paths_reject_before_network(self):
        for output in (
            Path("../outside.json"),
            Path("nested/../images.json"),
            self.output,
            self.root / "checkout-neighbor" / "images.json",
        ):
            with self.subTest(output=output):
                self.assertEqual(self.run_main(output=output), 1)
                self.resolution.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual(self.original.read_bytes(), b"outside sentinel\n")

    def test_missing_output_root_or_parent_reject_before_network(self):
        for output, root in (
            (Path("images.json"), self.root / "missing"),
            (Path("missing/images.json"), self.checkout),
            (self.checkout, self.checkout),
        ):
            with self.subTest(output=output, root=root):
                self.assertEqual(self.run_main(output, root), 1)
                self.resolution.assert_not_called()
        self.assertEqual(
            list(self.checkout.iterdir()), [self.checkout / ".release-policy.json"]
        )

    def test_symlink_destination_parent_rejects_before_network(self):
        parent = self.checkout / "linked"
        parent.symlink_to(self.destination, target_is_directory=True)
        self.assertEqual(self.run_main(parent / "images.json"), 1)
        self.resolution.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_explicit_root_can_use_a_platform_directory_alias(self):
        alias = self.root / "platform-alias"
        alias.symlink_to(self.destination, target_is_directory=True)
        for output in (alias / "images.json", self.output.resolve()):
            with self.subTest(output=output):
                self.assertEqual(self.run_main(output, alias), 0)
                self.assertEqual(json.loads(self.output.read_bytes()), self.result)

    def test_parent_replacement_cannot_redirect_the_anchored_write(self):
        parent = self.checkout / "output"
        parent.mkdir()
        anchored = self.checkout / "original-output"

        def replace_parent(*_):
            parent.rename(anchored)
            parent.symlink_to(self.destination, target_is_directory=True)
            return self.result

        self.assertEqual(
            self.run_main(parent / "images.json", resolver=replace_parent), 0
        )
        self.assertFalse(self.output.exists())
        self.assertEqual(
            json.loads((anchored / "images.json").read_bytes()), self.result
        )
        self.assertEqual(self.original.read_bytes(), b"outside sentinel\n")

    def test_write_failures_preserve_existing_output_and_close_directory(self):
        self.output.write_bytes(b"prior verified inputs\n")
        self.output.chmod(0o640)
        for operation in ("fsync", "replace"):
            with (
                self.subTest(operation=operation),
                mock.patch.object(
                    verified_images.os,
                    operation,
                    side_effect=OSError("injected failure"),
                ),
                mock.patch.object(verified_images.os, "close", wraps=os.close) as close,
            ):
                self.assertEqual(self.run_main(output_root=self.destination), 1)
            self.assertEqual(self.output.read_bytes(), b"prior verified inputs\n")
            self.assertEqual(self.output.stat().st_mode & 0o777, 0o640)
            self.assertEqual(list(self.destination.iterdir()), [self.output])
            self.assertTrue(close.called)
            for call in close.call_args_list:
                with self.assertRaises(OSError):
                    os.fstat(call.args[0])

    def test_unsupported_platform_fails_without_network_or_fallback_write(self):
        with mock.patch.object(verified_images.os, "supports_dir_fd", set()):
            self.assertEqual(self.run_main(output_root=self.destination), 1)
        self.resolution.assert_not_called()
        self.assertFalse(self.output.exists())



class RegistryCredentialTests(unittest.TestCase):
    """Pass docker login credentials to skopeo, which ignores docker credHelpers."""

    @contextmanager
    def docker_environment(self, directory, data):
        """Point the docker client configuration at a disposable directory."""
        directory = Path(directory)
        (directory / "config.json").write_text(json.dumps(data))
        with mock.patch.dict(
            os.environ,
            {
                "DOCKER_CONFIG": str(directory),
                "HOME": str(directory),
                "XDG_CONFIG_HOME": str(directory / ".config"),
                "XDG_RUNTIME_DIR": str(directory / "run"),
            },
        ):
            os.environ.pop("REGISTRY_AUTH_FILE", None)
            yield

    @staticmethod
    def skopeo_auth_file(directory, host, value):
        """Store one login where skopeo reads it before the docker config."""
        path = Path(directory) / ".config" / "containers" / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"auths": {host: value}}))
        return path

    @staticmethod
    def skopeo_auth_document(directory, document):
        """Store one complete auth document where skopeo reads it first."""
        path = Path(directory) / ".config" / "containers" / "auth.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
        return path

    @staticmethod
    def materialized(overrides):
        """Read the credential file the publisher hands to skopeo."""
        path = Path(overrides["DOCKER_CONFIG"]) / "config.json"
        return path, json.loads(path.read_text())

    @staticmethod
    def helper_result(username, secret):
        """Return a credential helper answer with the docker helper JSON shape."""

        def helper(command, **_kwargs):
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"Username": username, "Secret": secret}),
                "",
            )

        return helper

    def test_creds_store_login_is_materialized_for_skopeo(self):
        """A helper-backed docker login becomes a private skopeo auth file."""
        calls = []

        def helper(command, **kwargs):
            calls.append((command, kwargs.get("input")))
            return subprocess.CompletedProcess(
                command, 0, '{"Username":"alvit","Secret":"hunter2"}', ""
            )

        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"credsStore": "osxkeychain"}),
            mock.patch.object(publisher.subprocess, "run", side_effect=helper),
        ):
            with publisher.registry_credentials(
                ["docker.io/alvit/ottplay-foss"]
            ) as overrides:
                path, config = self.materialized(overrides)
                entry = config["auths"][publisher.DOCKER_HUB_SERVER]["auth"]
                self.assertEqual(
                    base64.b64decode(entry).decode(), "alvit:hunter2"
                )
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(Path(overrides["DOCKER_CONFIG"]).exists())
            self.assertEqual(os.environ["DOCKER_CONFIG"], config_dir)
        self.assertEqual(
            calls,
            [(["docker-credential-osxkeychain", "get"],
              publisher.DOCKER_HUB_SERVER)],
        )
        self.assertNotIn("hunter2", " ".join(calls[0][0]))

    def test_plain_docker_login_config_is_left_to_skopeo(self):
        """Credentials already in the client config need no rewrite."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(
                config_dir,
                {"auths": {"ghcr.io": {"auth": "b3duZXI6c2VjcmV0"}}},
            ),
            mock.patch.object(publisher.subprocess, "run") as run,
        ):
            with publisher.registry_credentials(
                ["ghcr.io/owner/image"]
            ) as overrides:
                self.assertEqual(overrides, {})
                self.assertEqual(os.environ["DOCKER_CONFIG"], config_dir)
        run.assert_not_called()

    def test_skopeo_login_alone_satisfies_the_preflight(self):
        """A destination logged in with skopeo must not demand a docker login."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"auths": {}}),
            mock.patch.object(publisher.subprocess, "run") as run,
        ):
            self.skopeo_auth_file(
                config_dir,
                "https://index.docker.io/v1/",
                {"auth": "b3duZXI6c2VjcmV0"},
            )
            with publisher.registry_credentials(
                ["docker.io/alvit/ottplay-foss"]
            ) as overrides:
                self.assertEqual(overrides, {})
        run.assert_not_called()

    def test_docker_namespace_login_covers_its_own_repository(self):
        """containers/image accepts a repository or namespace key for one image."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(
                config_dir,
                {"auths": {"ghcr.io/owner": {"auth": "b3duZXI6c2VjcmV0"}}},
            ),
            mock.patch.object(publisher.subprocess, "run") as run,
        ):
            with publisher.registry_credentials(
                ["ghcr.io/owner/image"]
            ) as overrides:
                self.assertEqual(overrides, {})
                self.assertEqual(os.environ["DOCKER_CONFIG"], config_dir)
        run.assert_not_called()

    def test_unrelated_namespace_login_does_not_cover_the_destination(self):
        """A credential scoped to another namespace is not a login."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(
                config_dir,
                {"auths": {"ghcr.io/other": {"auth": "b3duZXI6c2VjcmV0"}}},
            ),
            mock.patch.object(publisher.subprocess, "run") as run,
            self.assertRaisesRegex(
                ReleaseError, "No registry credentials for ghcr.io"
            ),
        ):
            with publisher.registry_credentials(["ghcr.io/owner/image"]):
                pass
        run.assert_not_called()

    def test_skopeo_auth_helper_is_accepted_without_a_docker_login(self):
        """A helper configured in skopeo's auth file resolves that login."""
        queries = []

        def helper(command, **kwargs):
            queries.append((command, kwargs.get("input")))
            return subprocess.CompletedProcess(
                command, 0, '{"Username":"alvit","Secret":"hunter2"}', ""
            )

        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"auths": {}}),
            mock.patch.object(publisher.subprocess, "run", side_effect=helper),
        ):
            self.skopeo_auth_document(
                config_dir, {"credHelpers": {"ghcr.io/owner": "pass"}}
            )
            with publisher.registry_credentials(
                ["ghcr.io/owner/image"]
            ) as overrides:
                self.assertEqual(overrides, {})
        self.assertEqual(
            queries, [(["docker-credential-pass", "get"], "ghcr.io/owner")]
        )

    def test_skopeo_helper_for_another_namespace_is_not_a_login(self):
        """A skopeo helper scoped to another namespace does not cover the image."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"auths": {}}),
            mock.patch.object(publisher.subprocess, "run") as run,
            self.assertRaisesRegex(
                ReleaseError, "No registry credentials for ghcr.io"
            ),
        ):
            self.skopeo_auth_document(
                config_dir, {"credHelpers": {"ghcr.io/other": "pass"}}
            )
            with publisher.registry_credentials(["ghcr.io/owner/image"]):
                pass
        run.assert_not_called()

    def test_skopeo_login_does_not_mask_another_missing_registry(self):
        """Every destination still has to resolve its own login."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"auths": {}}),
            mock.patch.object(publisher.subprocess, "run"),
            self.assertRaisesRegex(
                ReleaseError, "No registry credentials for ghcr.io"
            ),
        ):
            self.skopeo_auth_file(
                config_dir, "docker.io", {"auth": "b3duZXI6c2VjcmV0"}
            )
            with publisher.registry_credentials(
                ["docker.io/alvit/ottplay-foss", "ghcr.io/owner/image"]
            ):
                pass

    def test_docker_hub_helper_uses_the_canonical_server_key(self):
        """Docker keys a Hub login by registry URL, not by the image host."""
        calls = []

        def helper(command, **kwargs):
            calls.append((command, kwargs.get("input")))
            return subprocess.CompletedProcess(
                command, 0, '{"Username":"alvit","Secret":"hunter2"}', ""
            )

        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(
                config_dir,
                {
                    "credHelpers": {publisher.DOCKER_HUB_SERVER: "desktop"},
                    "credsStore": "fallback-store",
                },
            ),
            mock.patch.object(publisher.subprocess, "run", side_effect=helper),
        ):
            with publisher.registry_credentials(
                ["docker.io/alvit/ottplay-foss"]
            ) as overrides:
                _, config = self.materialized(overrides)
                entry = config["auths"][publisher.DOCKER_HUB_SERVER]["auth"]
                self.assertEqual(
                    base64.b64decode(entry).decode(), "alvit:hunter2"
                )
        self.assertEqual(
            calls,
            [(["docker-credential-desktop", "get"],
              publisher.DOCKER_HUB_SERVER)],
        )

    def test_helper_query_falls_back_to_the_image_host(self):
        """A helper keyed for the host still answers when the URL query misses."""
        queries = []

        def helper(command, **kwargs):
            queries.append(kwargs.get("input"))
            if kwargs.get("input") == publisher.DOCKER_HUB_SERVER:
                return subprocess.CompletedProcess(
                    command, 1, "", "credentials not found"
                )
            return subprocess.CompletedProcess(
                command, 0, '{"Username":"alvit","Secret":"hunter2"}', ""
            )

        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"credsStore": "pass"}),
            mock.patch.object(publisher.subprocess, "run", side_effect=helper),
        ):
            with publisher.registry_credentials(
                ["docker.io/alvit/ottplay-foss"]
            ):
                pass
        self.assertEqual(queries, [publisher.DOCKER_HUB_SERVER, "docker.io"])

    def test_identity_token_helper_result_keeps_its_token_semantics(self):
        """A '<token>' username is an OAuth refresh token, not a password."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"credsStore": "pass"}),
            mock.patch.object(
                publisher.subprocess,
                "run",
                side_effect=self.helper_result("<token>", "refresh-token"),
            ),
        ):
            with publisher.registry_credentials(
                ["ghcr.io/owner/image"]
            ) as overrides:
                _, config = self.materialized(overrides)
                entry = config["auths"]["ghcr.io"]
        # containers/image returns identitytoken only after a colon-bearing auth.
        self.assertEqual(entry["identitytoken"], "refresh-token")
        self.assertEqual(base64.b64decode(entry["auth"]), b":")

    def test_unusable_helper_results_require_a_login_before_any_download(self):
        """Broken or hung helpers report the login, not an anonymous push."""
        cases = {
            "nonzero exit": subprocess.CompletedProcess([], 1, "", "not logged in"),
            "invalid json": subprocess.CompletedProcess([], 0, "not json", ""),
            "empty secret": subprocess.CompletedProcess(
                [], 0, '{"Username":"alvit","Secret":""}', ""
            ),
            "missing helper": OSError("no credential helper"),
            "hung helper": subprocess.TimeoutExpired(
                ["docker-credential-pass", "get"],
                publisher.DOCKER_CREDENTIAL_TIMEOUT,
            ),
        }
        for name, result in cases.items():
            with (
                self.subTest(name=name),
                tempfile.TemporaryDirectory() as config_dir,
                self.docker_environment(config_dir, {"credsStore": "pass"}),
            ):
                helper = (
                    mock.patch.object(
                        publisher.subprocess, "run", side_effect=result
                    )
                    if isinstance(result, BaseException)
                    else mock.patch.object(
                        publisher.subprocess, "run", return_value=result
                    )
                )
                with (
                    helper,
                    self.assertRaisesRegex(ReleaseError, "run docker login"),
                ):
                    with publisher.registry_credentials(
                        ["ghcr.io/owner/image"]
                    ):
                        pass

    def test_invalid_helper_name_reports_the_registry_and_value(self):
        """A malformed credHelpers entry names the entry to fix."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(
                config_dir, {"credHelpers": {"ghcr.io": "not a helper"}}
            ),
            self.assertRaisesRegex(
                ReleaseError,
                "Invalid docker credential helper name 'not a helper' for ghcr.io",
            ),
        ):
            with publisher.registry_credentials(["ghcr.io/owner/image"]):
                pass

    @unittest.skipUnless(shutil.which("skopeo"), "skopeo is not installed")
    def test_skopeo_finds_the_materialized_login_itself(self):
        """Skopeo's own lookup must find the login the publisher passes it."""
        with (
            tempfile.TemporaryDirectory() as config_dir,
            self.docker_environment(config_dir, {"credsStore": "pass"}),
            mock.patch.object(
                publisher,
                "docker_helper_credentials",
                return_value=("alvit", "hunter2"),
            ),
        ):
            with publisher.registry_credentials(
                ["ghcr.io/owner/image"]
            ) as overrides:
                result = subprocess.run(  # nosec B603, B607
                    ["skopeo", "login", "--get-login", "ghcr.io"],
                    capture_output=True,
                    text=True,
                    check=False,
                    env={**os.environ, **overrides},
                )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "alvit")

    def test_missing_login_fails_before_downloading_payloads(self):
        """An absent registry login is reported before verified payload bytes."""
        with (
            tempfile.TemporaryDirectory() as temp,
            tempfile.TemporaryDirectory() as config_dir,
        ):
            root = Path(temp)
            (root / ".release-policy.json").write_text(
                json.dumps(
                    {
                        "repository": "owner/repo",
                        "container_assets": {
                            "image.oci.tar": "ghcr.io/owner/image"
                        },
                    }
                )
            )
            with (
                self.docker_environment(config_dir, {"auths": {}}),
                mock.patch.object(publisher, "ROOT", root),
                mock.patch.object(publisher, "verified_assets") as assets,
                mock.patch.object(publisher.subprocess, "run") as run,
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
                mock.patch("sys.stderr", new_callable=io.StringIO) as errors,
            ):
                self.assertEqual(publisher.main(), 1)
            assets.assert_not_called()
            run.assert_not_called()
            self.assertIn("docker login", errors.getvalue())


class CompactPublicationTests(unittest.TestCase):
    def test_registry_admission_uses_authenticated_rc_policy_and_only_shipped_files(self):
        from test_versioned_release import CompactConsumerTests
        fixture = CompactConsumerTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        with tempfile.TemporaryDirectory() as directory:
            manifest = publisher.verified_assets(fixture.gh, fixture.compact["tag"], Path(directory))
            self.assertEqual(manifest["source_policy"], fixture.manifest["source_policy"])
            self.assertEqual({path.name for path in Path(directory).iterdir()},
                             {item["name"] for item in fixture.compact["assets"]})
        self.assertEqual(fixture.gh.writes, [])

    def test_registry_admission_rejects_retained_rc_loss(self):
        from test_versioned_release import CompactConsumerTests, rc as fixture_rc
        fixture = CompactConsumerTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.gh.evidence_by_run[100][0]["expired"] = True
        with tempfile.TemporaryDirectory() as directory, self.assertRaises((ValueError, fixture_rc.ReleaseError)):
            publisher.verified_assets(fixture.gh, fixture.compact["tag"], Path(directory))
        self.assertEqual(fixture.gh.writes, [])


if __name__ == "__main__":
    unittest.main()
