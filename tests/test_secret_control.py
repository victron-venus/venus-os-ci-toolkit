"""Synthetic tests only: no credentials, cloud clients or real Terraform processes."""

import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import signal
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from tools.secret_control import catalog, hcp, oci_api, private_io
from tools.secret_control import __main__ as cli


def fixture():
    return {
        "schema_version": 1,
        "catalog_id": "6b5b6a90-f1af-4be7-8926-57e295617a88",
        "generation": 1,
        "entries": [
            {
                "id": "service-token",
                "revision": 1,
                "value": "synthetic-value",
                "targets": [
                    {
                        "type": "oci",
                        "region": "eu-example-1",
                        "compartment_id": "ocid1.compartment.oc1..example",
                        "vault_id": "ocid1.vault.oc1.eu-example-1.example",
                        "key_id": "ocid1.key.oc1.eu-example-1.example",
                        "name": "synthetic",
                    },
                    {
                        "type": "hcp",
                        "workspace_id": "ws-ExampleOnly12345",
                        "key": "EXAMPLE_TOKEN",
                        "category": "env",
                    },
                ],
            }
        ],
    }


class CatalogTests(unittest.TestCase):
    def test_valid_and_redacted(self):
        obj = fixture()
        self.assertEqual(catalog.decode(json.dumps(obj).encode()), obj)
        self.assertNotIn("synthetic-value", json.dumps(catalog.plan(obj)))
        self.assertNotIn("workspace_id", json.dumps(catalog.plan(obj)))

    def test_duplicate_keys_and_bad_numbers(self):
        for raw in (b'{"schema_version":1,"schema_version":1}', b'{"x":NaN}', b"\xff"):
            with self.assertRaises(catalog.ControlError):
                catalog.decode(raw)

    def test_exact_schema_limits(self):
        for mutate in (
            lambda c: c.update(extra=True),
            lambda c: c.update(generation=True),
            lambda c: c["entries"][0].update(revision=0),
            lambda c: c["entries"][0].update(value="x" * 16385),
            lambda c: c["entries"][0].update(value="a\x00b"),
            lambda c: c["entries"][0]["targets"][1].update(variable_set_id="varset-X"),
            lambda c: c["entries"][0]["targets"].append(
                copy.deepcopy(c["entries"][0]["targets"][0])
            ),
            lambda c: c["entries"][0]["targets"][0].update(region="--endpoint"),
        ):
            c = fixture()
            mutate(c)
            with self.assertRaises(catalog.ControlError):
                catalog.validate(c)

    def test_hcp_scope_suffix_length_in_isolation(self):
        for field, prefix in (("workspace_id", "ws-"), ("variable_set_id", "varset-")):
            for length in (15, 16, 17):
                with self.subTest(field=field, length=length):
                    value = fixture()
                    target = value["entries"][0]["targets"][1]
                    target.pop("workspace_id")
                    target[field] = prefix + "X" * length
                    if length == 16:
                        catalog.validate(value)
                    else:
                        with self.assertRaises(catalog.ControlError):
                            catalog.validate(value)

    def test_newline_empty_unicode_and_varset(self):
        for value in ("", "\n", "ユニコード"):
            c = fixture()
            c["entries"][0]["value"] = value
            t = c["entries"][0]["targets"][1]
            del t["workspace_id"]
            t["variable_set_id"] = "varset-ExampleOnly12345"
            catalog.validate(c)

    def test_lineage(self):
        old = fixture()
        catalog.lineage(old)
        new = copy.deepcopy(old)
        new["generation"] = 2
        catalog.lineage(new, old)
        new["entries"][0]["value"] = "changed"
        with self.assertRaises(catalog.ControlError):
            catalog.lineage(new, old)
        new["entries"][0]["revision"] = 2
        catalog.lineage(new, old)
        new["entries"] = []
        with self.assertRaises(catalog.ControlError):
            catalog.lineage(new, old)


class PrivateIOTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_overwrite_and_permissions(self):
        p = self.root / "cipher"
        private_io.new_private(p, b"cipher")
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)
        self.assertEqual(private_io.read_private(p), b"cipher")
        with self.assertRaises(FileExistsError):
            private_io.new_private(p, b"other")
        p.chmod(0o644)
        with self.assertRaises(catalog.ControlError):
            private_io.read_private(p)

    def test_symlink_hardlink_and_parent(self):
        p = self.root / "cipher"
        private_io.new_private(p, b"cipher")
        link = self.root / "link"
        link.symlink_to(p)
        with self.assertRaises(OSError):
            private_io.read_private(link)
        link.unlink()
        os.link(p, link)
        with self.assertRaises(catalog.ControlError):
            private_io.read_private(p)
        link.unlink()
        self.root.chmod(0o755)
        with self.assertRaises(catalog.ControlError):
            private_io.read_private(p)

    def test_ancestor_symlink(self):
        d = self.root / "real"
        d.mkdir(mode=0o700)
        private_io.new_private(d / "a", b"a")
        (self.root / "alias").symlink_to(d)
        with self.assertRaises(OSError):
            private_io.read_private(self.root / "alias" / "a")

    def test_writable_ancestor_refused_for_private_reads_and_writes(self):
        ancestor = self.root / "shared"
        ancestor.mkdir(mode=0o700)
        leaf = ancestor / "private"
        leaf.mkdir(mode=0o700)
        private_io.new_private(leaf / "input", b"synthetic")
        for mode in (0o770, 0o777):
            ancestor.chmod(mode)
            with self.subTest(mode=oct(mode)):
                with self.assertRaises(catalog.ControlError):
                    private_io.read_private(leaf / "input")
                with self.assertRaises(catalog.ControlError):
                    private_io.new_private(leaf / "output", b"synthetic")
                self.assertFalse((leaf / "output").exists())
        ancestor.chmod(0o700)

    def test_shared_system_temporary_parent_remains_supported(self):
        with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
            root = Path(temporary).resolve()
            root.chmod(0o700)
            private_io.new_private(root / "input", b"synthetic")
            self.assertEqual(private_io.read_private(root / "input"), b"synthetic")

    def test_changed_read_rejected(self):
        p = self.root / "cipher"
        private_io.new_private(p, b"cipher")
        real = os.read

        def changed(fd, n):
            data = real(fd, n)
            if data:
                p.write_bytes(b"change")
            return data

        with patch.object(private_io.os, "read", side_effect=changed):
            with self.assertRaises(catalog.ControlError):
                private_io.read_private(p)

    def test_env_scrub_and_home_preserved(self):
        original_home = os.environ.get("HOME")
        with patch.dict(
            os.environ,
            {"TF_LOG": "TRACE", "TF_CLI_ARGS": "-destroy", "PYTHONPATH": "x"},
        ):
            env = private_io.clean_env()
        self.assertEqual(env.get("HOME"), original_home)
        self.assertNotIn("TF_LOG", env)
        self.assertNotIn("TF_CLI_ARGS", env)
        self.assertNotIn("PYTHONPATH", env)

    def test_process_redaction_and_limits(self):
        with self.assertRaises(catalog.ControlError) as ctx:
            private_io.process(
                [
                    sys.executable,
                    "-c",
                    'import sys; print("synthetic-sensitive"); sys.exit(1)',
                ]
            )
        self.assertNotIn("synthetic-sensitive", str(ctx.exception))
        with self.assertRaises(catalog.ControlError):
            private_io.process([sys.executable, "-c", 'print("x"*10000)'], limit=100)

    def test_process_rejects_unconsumed_input(self):
        with self.assertRaises(catalog.ControlError):
            private_io.process(
                [sys.executable, "-c", "import os; os.close(0)"], b"x" * (1024 * 1024)
            )

    def test_timeout_reaps_descendants_after_leader_exit(self):
        pid_file = self.root / "child.pid"
        ready = self.root / "ready"
        release = self.root / "release"
        survived = self.root / "survived"
        child = (
            "from pathlib import Path; import time; "
            f"Path({str(ready)!r}).touch(); release=Path({str(release)!r})\n"
            "while not release.exists(): time.sleep(0.005)\n"
            f"Path({str(survived)!r}).touch(); time.sleep(30)"
        )
        leader = (
            "import subprocess,sys,time; from pathlib import Path; "
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
            f"Path({str(pid_file)!r}).write_text(str(p.pid)); "
            f"ready=Path({str(ready)!r})\n"
            "while not ready.exists(): time.sleep(0.005)\n"
        )
        try:
            with self.assertRaisesRegex(catalog.ControlError, "timeout"):
                private_io.process([sys.executable, "-c", leader], timeout=2)
            self.assertTrue(ready.exists())
            release.touch()
            time.sleep(0.7)
            self.assertFalse(survived.exists(), "descendant survived the tool timeout")
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_age_identity_fd_not_payload_argv(self):
        p = self.root / "cipher"
        k = self.root / "key"
        private_io.new_private(p, b"cipher")
        private_io.new_private(k, b"identity")
        with (
            patch.object(private_io, "trusted_tool", return_value="/trusted/age"),
            patch.object(private_io, "process", return_value=b"plain") as run,
        ):
            self.assertEqual(private_io.decrypt(p, k, "age"), b"plain")
            args, kw = run.call_args
            self.assertEqual(args[1], b"cipher")
            self.assertTrue(args[0][-1].startswith("/dev/fd/"))
            self.assertEqual(len(kw["pass_fds"]), 1)
            self.assertNotIn("identity", args[0])

    def test_decrypt_bytes_uses_supplied_ciphertext_without_reopening(self):
        key = self.root / "key"
        private_io.new_private(key, b"synthetic-identity")
        with (
            patch.object(private_io, "read_private", side_effect=AssertionError("unexpected reread")),
            patch.object(private_io, "trusted_tool", return_value="/trusted/age"),
            patch.object(private_io, "process", return_value=b"plain") as run,
        ):
            self.assertEqual(private_io.decrypt_bytes(b"exact-verified-ciphertext", key, "age"), b"plain")
        self.assertEqual(run.call_args.args[1], b"exact-verified-ciphertext")
        with patch.object(private_io, "open_private") as opened:
            for invalid in ("not bytes", b"x" * (catalog.MAX_CATALOG * 2 + 1)):
                with self.assertRaises(catalog.ControlError):
                    private_io.decrypt_bytes(invalid, key, "age")
            opened.assert_not_called()


class FakeOCI:
    def __init__(self, c, *, existing=True):
        self.c = c
        self.writes = []
        self.fail = False
        self.drift = False
        self.existing = existing
        e = c["entries"][0]
        t = e["targets"][0]
        self.value = e["value"].encode()
        self.etag = "v1"
        self.secret = NS(
            id="ocid1.vaultsecret.oc1..example",
            compartment_id=t["compartment_id"],
            vault_id=t["vault_id"],
            key_id=t["key_id"],
            secret_name=t["name"],
            lifecycle_state="ACTIVE",
            current_version_number=1,
            freeform_tags={
                oci_api.TAG + "managed": "1",
                oci_api.TAG + "catalog": c["catalog_id"],
                oci_api.TAG + "entry": e["id"],
                oci_api.TAG + "revision": str(e["revision"]),
            },
        )
        self.snapshots = 0

    def find(self, t):
        return self.secret.id if self.existing else None

    def snapshot(self, sid):
        self.snapshots += 1
        return self.secret, "drift" if self.drift and self.snapshots > 1 else self.etag

    def payload(self, sid, version):
        return self.value

    def advance(self, sid, tags, etag):
        self.writes.append(("advance", etag))
        self.secret.freeform_tags = tags
        return sid

    def write(self, t, value, tags, sid=None, etag=None):
        self.writes.append((sid, etag))
        if self.fail:
            raise RuntimeError("synthetic-sensitive-error")
        if sid and etag != self.etag:
            raise RuntimeError("precondition")
        self.value = value
        self.secret.freeform_tags = tags
        self.existing = True
        return self.secret.id


class OCITests(unittest.TestCase):
    def test_noop_and_plan_no_write(self):
        c = fixture()
        api = FakeOCI(c)
        out = oci_api.publish(c, lambda _: api, apply=True, generation=1)
        self.assertEqual(out["noop"], 1)
        self.assertEqual(api.writes, [])
        api = FakeOCI(c, existing=False)
        self.assertEqual(oci_api.publish(c, lambda _: api)["create"], 1)
        self.assertEqual(api.writes, [])

    def test_create_and_update_exact_cas(self):
        c = fixture()
        api = FakeOCI(c, existing=False)
        self.assertEqual(
            oci_api.publish(c, lambda _: api, apply=True, generation=1)["verified"], 1
        )
        self.assertEqual(api.writes, [(None, None)])
        c = fixture()
        api = FakeOCI(c)
        c["generation"] = 2
        c["entries"][0]["revision"] = 2
        c["entries"][0]["value"] = "changed"
        self.assertEqual(
            oci_api.publish(c, lambda _: api, apply=True, generation=2)["verified"], 1
        )
        self.assertEqual(api.writes, [(api.secret.id, "v1")])

    def test_metadata_advance_without_new_version(self):
        c = fixture()
        api = FakeOCI(c)
        c["generation"] = 2
        c["entries"][0]["revision"] = 2
        out = oci_api.publish(c, lambda _: api, apply=True, generation=2)
        self.assertEqual(out["advance"], 1)
        self.assertEqual(api.writes, [("advance", "v1")])
        self.assertEqual(api.secret.current_version_number, 1)

    def test_busy_readback_polls_only_reads(self):
        c = fixture()
        api = FakeOCI(c, existing=False)
        real = api.snapshot
        count = 0

        def snapshot(sid):
            nonlocal count
            count += 1
            api.secret.lifecycle_state = "CREATING" if count < 3 else "ACTIVE"
            return real(sid)

        api.snapshot = snapshot
        with patch.object(oci_api.time, "sleep") as sleep:
            out = oci_api.publish(c, lambda _: api, apply=True, generation=1)
        self.assertEqual(out["verified"], 1)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(sleep.call_count, 2)

    def test_busy_timeout_does_not_retry_write(self):
        c = fixture()
        api = FakeOCI(c, existing=False)
        api.secret.lifecycle_state = "CREATING"
        with patch.object(oci_api.time, "sleep"):
            with self.assertRaises(catalog.ControlError):
                oci_api.publish(c, lambda _: api, apply=True, generation=1)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(api.snapshots, 13)

    def test_managed_tag_names_follow_actual_service_constraints(self):
        value = fixture()
        api = FakeOCI(value, existing=False)
        oci_api.publish(value, lambda _: api, apply=True, generation=1)
        tags = api.secret.freeform_tags
        self.assertEqual(
            set(tags),
            {
                "secret-control-managed",
                "secret-control-catalog",
                "secret-control-entry",
                "secret-control-revision",
            },
        )
        for key in tags:
            self.assertTrue(0 < len(key) <= 100)
            self.assertTrue(all(33 <= ord(char) <= 126 and char != "." for char in key))

    def test_invalid_tags_refused_before_all_api_mutation_routes(self):
        # No SDK/client fields: validation must happen before model/API access.
        api = object.__new__(oci_api.SDK)
        target = fixture()["entries"][0]["targets"][0]
        invalid_keys = ("bad.key", "bad key", "bad\nkey", "é", "", "x" * 101)
        for key in invalid_keys:
            for mode in ("create", "update", "advance"):
                with self.subTest(key=repr(key), mode=mode):
                    with self.assertRaisesRegex(
                        catalog.ControlError, "invalid OCI freeform tag key"
                    ):
                        if mode == "advance":
                            api.advance("id", {key: "value"}, "etag")
                        elif mode == "update":
                            api.write(target, b"value", {key: "value"}, "id", "etag")
                        else:
                            api.write(target, b"value", {key: "value"})
        oci_api.validate_freeform_tags({"x" * 100: "v" * 256})
        for tags in ({str(i): "x" for i in range(11)}, {"key": "v" * 257}, {"key": 1}):
            with self.assertRaises(catalog.ControlError):
                oci_api.validate_freeform_tags(tags)

    def test_batch_error_exposes_only_status_and_known_code(self):
        class Failure(Exception):
            status = 404
            code = "NotAuthorizedOrNotFound"

        value = fixture()
        api = FakeOCI(value, existing=False)
        with patch.object(
            api, "write", side_effect=Failure("sensitive response body")
        ) as write:
            with self.assertRaises(catalog.ControlError) as error:
                oci_api.publish(value, lambda _: api, apply=True, generation=1)
            self.assertEqual(write.call_count, 1)
        self.assertIn("HTTP 404/NotAuthorizedOrNotFound", str(error.exception))
        self.assertNotIn("sensitive response body", str(error.exception))
        self.assertEqual(
            oci_api.safe_service_diagnostic(NS(status="private", code="private")), ""
        )

    def test_same_revision_diff_and_newer_remote_refused(self):
        for revision in ("1", "3"):
            c = fixture()
            api = FakeOCI(c)
            api.value = b"old"
            api.secret.freeform_tags[oci_api.TAG + "revision"] = revision
            with self.assertRaises(catalog.ControlError):
                oci_api.publish(c, lambda _: api, apply=True, generation=1)
            self.assertEqual(api.writes, [])

    def test_unmanaged_scope_lifecycle_and_drift_refused(self):
        for mutation in (
            lambda a: a.secret.freeform_tags.clear(),
            lambda a: setattr(a.secret, "key_id", "other"),
            lambda a: setattr(a.secret, "lifecycle_state", "DELETING"),
            lambda a: setattr(a, "drift", True),
        ):
            c = fixture()
            api = FakeOCI(c)
            mutation(api)
            with self.assertRaises(catalog.ControlError):
                oci_api.publish(c, lambda _: api, apply=True, generation=1)
            self.assertEqual(api.writes, [])

    def test_all_target_preflight_before_any_write(self):
        c = fixture()
        second = copy.deepcopy(c["entries"][0])
        second["id"] = "second"
        second["targets"] = [dict(second["targets"][0], name="second")]
        c["entries"].append(second)
        a = FakeOCI(c, existing=False)
        b = FakeOCI(c)
        b.secret.freeform_tags.clear()
        calls = iter([a, b])
        with self.assertRaises(catalog.ControlError):
            oci_api.publish(c, lambda _: next(calls), apply=True, generation=1)
        self.assertEqual(a.writes, [])

    def test_failed_write_once_redacted(self):
        c = fixture()
        api = FakeOCI(c, existing=False)
        api.fail = True
        with self.assertRaises(catalog.ControlError) as ctx:
            oci_api.publish(c, lambda _: api, apply=True, generation=1)
        self.assertEqual(len(api.writes), 1)
        self.assertNotIn("sensitive", str(ctx.exception))

    def test_partial_batch_stops_after_first_failed_write(self):
        c = fixture()
        second = copy.deepcopy(c["entries"][0])
        second["id"] = "second"
        second["targets"] = [
            dict(second["targets"][0], region="us-example-1", name="second")
        ]
        c["entries"].append(second)
        a = FakeOCI(c, existing=False)
        c2 = copy.deepcopy(c)
        c2["entries"] = [second]
        b = FakeOCI(c2, existing=False)
        b.fail = True
        with self.assertRaises(catalog.ControlError) as ctx:
            oci_api.publish(
                c,
                lambda region: a if region == "eu-example-1" else b,
                apply=True,
                generation=1,
            )
        self.assertIn("verified writes: 1", str(ctx.exception))
        self.assertEqual(len(a.writes), 1)
        self.assertEqual(len(b.writes), 1)

    def test_stale_active_readback_is_only_read_retry(self):
        c = fixture()
        api = FakeOCI(c, existing=False)
        original = api.payload
        calls = 0

        def payload(sid, version):
            nonlocal calls
            calls += 1
            return b"stale" if calls == 1 else original(sid, version)

        api.payload = payload
        with patch.object(oci_api.time, "sleep") as sleep:
            result = oci_api.publish(c, lambda _: api, apply=True, generation=1)
        self.assertEqual(result["verified"], 1)
        self.assertEqual(len(api.writes), 1)
        self.assertEqual(sleep.call_count, 1)

    def test_generation_required_before_reads(self):
        with self.assertRaises(catalog.ControlError):
            oci_api.publish(fixture(), lambda _: self.fail("no call"), apply=True)

    def test_strict_bundle(self):
        s = NS(
            secret_id="id",
            version_number=2,
            secret_bundle_content=NS(
                content_type="BASE64", content=base64.b64encode(b"x").decode()
            ),
        )
        self.assertEqual(oci_api.unpack(s, "id", 2), b"x")
        with self.assertRaises(catalog.ControlError):
            oci_api.unpack(s, "id", 1)
        s.secret_bundle_content.content = "!!!"
        with self.assertRaises(catalog.ControlError):
            oci_api.unpack(s, "id", 2)


class HCPTests(unittest.TestCase):
    def test_non_nfc_value_refused_before_local_or_remote_mutation(self):
        c = fixture()
        c["entries"][0]["value"] = "e\u0301"
        with patch.object(hcp, "verify_root") as verify, patch.object(hcp, "process") as process:
            with self.assertRaisesRegex(catalog.ControlError, "NFC-normalized"):
                hcp.run(c, self.dest, "tf", apply=True, generation=1)
        verify.assert_not_called()
        process.assert_not_called()
        c["entries"][0]["targets"] = [t for t in c["entries"][0]["targets"] if t["type"] == "oci"]
        self.assertEqual(hcp.mappings(c), ({}, {}))

    def test_json_string_transport_preserves_literal_templates(self):
        c = fixture()
        value = '${missing} %{ if true } $${escaped} %%{escaped} "quoted" \\path\n\u96ea\u00e9'
        c["entries"][0]["value"] = value
        for target in c["entries"][0]["targets"]:
            if target["type"] == "hcp":
                target["category"] = "terraform"
        with patch.object(hcp, "trusted_tool", return_value="/trusted/terraform"), patch.object(hcp, "process") as process:
            hcp.run(c, self.dest, "tf")
        env = process.call_args.kwargs["env"]
        self.assertNotIn("TF_VAR_hcp_secret_values", env)
        self.assertEqual(list(json.loads(env["TF_VAR_hcp_secret_values_json"]).values()), [value])

    def test_environment_newlines_refused_before_local_or_remote_mutation(self):
        for newline in ("\n", "\r", "\r\n"):
            with self.subTest(newline=repr(newline)):
                c = fixture()
                value = "first" + newline + "second"
                c["entries"][0]["value"] = value
                with patch.object(hcp, "verify_root") as verify, patch.object(hcp, "process") as process:
                    with self.assertRaisesRegex(catalog.ControlError, "cannot contain CR or LF"):
                        hcp.run(c, self.dest, "tf", apply=True, generation=1)
                verify.assert_not_called()
                process.assert_not_called()
                for target in c["entries"][0]["targets"]:
                    if target["type"] == "hcp":
                        target["category"] = "terraform"
                _, values = hcp.mappings(c)
                self.assertEqual(list(values.values()), [value])
                c["entries"][0]["targets"] = [t for t in c["entries"][0]["targets"] if t["type"] == "oci"]
                self.assertEqual(hcp.mappings(c), ({}, {}))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.dest = self.root / "hcp"
        hcp.prepare(self.dest)
        (self.dest / ".terraform").mkdir(mode=0o700)
        (self.dest / ".terraform/terraform.tfstate").write_text(
            json.dumps(
                {
                    "backend": {
                        "type": "local",
                        "config": {"path": None, "workspace_dir": None},
                    }
                }
            )
        )
        lock = self.dest / ".terraform.lock.hcl"
        if not lock.exists():
            lock.write_text(
                'provider "registry.terraform.io/hashicorp/tfe" { version = "0.76.0" }'
            )

    def tearDown(self):
        self.tmp.cleanup()

    def test_prepare_exclusive_and_hcp_only(self):
        with self.assertRaises(FileExistsError):
            hcp.prepare(self.dest)
        self.assertFalse((self.dest / "oci").exists())
        self.assertIn("tfe_variable", (self.dest / "main.tf").read_text())

    def test_multitarget_stable_maps(self):
        c = fixture()
        e = c["entries"][0]
        e["id"] = "a" * 64
        e["targets"].append(
            {
                "type": "hcp",
                "variable_set_id": "varset-ExampleOnly12345",
                "key": "OTHER",
                "category": "env",
            }
        )
        m, v = hcp.mappings(c)
        self.assertEqual(set(m), set(v))
        self.assertEqual(len(m), 2)
        self.assertTrue(all(len(k) <= 64 for k in m))
        self.assertTrue(all(x == "synthetic-value" for x in v.values()))

    def test_fixed_action_scrub_and_no_output(self):
        c = fixture()
        with (
            patch.object(hcp, "trusted_tool", return_value="/trusted/terraform"),
            patch.object(hcp, "process", return_value=b"synthetic-sensitive") as run,
            patch.dict(
                os.environ,
                {
                    "TF_LOG": "TRACE",
                    "TF_CLI_ARGS": "-destroy",
                    "TFE_TOKEN": "synthetic-auth",
                },
            ),
        ):
            result = hcp.run(c, self.dest, "tf", apply=True, generation=1)
        args, kw = run.call_args
        self.assertEqual(
            args[0],
            [
                "/trusted/terraform",
                "apply",
                "-input=false",
                "-lock=true",
                "-no-color",
                "-auto-approve",
            ],
        )
        self.assertNotIn("TF_LOG", kw["env"])
        self.assertNotIn("TF_CLI_ARGS", kw["env"])
        self.assertEqual(kw["env"]["TFE_TOKEN"], "synthetic-auth")
        self.assertNotIn("sensitive", str(result))
        for p in self.dest.iterdir():
            if p.is_file():
                self.assertNotIn(b"synthetic-value", p.read_bytes())

    def test_drift_extra_code_and_revision_refused(self):
        (self.dest / "extra.tf").write_text("bad")
        with self.assertRaises(catalog.ControlError):
            hcp.verify_root(self.dest)
        (self.dest / "extra.tf").unlink()
        c = fixture()
        m, v = hcp.mappings(c)
        hcp.record_intent(self.dest, c, m, v, True)
        c["entries"][0]["value"] = "changed"
        m, v = hcp.mappings(c)
        with self.assertRaises(catalog.ControlError):
            hcp.record_intent(self.dest, c, m, v, False)

    def test_remote_backend_and_unrelated_state_refused(self):
        c = fixture()
        m, _ = hcp.mappings(c)
        backend = self.dest / ".terraform/terraform.tfstate"
        backend.write_text(json.dumps({"backend": {"type": "remote", "config": {}}}))
        with self.assertRaises(catalog.ControlError):
            hcp.verify_state(self.dest, m)
        backend.write_text(json.dumps({"backend": {"type": "local", "config": {}}}))
        (self.dest / "terraform.tfstate").write_text(
            json.dumps(
                {
                    "resources": [
                        {"type": "oci_kms_vault", "name": "main", "mode": "managed"}
                    ]
                }
            )
        )
        with self.assertRaises(catalog.ControlError):
            hcp.verify_state(self.dest, m)

    def test_alternate_workspace_refused(self):
        (self.dest / ".terraform/environment").write_text("alternate")
        m, _ = hcp.mappings(fixture())
        with self.assertRaises(catalog.ControlError):
            hcp.verify_state(self.dest, m)

    def test_lock_precedes_configuration_read(self):
        events = []
        original = hcp.read_private

        def read(*args, **kwargs):
            events.append("read")
            return original(*args, **kwargs)

        with (
            patch.object(
                hcp.fcntl, "flock", side_effect=lambda *_: events.append("lock")
            ),
            patch.object(hcp, "read_private", side_effect=read),
        ):
            _, fd = hcp.verify_root(self.dest)
            os.close(fd)
        self.assertEqual(events[0], "lock")

    def test_foreign_instance_and_deposed_state_refused(self):
        c = fixture()
        m, _ = hcp.mappings(c)
        index = next(iter(m))
        attributes = dict(m[index], value_wo_version=1)
        attributes.pop("revision")
        instance = {"index_key": index, "attributes": attributes}
        state = {
            "version": 4,
            "resources": [
                {
                    "mode": "managed",
                    "type": "tfe_variable",
                    "name": "secret",
                    "instances": [instance],
                }
            ],
        }
        statefile = self.dest / "terraform.tfstate"
        statefile.write_text(json.dumps(state))
        hcp.verify_state(self.dest, m)
        instance["index_key"] = "foreign"
        statefile.write_text(json.dumps(state))
        with self.assertRaises(catalog.ControlError):
            hcp.verify_state(self.dest, m)
        instance["index_key"] = index
        instance["deposed"] = "old"
        statefile.write_text(json.dumps(state))
        with self.assertRaises(catalog.ControlError):
            hcp.verify_state(self.dest, m)
        del instance["deposed"]
        attributes["value"] = "legacy-stateful-secret"
        statefile.write_text(json.dumps(state))
        with self.assertRaises(catalog.ControlError):
            hcp.verify_state(self.dest, m)

    def test_internal_symlink_refused(self):
        p = self.dest / ".terraform/terraform.tfstate"
        p.unlink()
        p.symlink_to("/nonexistent")
        with self.assertRaises(OSError):
            hcp.internal_file(self.dest, ".terraform/terraform.tfstate")

    def test_dangling_state_or_workspace_link_refused_before_process(self):
        for relative in ("terraform.tfstate", ".terraform/environment"):
            with self.subTest(relative=relative):
                path = self.dest / relative
                path.symlink_to(self.root / "absent-target")
                try:
                    with patch.object(hcp, "trusted_tool", return_value="/trusted/terraform"), patch.object(hcp, "process") as process:
                        with self.assertRaises(OSError):
                            hcp.run(fixture(), self.dest, "tf", apply=True, generation=1)
                    process.assert_not_called()
                    self.assertFalse(list(self.dest.glob(".secret-control-intent-*.json")))
                finally:
                    path.unlink()

    def test_dangling_ledger_key_is_not_treated_as_absent(self):
        (self.dest / ".secret-control-ledger-key").symlink_to(self.root / "missing-key")
        with patch.object(hcp, "trusted_tool", return_value="/trusted/terraform"), patch.object(hcp, "process") as process:
            with self.assertRaises(OSError):
                hcp.run(fixture(), self.dest, "tf")
        process.assert_not_called()

    def test_ledger_hmac_not_bare_secret_hash(self):
        c = fixture()
        m, v = hcp.mappings(c)
        hcp.record_intent(self.dest, c, m, v, True)
        raw = (self.dest / ".secret-control-intent-1.json").read_bytes()
        self.assertNotIn(hashlib.sha256(b"synthetic-value").hexdigest().encode(), raw)
        self.assertTrue((self.dest / ".secret-control-ledger-key").is_file())

    def test_existing_state_no_adoption(self):
        (self.dest / "terraform.tfstate").write_text("{}")
        c = fixture()
        m, v = hcp.mappings(c)
        with self.assertRaises(catalog.ControlError):
            hcp.record_intent(self.dest, c, m, v, False)


class SDKAdapterTests(unittest.TestCase):
    def test_models_retry_cas_and_exact_version(self):
        calls = []

        def update(sid, details, **kwargs):
            calls.append(("update", sid, details, kwargs))
            return NS(data=NS())

        def create(details, **kwargs):
            calls.append(("create", details, kwargs))
            return NS(data=NS(id="created"))

        def bundle(sid, **kwargs):
            calls.append(("bundle", sid, kwargs))
            return NS(
                data=NS(
                    secret_id=sid,
                    version_number=kwargs["version_number"],
                    secret_bundle_content=NS(content_type="BASE64", content="eA=="),
                )
            )

        models = NS(
            Base64SecretContentDetails=lambda **kw: NS(**kw),
            CreateSecretDetails=lambda **kw: NS(**kw),
            UpdateSecretDetails=lambda **kw: NS(**kw),
        )
        retry = object()
        fake = NS(
            __version__=oci_api.SDK_VERSION,
            vault=NS(models=models),
            retry=NS(NoneRetryStrategy=lambda: retry),
            auth=NS(signers=NS(InstancePrincipalsSecurityTokenSigner=lambda: object())),
        )

        def vault(cfg, **kwargs):
            self.assertEqual(cfg, {"region": "eu-example-1"})
            self.assertIs(kwargs["retry_strategy"], retry)
            return NS(update_secret=update, create_secret=create)

        fake.vault.VaultsClient = vault
        fake.secrets = NS(SecretsClient=lambda cfg, **kw: NS(get_secret_bundle=bundle))
        import logging

        previous = logging.root.manager.disable
        try:
            with patch.object(oci_api.importlib, "import_module", return_value=fake):
                api = oci_api.SDK("eu-example-1", "instance-principal")
            t = fixture()["entries"][0]["targets"][0]
            self.assertEqual(api.write(t, b"x", {"managed": "1"}), "created")
            api.write(t, b"y", {}, "sid", "etag")
            api.advance("sid", {}, "new-etag")
            self.assertEqual(api.payload("sid", 9), b"x")
        finally:
            logging.disable(previous)
        self.assertEqual(calls[0][1].secret_content.content, "eA==")
        self.assertEqual(calls[0][1].secret_content.stage, "CURRENT")
        self.assertEqual(calls[1][-1], {"if_match": "etag"})
        self.assertFalse(hasattr(calls[2][2], "secret_content"))
        self.assertEqual(calls[3], ("bundle", "sid", {"version_number": 9}))

    def test_pagination_duplicate_refused(self):
        api = object.__new__(oci_api.SDK)
        target = fixture()["entries"][0]["targets"][0]
        pages = iter(
            [
                NS(
                    data=[NS(id="one", secret_name="synthetic")],
                    headers={"opc-next-page": "two"},
                ),
                NS(data=[NS(id="two", secret_name="synthetic")], headers={}),
            ]
        )
        api.vault = NS(list_secrets=lambda *a, **kw: next(pages))
        with self.assertRaises(catalog.ControlError):
            api.find(target)


class CLITests(unittest.TestCase):
    def test_help_no_sdk(self):
        with patch("sys.stdout", new=io.StringIO()):
            with patch.object(
                oci_api.importlib,
                "import_module",
                side_effect=AssertionError("no import"),
            ):
                with self.assertRaises(SystemExit) as out:
                    cli.main(["--help"])
            self.assertEqual(out.exception.code, 0)

    def test_bad_argument_does_not_echo(self):
        with patch("sys.stderr", new=io.StringIO()) as err:
            self.assertEqual(cli.main(["synthetic-sensitive"]), 2)
            self.assertNotIn("synthetic-sensitive", err.getvalue())

    def test_fetch_invalid_pin_before_sdk(self):
        with (
            patch.object(oci_api, "SDK", side_effect=AssertionError("no SDK")),
            patch("sys.stderr", new=io.StringIO()),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "fetch",
                        "--region",
                        "eu-example-1",
                        "--secret-id",
                        "ocid1.vaultsecret.oc1..example",
                        "--version-number",
                        "1",
                        "--expected-sha256",
                        "invalid",
                        "--output",
                        "/no",
                        "--auth",
                        "instance-principal",
                    ]
                ),
                2,
            )

    def test_fetch_materialize(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d).resolve()
            p.chmod(0o700)
            dest = p / "output"
            data = b"synthetic"
            with (
                patch.object(oci_api, "SDK", return_value=NS(payload=lambda *_: data)),
                patch("sys.stdout", new=io.StringIO()),
            ):
                code = cli.main(
                    [
                        "fetch",
                        "--region",
                        "eu-example-1",
                        "--secret-id",
                        "ocid1.vaultsecret.oc1..example",
                        "--version-number",
                        "1",
                        "--expected-sha256",
                        hashlib.sha256(data).hexdigest(),
                        "--output",
                        str(dest),
                        "--auth",
                        "instance-principal",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(dest.read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
