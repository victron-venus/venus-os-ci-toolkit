"""Real pinned OCI SDK constructors; synthetic RSA credentials, no network."""

import contextlib
import importlib.util
import logging
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from tools.secret_control import oci_api
from tools.secret_control.private_io import new_private


@unittest.skipUnless(importlib.util.find_spec("oci"), "install pinned optional OCI SDK")
class RealOCIInitializationTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("connect", "connect_ex"):
            self.stack.enter_context(
                patch.object(
                    socket.socket, name, side_effect=AssertionError("network forbidden")
                )
            )
        for name in ("create_connection", "getaddrinfo"):
            self.stack.enter_context(
                patch.object(
                    socket, name, side_effect=AssertionError("network forbidden")
                )
            )
        import oci
        from oci._vendor import requests
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.oci = oci
        self.assertEqual(oci.__version__, oci_api.SDK_VERSION)
        self.stack.enter_context(
            patch.object(
                requests.sessions.Session,
                "send",
                side_effect=AssertionError("HTTP forbidden"),
            )
        )
        previous_logging = logging.root.manager.disable
        self.addCleanup(logging.disable, previous_logging)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.root.chmod(0o700)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.key_path = self.root / "synthetic-key.pem"
        new_private(self.key_path, self.pem)
        self.config_path = self.root / "synthetic-config"
        self.tenancy = "ocid1.tenancy.oc1..syntheticonly"
        self.user = "ocid1.user.oc1..syntheticonly"
        self.fingerprint = ":".join(["ab"] * 16)
        new_private(
            self.config_path,
            (
                "[DEFAULT]\n"
                f"tenancy={self.tenancy}\nuser={self.user}\n"
                f"fingerprint={self.fingerprint}\nkey_file={self.key_path}\n"
                "region=ignored-region\nendpoint=https://unused.invalid\n"
                "log_requests=true\nadditional_user_agent=not-forwarded\n"
                "key_content=not-forwarded\n"
            ).encode(),
        )

    def test_real_clients_accept_whitelisted_in_memory_config(self):
        read = oci_api.read_private
        with patch.object(oci_api, "read_private", wraps=read) as reader:
            api = oci_api.SDK("us-ashburn-1", "api-key", self.config_path)
        self.assertIsInstance(api.vault, self.oci.vault.VaultsClient)
        self.assertIsInstance(api.secrets, self.oci.secrets.SecretsClient)
        self.assertEqual(
            sum(call.args[0] == self.key_path for call in reader.call_args_list), 1
        )
        for client in (api.vault, api.secrets):
            self.addCleanup(client.base_client.session.close)
            config = client.base_client.config
            self.assertEqual(
                set(config), {"region", "user", "tenancy", "fingerprint", "key_content"}
            )
            self.assertEqual(config["region"], "us-ashburn-1")
            self.assertEqual(config["user"], self.user)
            self.assertEqual(config["tenancy"], self.tenancy)
            self.assertEqual(config["key_content"], self.pem.decode())
            self.assertIsInstance(
                client.retry_strategy, self.oci.retry.NoneRetryStrategy
            )
            self.assertNotIn("unused.invalid", client.base_client.endpoint)

    def test_original_region_only_config_fails_with_basic_signer(self):
        signer = self.oci.signer.Signer(
            self.tenancy,
            self.user,
            self.fingerprint,
            private_key_file_location=None,
            private_key_content=self.pem.decode(),
        )
        for client in (self.oci.vault.VaultsClient, self.oci.secrets.SecretsClient):
            with self.subTest(client=client.__name__):
                with self.assertRaises(self.oci.exceptions.InvalidConfig):
                    client({"region": "us-ashburn-1"}, signer=signer)

    def test_network_guard_is_active(self):
        with self.assertRaisesRegex(AssertionError, "network forbidden"):
            socket.create_connection(("127.0.0.1", 1))


if __name__ == "__main__":
    unittest.main()
