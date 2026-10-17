"""Synthetic encrypted-catalog delivery; no production files or cloud access."""

import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from nacl.public import PrivateKey, SealedBox

from tools.secret_control import catalog, catalog_delivery, github_api, github_catalog
from tools.secret_control import __main__ as cli
from tools.secret_control.hcp import mappings
from tools.secret_control.materialize import MaterializationError
from tools.secret_control.private_io import new_private, process


def organization():
    return {"type": "github", "scope": "organization", "name": "BOT_PAT",
            "owner": {"login": "example-org", "id": 18, "type": "Organization"},
            "visibility": "selected", "selected_repository_ids": [19, 20]}


def repository():
    return {"type": "github", "scope": "repository", "name": "BOT_PAT",
            "owner": {"login": "example-user", "id": 21, "type": "User"},
            "repository": {"full_name": "example-user/project", "id": 22}}


def document():
    return {"schema_version": 2, "catalog_id": "f89dc70c-6ee5-4c29-9927-179924824ac2",
            "generation": 1, "entries": [
                {"id": "bot", "revision": 1, "value": "synthetic-bot-not-a-token",
                 "targets": [organization(), repository()]},
                {"id": "profile", "revision": 1, "value": "synthetic profile\nsecond line\n",
                 "targets": [{"type": "hcp", "workspace_id": "ws-0000000000000001",
                              "category": "terraform", "key": "profile"},
                             {"type": "oci", "region": "us-example-1",
                              "compartment_id": "ocid1.compartment.oc1..example",
                              "vault_id": "ocid1.vault.oc1..example",
                              "key_id": "ocid1.key.oc1..example", "name": "profile"}]}]}


def expectation(target):
    result = {"created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"}
    if target["scope"] == "organization":
        result.update(visibility=target["visibility"],
                      selected_repository_ids=target["selected_repository_ids"])
    return copy.deepcopy(result)


class CatalogSchemaTests(unittest.TestCase):
    def test_mixed_destination_catalog_and_plan_redaction(self):
        value = document()
        catalog.validate(value)
        result = catalog.plan(value)
        self.assertEqual(result["targets"], {"github": 2, "hcp": 1, "oci": 1})
        self.assertNotIn("synthetic-bot", json.dumps(result))
        self.assertNotIn("example-user", json.dumps(result))
        metadata, payloads = mappings(value)
        self.assertEqual(len(metadata), 1)
        self.assertEqual(list(payloads.values()), [value["entries"][1]["value"]])

    def test_schema_one_remains_strict_and_upgrade_preserves_lineage(self):
        old = document()
        old["schema_version"] = 1
        old["entries"] = old["entries"][1:]
        catalog.validate(old)
        new = copy.deepcopy(old)
        new.update(schema_version=2, generation=2)
        catalog.lineage(new, old)
        new["entries"][0]["targets"].append(repository())
        with self.assertRaisesRegex(catalog.ControlError, "revision"):
            catalog.lineage(new, old)
        new["entries"][0]["revision"] = 2
        catalog.validate(new)
        catalog.lineage(new, old)
        old["generation"] = 3
        with self.assertRaisesRegex(catalog.ControlError, "lineage"):
            catalog.lineage(old, new)
        old["entries"][0]["targets"].append(repository())
        with self.assertRaisesRegex(catalog.ControlError, "unsupported target"):
            catalog.validate(old)

    def test_no_removed_entries_during_upgrade(self):
        old = document()
        new = copy.deepcopy(old)
        new["generation"] = 2
        new["entries"].pop()
        with self.assertRaisesRegex(catalog.ControlError, "removal"):
            catalog.lineage(new, old)

    def test_duplicate_destination_uses_immutable_id(self):
        value = document()
        duplicate = repository()
        duplicate["repository"]["full_name"] = "example-user/renamed"
        value["entries"][1]["targets"].append(duplicate)
        with self.assertRaisesRegex(catalog.ControlError, "duplicate destination"):
            catalog.validate(value)

    def test_invalid_scope_names_owners_and_visibility_fail_closed(self):
        changes = [lambda t: t.update(scope="environment"),
                   lambda t: t.update(name="GITHUB_ADMIN_TOKEN"),
                   lambda t: t.update(name="../TOKEN"),
                   lambda t: t["owner"].update(id=True),
                   lambda t: t["owner"].update(type="User"),
                   lambda t: t.update(selected_repository_ids=[20, 19]),
                   lambda t: t.update(visibility="all")]
        for change in changes:
            with self.subTest(change=change):
                target = organization()
                change(target)
                with self.assertRaises(catalog.ControlError):
                    catalog.github_target(target)
        target = repository()
        target["repository"]["full_name"] = "someone-else/project"
        with self.assertRaises(catalog.ControlError):
            catalog.github_target(target)

    def test_target_count_limit(self):
        value = document()
        value["entries"][0]["targets"] = [dict(repository(), name=f"TOKEN_{n}") for n in range(100)]
        catalog.validate(value)
        value["entries"][0]["targets"].append(dict(repository(), name="TOKEN_100"))
        with self.assertRaisesRegex(catalog.ControlError, "targets"):
            catalog.validate(value)


class EncryptedFixture(unittest.TestCase):
    def setUp(self):
        self.age = os.environ.get("SECRET_CONTROL_AGE") or shutil.which("age")
        self.keygen = os.environ.get("SECRET_CONTROL_AGE_KEYGEN") or shutil.which("age-keygen")
        if not self.age or not self.keygen:
            self.skipTest("real age tools are required for catalog delivery tests")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.identity = self.root / "synthetic.agekey"
        identity = process([self.keygen])
        new_private(self.identity, identity)
        self.recipient = process([self.keygen, "-y"], identity).decode().strip()
        self.value = document()
        self.path = self.root / "catalog.age"
        self.encrypt()

    def encrypt(self):
        if self.path.exists():
            self.path.unlink()
        new_private(self.path, process([self.age, "-r", self.recipient], json.dumps(self.value).encode()))
        self.digest = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def load(self, **kwargs):
        args = {"expected_sha256": self.digest, "generation": 1}
        args.update(kwargs)
        return catalog_delivery.load(self.path, self.identity, self.age, **args)


class CatalogSelectionTests(EncryptedFixture):
    def test_wrong_ciphertext_hash_precedes_identity_and_tool_access(self):
        with patch.object(catalog_delivery, "decrypt_bytes") as decrypt:
            with self.assertRaisesRegex(catalog.ControlError, "digest mismatch"):
                self.load(expected_sha256="0" * 64)
            decrypt.assert_not_called()

    def test_wrong_generation_identifier_and_revision_refused(self):
        for kwargs in ({"generation": 2}, {"catalog_id": "another"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(catalog.ControlError):
                self.load(**kwargs)
        with self.assertRaisesRegex(catalog.ControlError, "revision mismatch"):
            catalog_delivery.entry(self.load(), "bot", 2)

    def test_only_selected_value_materialized_and_conflicts_refused(self):
        output = self.root / "selected"
        args = dict(expected_sha256=self.digest, generation=1, name="profile", revision=1,
                    output=output)
        result = catalog_delivery.materialize(self.path, self.identity, self.age, **args)
        self.assertEqual(output.read_text(), self.value["entries"][1]["value"])
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("sha256", result)
        self.assertNotIn("synthetic", json.dumps(result))
        self.assertEqual(catalog_delivery.materialize(self.path, self.identity, self.age, **args)["status"],
                         "unchanged")
        output.write_bytes(b"different")
        with self.assertRaises(MaterializationError):
            catalog_delivery.materialize(self.path, self.identity, self.age, **args)

    def test_selection_rejects_links_and_wrong_entry_without_output(self):
        output = self.root / "selected"
        args = dict(expected_sha256=self.digest, generation=1, name="missing", revision=1,
                    output=output)
        with self.assertRaises(catalog.ControlError):
            catalog_delivery.materialize(self.path, self.identity, self.age, **args)
        self.assertFalse(output.exists())
        output.symlink_to(self.identity)
        args["name"] = "bot"
        with self.assertRaises(MaterializationError):
            catalog_delivery.materialize(self.path, self.identity, self.age, **args)

    def test_cli_selected_materialization(self):
        output = self.root / "from-cli"
        args = cli.parser().parse_args([
            "catalog-materialize", "--catalog", str(self.path), "--identity", str(self.identity),
            "--age", self.age, "--expected-sha256", self.digest, "--generation", "1",
            "--entry", "bot", "--revision", "1", "--output", str(output)])
        self.assertEqual(cli.execute(args)["status"], "created")
        self.assertEqual(output.read_text(), self.value["entries"][0]["value"])


class ScopedFake:
    def __init__(self, target, actor):
        self.target, self.expected_actor = target, actor
        self.current = expectation(target)
        self.key = PrivateKey.generate()
        self.writes = []
        self.on_read = lambda: None
        self.failure = False

    def actor(self):
        return self.expected_actor

    def repository(self):
        return {**self.target["repository"], "owner": self.target["owner"],
                "archived": False, "disabled": False, "permissions": {"admin": True}}

    def organization(self):
        return self.target["owner"]

    def organization_membership(self):
        return {"state": "active", "role": "admin", "organization": self.organization(),
                "user": self.actor()}

    def public_key(self, _item):
        return {"key_id": "key", "key": base64.b64encode(bytes(self.key.public_key)).decode()}

    def secret(self, _item):
        self.on_read()
        return copy.deepcopy(self.current)

    def write(self, item, encrypted, _key_id):
        self.writes.append(SealedBox(self.key).decrypt(base64.b64decode(encrypted)))
        if self.failure:
            raise RuntimeError("synthetic-private-error")
        self.current["updated_at"] = "2026-10-18T00:00:00Z"
        return 204


class CatalogPublicationTests(EncryptedFixture):
    def setUp(self):
        super().setUp()
        self.manifest_file = self.root / "manifest.json"
        self.journal = self.root / "journal"
        self.journal.mkdir(mode=0o700)
        self.binding = {"schema_version": 2,
                        "operation_id": "7adfe575-c5d4-4cbe-86c6-0453cf8b7135",
                        "actor": {"login": "operator", "id": 17},
                        "catalog": {"catalog_id": self.value["catalog_id"],
                                    "generation": 1, "sha256": self.digest},
                        "secrets": [{"entry_id": "bot", "revision": 1, "target": t,
                                     "expected": expectation(t)}
                                    for t in copy.deepcopy(self.value["entries"][0]["targets"])]}
        self.apis = [ScopedFake(i["target"], self.binding["actor"]) for i in self.binding["secrets"]]

    def publish(self, apply=False):
        if self.manifest_file.exists():
            self.manifest_file.unlink()
        raw = json.dumps(self.binding).encode()
        new_private(self.manifest_file, raw)
        factory = Mock(side_effect=self.apis)
        with patch.object(github_catalog, "CatalogAPI", factory):
            result = github_api.publish(self.manifest_file, hashlib.sha256(raw).hexdigest(), "unused",
                                        catalog_file=self.path, identity=self.identity, age=self.age,
                                        apply=apply, journal=self.journal)
        return result

    def test_personal_repository_and_selected_org_delivery_from_catalog(self):
        self.assertEqual(self.publish()["writes"], 0)
        self.assertFalse(list(self.journal.iterdir()))
        result = self.publish(True)
        self.assertEqual(result["metadata_observed"], 2)
        for api in self.apis:
            self.assertEqual(api.writes, [self.value["entries"][0]["value"].encode()])
        records = b"".join(p.read_bytes() for p in self.journal.iterdir())
        self.assertNotIn(b"synthetic-bot-not-a-token", records)
        self.assertNotIn(b"source_sha256", records)
        self.assertFalse(result["consumer_verified"])

    def test_manifest_size_limit_precedes_json_parser(self):
        with patch.object(github_api, "decode") as decode:
            with self.assertRaisesRegex(catalog.ControlError, "size limit"):
                github_api.manifest(b"x" * (catalog.MAX_CATALOG + 1))
            decode.assert_not_called()

    def test_top_level_cli_routes_schema_two_and_keeps_explicit_apply(self):
        raw = json.dumps(self.binding).encode()
        new_private(self.manifest_file, raw)
        args = cli.parser().parse_args([
            "github-publish", "--manifest", str(self.manifest_file), "--manifest-sha256",
            hashlib.sha256(raw).hexdigest(), "--token-file", "unused", "--catalog", str(self.path),
            "--identity", str(self.identity), "--age", self.age])
        with patch.object(github_catalog, "CatalogAPI", side_effect=self.apis):
            result = cli.execute(args)
        self.assertEqual(result["status"], "planned")
        self.assertTrue(all(not api.writes for api in self.apis))

    def test_catalog_and_entry_binding_mismatch_precede_authentication(self):
        changes = [lambda v: v["catalog"].update(sha256="0" * 64),
                   lambda v: v["catalog"].update(generation=2),
                   lambda v: v["catalog"].update(catalog_id="31294b94-6117-4871-afd7-dd25662c43f3"),
                   lambda v: v["secrets"][0].update(revision=2),
                   lambda v: v["secrets"][1]["target"]["repository"].update(id=23)]
        for change in changes:
            original = copy.deepcopy(self.binding)
            change(self.binding)
            with self.subTest(change=change), patch.object(github_catalog, "CatalogAPI") as factory:
                with self.assertRaises(catalog.ControlError):
                    github_catalog.publish_catalog(self.binding, "a" * 64, "unused",
                        catalog_file=self.path, identity=self.identity, age=self.age)
                factory.assert_not_called()
            self.binding = original

    def test_all_destinations_preflight_missing_existing_secret_and_policy(self):
        self.apis[1].current = None
        with self.assertRaises(catalog.ControlError):
            self.publish(True)
        self.assertTrue(all(not api.writes for api in self.apis))
        self.assertFalse(list(self.journal.glob("*.intent.json")))
        self.apis[1].current = expectation(self.binding["secrets"][1]["target"])
        self.apis[0].current["selected_repository_ids"] = [19]
        with self.assertRaises(catalog.ControlError):
            self.publish(True)
        self.assertTrue(all(not api.writes for api in self.apis))

    def test_expected_policy_cannot_expand_approved_catalog_targets(self):
        self.binding["secrets"][0]["expected"].update(visibility="all", selected_repository_ids=[])
        with self.assertRaisesRegex(catalog.ControlError, "cannot change"):
            self.publish()

    def test_duplicate_destination_and_invalid_standard_name_refused(self):
        self.binding["secrets"].append(copy.deepcopy(self.binding["secrets"][1]))
        with self.assertRaisesRegex(catalog.ControlError, "duplicate"):
            self.publish()
        self.binding["secrets"].pop()
        self.binding["secrets"][1]["target"]["name"] = "GITHUB_TOKEN"
        with self.assertRaisesRegex(catalog.ControlError, "secret name"):
            self.publish()

    def test_wrong_personal_owner_id_type_and_missing_admin_refused(self):
        api = self.apis[1]
        base = api.repository()
        responses = [dict(base, owner={**base["owner"], "id": 99}),
                     dict(base, owner={**base["owner"], "type": "Organization"}),
                     dict(base, permissions={"admin": False}), dict(base, id=True)]
        for response in responses:
            with self.subTest(response=response), patch.object(api, "repository", return_value=response):
                with self.assertRaises(catalog.ControlError):
                    self.publish(True)
                self.assertTrue(all(not item.writes for item in self.apis))

    def test_wrong_org_id_and_member_role_refused(self):
        for method, response in [("organization", {**organization()["owner"], "id": 99}),
                                 ("organization_membership", {"role": "member", "state": "active"})]:
            with self.subTest(method=method), patch.object(self.apis[0], method, return_value=response):
                with self.assertRaises(catalog.ControlError):
                    self.publish(True)
                self.assertTrue(all(not api.writes for api in self.apis))

    def test_payload_frozen_before_remote_reads_and_retry_refused(self):
        self.apis[0].on_read = lambda: self.path.write_bytes(b"changed ciphertext")
        self.publish(True)
        self.assertEqual(self.apis[1].writes, [self.value["entries"][0]["value"].encode()])
        self.encrypt()
        self.binding["catalog"]["sha256"] = self.digest
        with self.assertRaisesRegex(catalog.ControlError, "already attempted"):
            self.publish(True)

    def test_partial_ambiguous_write_journal_no_retry(self):
        self.apis[1].failure = True
        with self.assertRaises(catalog.ControlError) as error:
            self.publish(True)
        self.assertNotIn("synthetic-private", str(error.exception))
        record = json.loads(next(self.journal.glob("*.stopped.json")).read_bytes())
        self.assertEqual(record["accepted"], 1)
        self.assertTrue(record["write_outcome_may_be_unknown"])
        with self.assertRaisesRegex(catalog.ControlError, "already attempted"):
            self.publish(True)
        self.assertEqual([len(api.writes) for api in self.apis], [1, 1])


class CatalogTransportTests(unittest.TestCase):
    def api(self, target):
        with patch.object(github_catalog, "read_private", return_value=b"x" * 40):
            return github_catalog.CatalogAPI("unused", target)

    def test_personal_existing_standard_name_path_only(self):
        target = repository()
        target["name"] = "CORE_DELIVERY_SIGNING_KEY"
        api = self.api(target)
        path = "/repos/example-user/project/actions/secrets/CORE_DELIVERY_SIGNING_KEY"
        api.validate_request("PUT", path, {"encrypted_value": "encrypted", "key_id": "key"})
        for disallowed in (path.replace("project", "another"), path.replace("CORE_DELIVERY_SIGNING_KEY", "BOT_PAT"),
                           "/orgs/example-user/actions/secrets/CORE_DELIVERY_SIGNING_KEY"):
            with self.subTest(path=disallowed), self.assertRaises(catalog.ControlError):
                api.validate_request("PUT", disallowed, {"encrypted_value": "encrypted", "key_id": "key"})

    def test_visibility_and_selection_are_fixed_even_at_transport_boundary(self):
        api = self.api(organization())
        path = "/orgs/example-org/actions/secrets/BOT_PAT"
        body = {"encrypted_value": "encrypted", "key_id": "key", "visibility": "selected",
                "selected_repository_ids": [19, 20]}
        api.validate_request("PUT", path, body)
        for change in ({"visibility": "all"}, {"selected_repository_ids": [19, 20, 21]}):
            with self.subTest(change=change), self.assertRaises(catalog.ControlError):
                api.validate_request("PUT", path, {**body, **change})

    def test_bounded_request_accommodates_supported_value_and_selection(self):
        target = organization()
        target["selected_repository_ids"] = list(range(2**53 - 1001, 2**53 - 1))
        api = self.api(target)
        item = {**target, "expected": expectation(target)}
        with patch.object(github_api.http.client, "HTTPSConnection") as connection:
            connection.return_value.getresponse.return_value.status = 204
            self.assertEqual(api.write(item, "a" * 22000, "key"), 204)
            self.assertGreater(len(connection.return_value.request.call_args.kwargs["body"]), 32768)
        with patch.object(github_api.http.client, "HTTPSConnection") as connection:
            with self.assertRaisesRegex(catalog.ControlError, "size limit"):
                api.write(item, "a" * github_api.MAX_REQUEST, "key")
            connection.return_value.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
