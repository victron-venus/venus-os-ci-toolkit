"""Compact stable envelopes retain authenticated RC evidence and payload bytes."""
import argparse
import builtins
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import release_distribution as distribution
import test_versioned_release as versioned

rc = versioned.rc


def encoded(value):
    return json.dumps(value, sort_keys=True).encode()


def candidate():
    assets = [{"name": name, "size": len(name), "sha256": hashlib.sha256(name.encode()).hexdigest()}
              for name in ("player.zip", "player.zip.sig", "latest.json", "runtime.json",
                           "SHA256SUMS", "build-metadata.json", "release-inputs-build.json",
                           "release-inputs-fuzz.json", "fuzz-report.json")]
    return {
        "schema": 1, "repository": "owner/player", "version": "1.2.3", "channel": "rc",
        "tag": "v1.2.3-rc.2", "source_sha": "a" * 40, "run_id": 123, "run_attempt": 2,
        "version_plan": {"promotion": "promote-bytes"}, "assets": assets,
        "fuzzing_profile": {"channel": "rc"},
        "build_receipts": [{"name": item["name"], "sha256": item["sha256"]}
                           for item in assets if item["name"] == "release-inputs-build.json"],
        "validation_receipts": [{"name": item["name"], "sha256": item["sha256"]}
                                for item in assets if item["name"] == "release-inputs-fuzz.json"],
    }


class DistributionSchemaTests(unittest.TestCase):
    def setUp(self):
        self.rc = candidate()
        self.raw = encoded(self.rc)
        self.compact = distribution.stable_distribution(self.rc, self.raw)

    def test_only_declared_evidence_is_omitted(self):
        self.assertEqual([a["name"] for a in self.compact["assets"]],
                         ["SHA256SUMS", "build-metadata.json", "latest.json", "player.zip",
                          "player.zip.sig", "runtime.json"])
        self.assertEqual(self.compact["rc_evidence"]["manifest_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(distribution.parse_distribution(encoded(self.compact), "OWNER/player", "v1.2.3"), self.compact)
        distribution.verify_distribution(self.compact, self.rc, self.raw)

    def test_ordinary_report_filename_is_preserved_without_fuzz_declaration(self):
        self.rc.pop("fuzzing_profile")
        self.rc.pop("validation_receipts")
        self.rc["assets"] = [item for item in self.rc["assets"]
                             if item["name"] != "release-inputs-fuzz.json"]
        compact = distribution.stable_distribution(self.rc, encoded(self.rc))
        report = next(item for item in self.rc["assets"] if item["name"] == "fuzz-report.json")
        self.assertIn(report, compact["assets"])
        self.assertEqual({item["name"] for item in compact["assets"]},
                         {item["name"] for item in self.rc["assets"]} - {"release-inputs-build.json"})
        distribution.verify_distribution(compact, self.rc, encoded(self.rc))

    def test_partial_fuzz_declaration_cannot_be_reclassified_as_an_ordinary_payload(self):
        for remove in ("profile", "report", "receipt", "receipt-and-report"):
            altered = deepcopy(self.rc)
            if remove == "profile":
                altered.pop("fuzzing_profile")
            if "receipt" in remove:
                altered["validation_receipts"] = []
                altered["assets"] = [item for item in altered["assets"]
                                     if item["name"] != "release-inputs-fuzz.json"]
            if "report" in remove:
                altered["assets"] = [item for item in altered["assets"] if item["name"] != "fuzz-report.json"]
            with self.subTest(remove=remove), self.assertRaises(ValueError):
                distribution.stable_distribution(altered, encoded(altered))
        for profile in ({}, None, False, [], "rc"):
            altered = deepcopy(self.rc)
            altered["fuzzing_profile"] = profile
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                distribution.stable_distribution(altered, encoded(altered))

    def test_raw_manifest_hash_is_not_a_reserialized_hash(self):
        raw = json.dumps(self.rc, indent=4).encode() + b"\n"
        result = distribution.stable_distribution(self.rc, raw)
        self.assertNotEqual(result["rc_evidence"], self.compact["rc_evidence"])
        self.assertEqual(result["rc_evidence"]["manifest_sha256"], hashlib.sha256(raw).hexdigest())

    def test_projection_accepts_legacy_asset_metadata_but_does_not_copy_it(self):
        self.rc["assets"][0]["media_type"] = "application/zip"
        result = distribution.stable_distribution(self.rc, encoded(self.rc))
        self.assertTrue(all(set(a) == {"name", "size", "sha256"} for a in result["assets"]))

    def test_optional_mode_requires_exact_value(self):
        self.assertFalse(distribution.enabled({}))
        self.assertTrue(distribution.enabled({"stable_distribution": distribution.KIND}))
        for value in (None, False, True, 1, "", "unknown", []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                distribution.enabled({"stable_distribution": value})

    def test_closed_schema_rejects_untrusted_references_and_recursive_chains(self):
        changes = [("schema", True), ("schema", 1), ("kind", "other"),
                   ("repository", "other/player"), ("tag", "v1.2.4"),
                   ("version", "1.2.3-rc.2"), ("channel", "rc"), ("source_sha", "A" * 40)]
        for key, value in changes:
            altered = deepcopy(self.compact)
            altered[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                distribution.parse_distribution(encoded(altered), "owner/player", "v1.2.3")
        for key, value in (("url", "https://other.test/rc"), ("tag", "v1.2.3"),
                           ("run_attempt", True), ("run_id", 0), ("source_sha", "b" * 40),
                           ("manifest_sha256", "X" * 64)):
            altered = deepcopy(self.compact)
            altered["rc_evidence"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                distribution.parse_distribution(encoded(altered), "owner/player", "v1.2.3")
        self.rc["schema"] = 2
        with self.assertRaises(ValueError):
            distribution.stable_distribution(self.rc, encoded(self.rc))

    def test_duplicate_json_fields_bad_assets_and_resource_bounds_rejected(self):
        duplicate = encoded(self.compact).replace(b'"schema": 2', b'"schema": 2, "schema": 2')
        for raw in (duplicate, b"{}" * distribution.MAX_BYTES, b'{"schema": NaN}', b"[]"):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                distribution.parse_distribution(raw, "owner/player", "v1.2.3")
        for key, value in (("size", True), ("size", -1),
                           ("name", "../file"), ("name", "release-manifest.json"),
                           ("sha256", "0" * 63), ("extra", "no")):
            altered = deepcopy(self.compact)
            altered["assets"][0][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                distribution.parse_distribution(encoded(altered), "owner/player", "v1.2.3")
        self.compact["assets"].append(deepcopy(self.compact["assets"][0]))
        with self.assertRaises(ValueError):
            distribution.parse_distribution(encoded(self.compact), "owner/player", "v1.2.3")

    def test_zero_byte_payload_retains_its_exact_identity(self):
        empty = {"name": "empty-marker", "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}
        self.rc["assets"].append(empty)
        compact = distribution.stable_distribution(self.rc, encoded(self.rc))
        self.assertIn(empty, compact["assets"])
        self.assertEqual(distribution.parse_distribution(encoded(compact), "owner/player", "v1.2.3"), compact)

    def test_missing_receipt_and_unbound_fuzz_cannot_be_silently_removed(self):
        for edit in (lambda x: x["build_receipts"].clear(),
                     lambda x: x["validation_receipts"].clear(),
                     lambda x: x["fuzzing_profile"].clear(),
                     lambda x: x["build_receipts"][0].update(sha256="0" * 64),
                     lambda x: x["version_plan"].update(promotion="final-build")):
            altered = deepcopy(self.rc)
            edit(altered)
            with self.assertRaises(ValueError):
                distribution.stable_distribution(altered, encoded(altered))

    def test_projection_rejects_dropped_added_rehashed_or_reordered_payloads(self):
        for edit in (lambda x: x["assets"].pop(),
                     lambda x: x["assets"].append({"name": "extra.zip", "size": 1, "sha256": "0" * 64}),
                     lambda x: x["assets"][0].update(sha256="0" * 64),
                     lambda x: x["assets"].reverse()):
            altered = deepcopy(self.compact)
            edit(altered)
            with self.assertRaises(ValueError):
                distribution.verify_distribution(altered, self.rc, self.raw)


class CompactPromotionTests(unittest.TestCase):
    """Publish actual synthetic receipts through the production promotion path."""
    setUp = versioned.LifecycleTests.setUp
    start_run = versioned.LifecycleTests.start_run
    release_run = versioned.LifecycleTests.release_run

    def build_current(self):
        plan = versioned.LifecycleTests.build_current(self)
        if (getattr(self, "manifest_payload", False) or getattr(self, "empty_payload", False)
                or getattr(self, "ordinary_fuzz_report_payload", False)):
            assets = Path(self.args.assets)
            (assets / "release-inputs-linux.json").unlink()
            if getattr(self, "manifest_payload", False):
                (assets / "app.json").rename(assets / "app-manifest.json")
            if getattr(self, "empty_payload", False):
                (assets / "empty-marker").write_bytes(b"")
            if getattr(self, "ordinary_fuzz_report_payload", False):
                (assets / "fuzz-report.json").write_bytes(b'{"kind":"ordinary-application-report"}\n')
            versioned.receipt.create_receipt(versioned.lifecycle.PLAN,
                                            self.root / ".release-inputs.json", assets,
                                            assets / "release-inputs-linux.json")
        return plan

    def accepted_rc(self):
        self.policy["versioning"]["promotion"] = "promote-bytes"
        self.policy["versioning"]["artifacts"] = [
            {"path": "*manifest.json" if getattr(self, "manifest_payload", False) else "app.json",
             "format": "json", "field": "version", "value": "package"}]
        (self.root / rc.POLICY).write_bytes(rc.json_bytes(self.policy))
        published, plan, manifest = self.release_run("rc", 100)
        self.gh.jobs_by_run[100] = deepcopy(self.gh.jobs)
        return published, plan, manifest, rc.EVIDENCE.read_bytes()

    def driver(self, tag, **settings):
        self.start_run("stable", 101, tag)
        driver_sha = "b" * 40
        self.gh.default_head = driver_sha
        self.gh.runs[101]["head_sha"] = driver_sha
        self.enterContext(patch.object(rc, "checked_out_sha", return_value=driver_sha))
        self.gh.source_policies[driver_sha] = {**deepcopy(self.policy), **settings}
        (self.root / rc.POLICY).write_bytes(rc.json_bytes(self.gh.source_policies[driver_sha]))
        Path(os.environ["GITHUB_EVENT_PATH"]).write_text(json.dumps({"inputs": {
            "channel": "stable", "expected_sha": driver_sha, "rc_tag": tag}}))
        return argparse.Namespace(repo=versioned.REPO, rc=tag, run_id="101")

    def promoted_files(self, args):
        with patch.object(rc, "GitHub", return_value=self.gh):
            result = rc.promote(args)
        release = next(value for value in self.gh.releases.values() if value["tag_name"] == result["tag"])
        return release, {asset["name"]: self.gh.files[asset["id"]] for asset in self.gh.assets[release["id"]]}

    def test_current_driver_opt_in_promotes_older_rc_with_real_receipt_verification(self):
        published, plan, manifest, raw = self.accepted_rc()
        args = self.driver(published["tag"], stable_distribution=distribution.KIND)
        self.assertEqual(versioned.lifecycle.prepare(self.args)["build"], "false")
        release, files = self.promoted_files(args)
        self.assertEqual(set(files), {"app.json", rc.MANIFEST})
        compact = distribution.parse_distribution(files[rc.MANIFEST], versioned.REPO, "v1.2.3")
        distribution.verify_distribution(compact, manifest, raw)
        self.assertEqual(rc.digest(files["app.json"]), manifest["assets"][0]["sha256"])
        self.assertEqual(self.gh.ledger["publication_floor"], plan["build_number"])
        self.assertIn("Build and validation evidence remains", release["body"])
        self.assertNotIn("Assets and `release-manifest.json` are byte-for-byte", release["body"])
        original_id = next(key for key, value in self.gh.releases.items()
                           if value["tag_name"] == published["tag"])
        self.assertEqual(self.gh.binary("releases/assets/" + str(next(
            a["id"] for a in self.gh.assets[original_id] if a["name"] == rc.MANIFEST))), raw)

    def test_absent_current_driver_opt_in_preserves_exact_legacy_rc(self):
        published, _, manifest, raw = self.accepted_rc()
        _, files = self.promoted_files(self.driver(published["tag"]))
        self.assertEqual(files[rc.MANIFEST], raw)
        self.assertEqual(set(files), {a["name"] for a in manifest["assets"]} | {rc.MANIFEST})

    def test_package_metadata_glob_does_not_include_generated_release_manifest(self):
        self.manifest_payload = True
        published, _, manifest, raw = self.accepted_rc()
        _, files = self.promoted_files(self.driver(published["tag"], stable_distribution=distribution.KIND))
        self.assertEqual(set(files), {"app-manifest.json", rc.MANIFEST})
        distribution.verify_distribution(json.loads(files[rc.MANIFEST]), manifest, raw)

    def test_non_fuzz_producer_does_not_require_optional_fuzz_adapter(self):
        published, _, _, _ = self.accepted_rc()
        args = self.driver(published["tag"], stable_distribution=distribution.KIND)
        original = builtins.__import__

        def reject_optional(name, *values, **keywords):
            if name.endswith("release_fuzz"):
                raise ModuleNotFoundError("Optional fuzz adapter is not installed")
            return original(name, *values, **keywords)

        with patch.object(builtins, "__import__", side_effect=reject_optional):
            _, files = self.promoted_files(args)
        self.assertEqual(set(files), {"app.json", rc.MANIFEST})

    def test_projection_uses_the_real_uploader_private_staging_boundary(self):
        published, _, _, _ = self.accepted_rc()
        args = self.driver(published["tag"], stable_distribution=distribution.KIND)
        real_client = rc.GitHub(versioned.REPO)
        fake_upload = self.gh.upload

        def transport(argv, **_kwargs):
            path = Path(argv[argv.index("--input") + 1])
            self.assertTrue(path.parent.name.startswith("release-promote-distribution-"))
            response = {"id": 900, "name": path.name, "state": "uploaded", "size": path.stat().st_size}
            return subprocess.CompletedProcess(argv, 0, b"HTTP/2.0 201 Created\r\n\r\n" + encoded(response), b"")

        def admitted_upload(tag, path, *, release_id):
            real_client.upload(tag, path, release_id=release_id)
            fake_upload(tag, path, release_id=release_id)

        with patch.object(rc.subprocess, "run", side_effect=transport) as requests, patch.object(
            self.gh, "upload", side_effect=admitted_upload
        ):
            _, files = self.promoted_files(args)
        self.assertEqual(requests.call_count, len(files))

    def test_zero_byte_rc_payload_is_published_without_omission_or_rewriting(self):
        self.empty_payload = True
        published, _, manifest, raw = self.accepted_rc()
        _, files = self.promoted_files(self.driver(published["tag"], stable_distribution=distribution.KIND))
        self.assertEqual(files["empty-marker"], b"")
        compact = json.loads(files[rc.MANIFEST])
        distribution.verify_distribution(compact, manifest, raw)
        self.assertIn({"name": "empty-marker", "size": 0, "sha256": rc.digest(b"")}, compact["assets"])

    def test_ordinary_receipt_covered_report_survives_real_promotion(self):
        self.ordinary_fuzz_report_payload = True
        published, _, manifest, raw = self.accepted_rc()
        self.assertNotIn("fuzzing_profile", manifest)
        self.assertEqual(manifest.get("validation_receipts", []), [])
        original_id = next(key for key, value in self.gh.releases.items()
                           if value["tag_name"] == published["tag"])
        original_report = next(asset for asset in self.gh.assets[original_id]
                               if asset["name"] == "fuzz-report.json")
        original_bytes = self.gh.files[original_report["id"]]
        _, files = self.promoted_files(self.driver(published["tag"], stable_distribution=distribution.KIND))
        self.assertEqual(set(files), {"app.json", "fuzz-report.json", rc.MANIFEST})
        self.assertEqual(files["fuzz-report.json"], original_bytes)
        compact = json.loads(files[rc.MANIFEST])
        self.assertIn({"name": "fuzz-report.json", "size": len(original_bytes),
                       "sha256": rc.digest(original_bytes)}, compact["assets"])
        distribution.verify_distribution(compact, manifest, raw)

    def test_unknown_mode_and_inventory_signing_reject_before_allocation_or_upload(self):
        published, _, _, _ = self.accepted_rc()
        for settings in ({"stable_distribution": None}, {"stable_distribution": True},
                         {"stable_distribution": "other"},
                         {"stable_distribution": distribution.KIND, "sign_releases": True}):
            args = self.driver(published["tag"], **settings)
            writes = deepcopy(self.gh.writes), deepcopy(self.gh.ledger_writes)
            with self.assertRaises(rc.ReleaseError):
                versioned.lifecycle.prepare(self.args)
            with patch.object(rc, "GitHub", return_value=self.gh), self.assertRaises(rc.ReleaseError):
                rc.promote(args)
            self.assertEqual((self.gh.writes, self.gh.ledger_writes), writes)

    def test_compiled_signature_requirement_rejects_before_publication(self):
        published, _, _, _ = self.accepted_rc()
        args = self.driver(published["tag"], stable_distribution=distribution.KIND)
        writes = deepcopy(self.gh.writes)
        with patch.object(rc, "GitHub", return_value=self.gh), patch.object(rc, "RELEASE_SIGNATURES_REQUIRED", True), self.assertRaisesRegex(rc.ReleaseError, "full-inventory"):
            rc.promote(args)
        self.assertEqual(self.gh.writes, writes)

    def test_omitted_receipt_corruption_still_prevents_every_write(self):
        published, _, _, _ = self.accepted_rc()
        args = self.driver(published["tag"], stable_distribution=distribution.KIND)
        receipt_id = next(asset["id"] for assets in self.gh.assets.values() for asset in assets
                          if asset["name"] == "release-inputs-linux.json")
        self.gh.files[receipt_id] = b"x" * len(self.gh.files[receipt_id])
        writes = deepcopy(self.gh.writes), deepcopy(self.gh.ledger_writes)
        with patch.object(rc, "GitHub", return_value=self.gh), self.assertRaises(rc.ReleaseError):
            rc.promote(args)
        self.assertEqual((self.gh.writes, self.gh.ledger_writes), writes)

    def test_rehashed_but_invalid_omitted_receipt_is_verified_before_projection(self):
        published, _, manifest, _ = self.accepted_rc()
        args = self.driver(published["tag"], stable_distribution=distribution.KIND)
        original_id = next(key for key, value in self.gh.releases.items()
                           if value["tag_name"] == published["tag"])
        assets = self.gh.assets[original_id]
        receipt_asset = next(a for a in assets if a["name"] == "release-inputs-linux.json")
        invalid = b"{}"
        self.gh.files[receipt_asset["id"]] = invalid
        receipt_asset["size"] = len(invalid)
        entry = next(a for a in manifest["assets"] if a["name"] == receipt_asset["name"])
        entry.update(size=len(invalid), sha256=rc.digest(invalid))
        manifest["build_receipts"][0]["sha256"] = rc.digest(invalid)
        raw = rc.json_bytes(manifest)
        asset = next(a for a in assets if a["name"] == rc.MANIFEST)
        asset["size"] = len(raw)
        self.gh.files[asset["id"]] = raw
        self.gh.save_completed_evidence(raw)
        writes = deepcopy(self.gh.writes), deepcopy(self.gh.ledger_writes)
        with patch.object(rc, "GitHub", return_value=self.gh), self.assertRaises(rc.ReleaseError):
            rc.promote(args)
        self.assertEqual((self.gh.writes, self.gh.ledger_writes), writes)

    def test_candidate_snapshot_is_rechecked_after_full_receipt_verification(self):
        published, _, _, _ = self.accepted_rc()
        args = self.driver(published["tag"], stable_distribution=distribution.KIND)
        writes = deepcopy(self.gh.writes), deepcopy(self.gh.ledger_writes)
        original = rc.prepare_stable_distribution

        def changed_after_verification(*values):
            original(*values)
            self.gh.refs[published["tag"]]["object"]["sha"] = "b" * 40

        with patch.object(rc, "GitHub", return_value=self.gh), patch.object(
            rc, "prepare_stable_distribution", side_effect=changed_after_verification
        ), self.assertRaisesRegex(rc.ReleaseError, "Candidate changed"):
            rc.promote(args)
        self.assertEqual((self.gh.writes, self.gh.ledger_writes), writes)


if __name__ == "__main__":
    unittest.main()
