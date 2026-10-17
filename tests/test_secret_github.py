"""Synthetic GitHub publication: actual encryption, fake remote mutations."""
import base64
import copy
import hashlib
import json
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from nacl.public import PrivateKey, SealedBox

from tools.secret_control import github_api as gh
from tools.secret_control import __main__ as cli
from tools.secret_control.catalog import ControlError


class FakeAPI:
    def __init__(self, manifest):
        self.binding = manifest
        self.keys = {scope: PrivateKey.generate() for scope in ("organization", "repository")}
        self.values = {i["name"]: copy.deepcopy(i["expected"]) for i in manifest["secrets"]}
        self.writes = []
        self.fail_write = False
        self.reads = 0
        self.on_read = lambda: None

    def actor(self):
        return self.binding["actor"]

    def repository(self):
        return {**self.binding["repository"], "owner": self.binding["organization"],
                "permissions": {"admin": True}, "archived": False, "disabled": False}

    def organization(self):
        return self.binding["organization"]

    def organization_membership(self):
        return {"state": "active", "role": "admin", "organization": self.organization(),
                "user": self.actor()}

    def public_key(self, item):
        return {"key_id": item["scope"],
                "key": base64.b64encode(bytes(self.keys[item["scope"]].public_key)).decode()}

    def secret(self, item):
        self.reads += 1
        self.on_read()
        return copy.deepcopy(self.values[item["name"]])

    def write(self, item, encrypted, key_id):
        plaintext = SealedBox(self.keys[key_id]).decrypt(base64.b64decode(encrypted))
        self.writes.append((item["name"], plaintext))
        if self.fail_write:
            raise RuntimeError("synthetic-sensitive-network-detail")
        self.values[item["name"]]["updated_at"] = "2026-10-17T00:00:00Z"
        return 204


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.journal = self.root / "journal"
        self.journal.mkdir(mode=0o700)
        self.path = self.root / "manifest.json"
        self.value = {
            "schema_version": 1, "operation_id": "6b5b6a90-f1af-4be7-8926-57e295617a88",
            "actor": {"login": "operator", "id": 17},
            "organization": {"login": "example-org", "id": 18},
            "repository": {"full_name": "example-org/toolkit", "id": 19, "owner_id": 18},
            "secrets": [],
        }
        self.payloads = [b"synthetic-bot-token", b"-----BEGIN SYNTHETIC PRIVATE KEY-----\nline\n"]
        for name, scope, payload in zip(("BOT_PAT", "RENOVATE_SIGNING_KEY"),
                                         ("organization", "repository"), self.payloads):
            source = self.root / name
            self.write(source, payload)
            expected = {"created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z"}
            if scope == "organization":
                expected.update(visibility="all", selected_repository_ids=[])
            self.value["secrets"].append({"scope": scope, "name": name, "source": str(source),
                                        "source_sha256": hashlib.sha256(payload).hexdigest(),
                                        "expected": expected})
        self.api = FakeAPI(self.value)
        self.save()

    def write(self, path, data):
        path.write_bytes(data)
        path.chmod(0o600)

    def save(self):
        raw = json.dumps(self.value).encode()
        self.write(self.path, raw)
        self.digest = hashlib.sha256(raw).hexdigest()

    def publish(self, apply=False):
        return gh.publish(self.path, self.digest, "unused", apply=apply,
                          journal=self.journal, api_factory=lambda *_: self.api)

    def test_plan_encrypts_but_does_not_write_or_journal(self):
        self.assertEqual(self.publish()["writes"], 0)
        self.assertEqual(self.api.writes, [])
        self.assertEqual(list(self.journal.iterdir()), [])

    def test_real_sealed_boxes_two_scopes_and_redacted_receipts(self):
        result = self.publish(True)
        self.assertEqual([p for _, p in self.api.writes], self.payloads)
        self.assertEqual(result["metadata_observed"], 2)
        self.assertFalse(result["consumer_verified"])
        records = b"".join(p.read_bytes() for p in self.journal.iterdir())
        for secret in self.payloads:
            self.assertNotIn(secret, records)
        self.assertNotIn(b"source_sha256", records)
        with self.assertRaisesRegex(ControlError, "already attempted"):
            self.publish(True)
        self.assertEqual(len(self.api.writes), 2)

    def test_all_targets_preflight_before_any_mutation(self):
        self.api.values["RENOVATE_SIGNING_KEY"] = None
        with self.assertRaises(ControlError):
            self.publish(True)
        self.assertEqual(self.api.writes, [])
        self.assertFalse(list(self.journal.glob("*.intent.json")))

    def test_manifest_and_source_pins_before_authentication(self):
        factory = Mock()
        self.digest = "0" * 64
        with self.assertRaises(ControlError):
            gh.publish(self.path, self.digest, "unused", api_factory=factory)
        self.save()
        self.write(Path(self.value["secrets"][0]["source"]), b"changed")
        with self.assertRaises(ControlError):
            gh.publish(self.path, self.digest, "unused", api_factory=factory)
        factory.assert_not_called()

    def test_payload_is_not_normalized(self):
        item = self.value["secrets"][0]
        payload = b"synthetic-bot-token\n"
        self.write(Path(item["source"]), payload)
        item["source_sha256"] = hashlib.sha256(payload).hexdigest()
        self.save()
        with self.assertRaisesRegex(ControlError, "single line"):
            self.publish()

    def test_payload_bound_before_remote_reads(self):
        self.api.on_read = lambda: self.write(Path(self.value["secrets"][0]["source"]), b"changed")
        self.publish(True)
        self.assertEqual(self.api.writes[0][1], self.payloads[0])

    def test_wrong_ownership_and_non_admin_membership(self):
        for method, result in (
            ("actor", {"login": "operator", "id": 88}),
            ("organization", {"login": "example-org", "id": 88}),
            ("organization_membership", {"state": "active", "role": "member"}),
            ("repository", {**self.api.repository(), "permissions": {"admin": False}}),
        ):
            with self.subTest(method=method), patch.object(self.api, method, return_value=result):
                with self.assertRaises(ControlError):
                    self.publish(True)
                self.assertEqual(self.api.writes, [])

    def test_org_policy_drift_and_selection_drift(self):
        for changed in ({"visibility": "private"}, {"selected_repository_ids": [99]}):
            original = copy.deepcopy(self.api.values["BOT_PAT"])
            self.api.values["BOT_PAT"].update(changed)
            with self.assertRaises(ControlError):
                self.publish(True)
            self.api.values["BOT_PAT"] = original
        self.assertEqual(self.api.writes, [])

    def test_post_preflight_drift_stops_before_write(self):
        def drift():
            if self.api.reads >= 3:
                self.api.values["BOT_PAT"]["visibility"] = "private"
        self.api.on_read = drift
        with self.assertRaises(ControlError):
            self.publish(True)
        self.assertEqual(self.api.writes, [])
        record = json.loads(next(self.journal.glob("*.stopped.json")).read_bytes())
        self.assertEqual(record["phase"], "revalidation")

    def test_ambiguous_write_is_not_retried_and_blocks_replay(self):
        self.api.fail_write = True
        with self.assertRaises(ControlError) as error:
            self.publish(True)
        self.assertNotIn("synthetic-sensitive", str(error.exception))
        record = json.loads(next(self.journal.glob("*.stopped.json")).read_bytes())
        self.assertTrue(record["write_outcome_may_be_unknown"])
        with self.assertRaises(ControlError):
            self.publish(True)
        self.assertEqual(len(self.api.writes), 1)

    def test_public_key_drift_and_unexpected_creation_stop_batch(self):
        original = self.api.public_key
        calls = 0
        def changed_key(item):
            nonlocal calls
            calls += 1
            value = original(item)
            if calls > 2:
                value["key_id"] = "unexpected-new-key"
            return value
        with patch.object(self.api, "public_key", side_effect=changed_key):
            with self.assertRaises(ControlError):
                self.publish(True)
        self.assertEqual(self.api.writes, [])
        self.value["operation_id"] = "23689d8c-93d0-4ed5-99b0-bfeb239446a0"
        self.save()
        with patch.object(self.api, "write", return_value=201) as write:
            with self.assertRaises(ControlError):
                self.publish(True)
            self.assertEqual(write.call_count, 1)
        stopped = json.loads((self.journal / (self.value["operation_id"] + ".stopped.json")).read_bytes())
        self.assertTrue(stopped["write_outcome_may_be_unknown"])

    def test_missing_metadata_or_repository_bot_shadow_are_refused(self):
        mutations = [lambda v: v["secrets"][0].update(scope="repository"),
                     lambda v: v["secrets"][0].update(expected=None),
                     lambda v: v["secrets"][0].update(name="OTHER_SECRET"),
                     lambda v: v["repository"].update(full_name="elsewhere/project"),
                     lambda v: v["organization"].update(login="../elsewhere")]
        for mutate in mutations:
            value = copy.deepcopy(self.value)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ControlError):
                gh.manifest(json.dumps(value).encode())

    def test_unsafe_source_permissions_and_journal_lock(self):
        source = Path(self.value["secrets"][0]["source"])
        source.chmod(0o644)
        with self.assertRaises(ControlError):
            self.publish(True)
        source.chmod(0o600)
        with gh.journal_lock(self.journal), self.assertRaises(ControlError):
            self.publish(True)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.api = object.__new__(gh.API)
        self.api.token = "synthetic-auth-token"
        self.api.repo = "/repos/example-org/toolkit"
        self.api.org = "/orgs/example-org"
        self.api.membership = "/user/memberships/orgs/example-org"
        self.bot = {"scope": "organization", "name": "BOT_PAT",
                    "expected": {"visibility": "selected", "selected_repository_ids": [1, 2]}}

    def test_org_write_preserves_visibility_and_selected_ids(self):
        with patch.object(self.api, "request", return_value=204) as request:
            self.api.write(self.bot, "encrypted", "key")
            body = request.call_args.args[2]
            self.assertEqual(body["visibility"], "selected")
            self.assertEqual(body["selected_repository_ids"], [1, 2])
        self.bot["expected"] = {"visibility": "all", "selected_repository_ids": []}
        with patch.object(self.api, "request", return_value=204) as request:
            self.api.write(self.bot, "encrypted", "key")
            self.assertNotIn("selected_repository_ids", request.call_args.args[2])

    def test_selected_repository_pagination_complete_and_duplicate_refused(self):
        metadata = {"name": "BOT_PAT", "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z", "visibility": "selected"}
        pages = [{"total_count": 101, "repositories": [{"id": i} for i in range(1, 101)]},
                 {"total_count": 101, "repositories": [{"id": 101}]}]
        with patch.object(self.api, "request", side_effect=[metadata, *pages]) as request:
            self.assertEqual(self.api.secret(self.bot)["selected_repository_ids"], list(range(1, 102)))
            self.assertIn("page=2", request.call_args.args[1])
        pages[-1]["repositories"][0]["id"] = 1
        with patch.object(self.api, "request", side_effect=[metadata, *pages]):
            with self.assertRaises(ControlError):
                self.api.secret(self.bot)

    def transport(self, status, content=b"{}"):
        class Response:
            def __init__(self):
                self.status, self.content = status, content
            def read1(self, count):
                chunk, self.content = self.content[:count], self.content[count:]
                return chunk
        class Connection:
            def __init__(self, *_args, **_kwargs):
                self.sock = SimpleNamespace(settimeout=lambda _: None)
            def set_debuglevel(self, _):
                pass
            def request(self, *_args, **_kwargs):
                pass
            def getresponse(self):
                return Response()
            def close(self):
                pass
        return patch.object(gh.http.client, "HTTPSConnection", Connection)

    def test_redirects_and_unapproved_destinations_never_followed(self):
        with self.transport(302, b"sensitive response"):
            with self.assertRaises(ControlError) as error:
                self.api.actor()
            self.assertNotIn("sensitive", str(error.exception))
        with patch.object(gh.http.client, "HTTPSConnection", side_effect=AssertionError):
            with self.assertRaises(ControlError):
                self.api.request("GET", "https://attacker.invalid/")
            with self.assertRaises(ControlError):
                self.api.request("PUT", self.api.repo + "/actions/secrets/BOT_PAT", {})

    def test_finite_body_limit_and_no_success_body_required(self):
        with self.transport(200, b"x" * (gh.MAX_RESPONSE + 1)):
            with self.assertRaisesRegex(ControlError, "size limit"):
                self.api.actor()
        with self.transport(204, b"not json"):
            self.assertEqual(self.api.write(self.bot, "encrypted", "key"), 204)

    def test_full_repository_page_fits_bounded_transport(self):
        page = {"total_count": 100, "repositories": [
            {"id": i, "description": "synthetic metadata " * 400} for i in range(1, 101)]}
        body = json.dumps(page).encode()
        self.assertGreater(len(body), 128 * 1024)
        with self.transport(200, body):
            result = self.api.request("GET", self.api.org +
                                      "/actions/secrets/BOT_PAT/repositories?per_page=100&page=1")
        self.assertEqual(result["total_count"], 100)

    def test_response_deadline_and_transport_error_are_redacted(self):
        with self.transport(200), patch.object(gh.time, "monotonic", side_effect=[0, 46]):
            with self.assertRaisesRegex(ControlError, "timed out"):
                self.api.actor()
        with self.transport(200, b"sensitive-not-json"):
            with self.assertRaises(ControlError) as error:
                self.api.actor()
            self.assertNotIn("sensitive-not-json", str(error.exception))

    def test_real_library_and_hash_lock_match(self):
        import nacl
        root = Path(__file__).resolve().parents[1]
        direct = (root / "tools/secret_control/requirements-github.txt").read_text()
        locked = (root / "tools/secret_control/requirements-github-lock.txt").read_text()
        self.assertEqual(nacl.__version__, gh.PYNACL_VERSION)
        self.assertIn("PyNaCl==" + gh.PYNACL_VERSION, direct)
        self.assertIn("pynacl==" + gh.PYNACL_VERSION, locked)
        blocks = re.findall(r"(?ms)^([a-z][a-z0-9-]*)==([^\s]+)(.*?)(?=^[a-z]|\Z)", locked)
        self.assertEqual({name for name, _, _ in blocks}, {"pynacl", "cffi", "pycparser"})
        self.assertTrue(all("--hash=sha256:" in body for _, _, body in blocks))

    def test_cli_routes_publisher_without_age_or_implicit_apply(self):
        args = cli.parser().parse_args(["github-publish", "--manifest", "/example/manifest",
                                       "--manifest-sha256", "a" * 64,
                                       "--token-file", "/example/auth"])
        with patch.object(gh, "publish", return_value={"status": "planned"}) as publish:
            self.assertEqual(cli.execute(args), {"status": "planned"})
        self.assertFalse(publish.call_args.kwargs["apply"])
        self.assertIsNone(publish.call_args.kwargs["journal"])


if __name__ == "__main__":
    unittest.main()
