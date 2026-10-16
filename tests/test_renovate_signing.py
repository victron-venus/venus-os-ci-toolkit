"""Real-key regressions for the Renovate signing preflight."""

import base64
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "scripts.renovate_credentials", ROOT / "scripts/check_renovate_credentials.py"
)
credentials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(credentials)


class RenovateSigningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # GnuPG agent Unix sockets need a short path on macOS as well as Linux.
        cls.temporary = tempfile.TemporaryDirectory(prefix="renovate-test-", dir="/tmp")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        for tool in ("gpg", "gpgconf", "openssl", "ssh-keygen"):
            if not shutil.which(tool):
                raise AssertionError(
                    f"Required signing inspection tool missing: {tool}"
                )
        for args in (
            ["gpg", "--version"],
            ["gpgconf", "--version"],
            ["openssl", "version"],
        ):
            result = subprocess.run(
                args, capture_output=True, text=True, check=True, timeout=15
            )
            print("Signing test runtime: " + result.stdout.splitlines()[0], flush=True)
        print("Signing test runtime: " + str(shutil.which("ssh-keygen")), flush=True)
        cls.ssh = {}
        for label, algorithm, bits, passphrase in (
            ("ed25519", "ed25519", 256, ""),
            ("encrypted", "ed25519", 256, "synthetic fixture passphrase"),
            ("ecdsa256", "ecdsa", 256, ""),
            ("ecdsa384", "ecdsa", 384, ""),
            ("ecdsa521", "ecdsa", 521, ""),
            ("rsa2048", "rsa", 2048, ""),
            ("rsa1024", "rsa", 1024, ""),
        ):
            path = cls.root / label
            subprocess.run(
                [
                    "ssh-keygen",
                    "-q",
                    "-t",
                    algorithm,
                    "-b",
                    str(bits),
                    "-N",
                    passphrase,
                    "-C",
                    "synthetic-fixture-only",
                    "-f",
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=30,
            )
            cls.ssh[label] = path.read_text()
        # Random Ed448 generation can fail in libgcrypt's keygrip conversion
        # when the native public point starts with 0x02/0x03. Keep inspection
        # and real signing coverage without retrying that unrelated generator.
        # Provenance and upstream code: tests/fixtures/signing/README.md.
        cls.pgp = {
            "ed448": (ROOT / "tests/fixtures/signing/ed448-synthetic.asc").read_text()
        }
        for algorithm in ("rsa1024", "rsa2048", "ed25519", "nistp256"):
            home = cls.root / (algorithm + "-pgp")
            home.mkdir(mode=0o700)
            args = [
                "gpg",
                "--no-options",
                # Explicitly select GnuPG's legacy Ed448 fixture format;
                # this does not modify global settings or skip real key tests.
                "--compliance=gnupg",
                "--homedir",
                str(home),
                "--batch",
                "--no-tty",
            ]
            cls.addClassCleanup(
                subprocess.run,
                ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
                capture_output=True,
                timeout=15,
            )
            generated = subprocess.run(
                args
                + [
                    "--pinentry-mode",
                    "loopback",
                    "--passphrase",
                    "",
                    "--quick-generate-key",
                    "Synthetic Test <fixture@example.invalid>",
                    algorithm,
                    "sign",
                    "0",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            if generated.returncode:
                raise AssertionError(
                    f"Cannot generate synthetic {algorithm} fixture: "
                    + generated.stderr.decode("utf-8", errors="replace")
                )
            cls.pgp[algorithm] = subprocess.check_output(
                args + ["--armor", "--export-secret-keys"],
                stderr=subprocess.PIPE,
                timeout=15,
            ).decode()
            if algorithm in {"rsa2048", "ed25519", "nistp256"}:
                subkey, usage, label = {
                    "rsa2048": ("rsa1024", "sign", "weak-subkey"),
                    "ed25519": ("cv25519", "encr", "ed25519-ecdh"),
                    "nistp256": ("nistp384", "encr", "nist-ecdh"),
                }[algorithm]
                metadata = subprocess.check_output(
                    args + ["--with-colons", "--list-keys"],
                    stderr=subprocess.PIPE,
                    timeout=15,
                ).decode()
                fingerprint = next(
                    line.split(":")[9]
                    for line in metadata.splitlines()
                    if line.startswith("fpr:")
                )
                subprocess.run(
                    args
                    + [
                        "--pinentry-mode",
                        "loopback",
                        "--passphrase",
                        "",
                        "--quick-add-key",
                        fingerprint,
                        subkey,
                        usage,
                        "0",
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                cls.pgp[label] = subprocess.check_output(
                    args + ["--armor", "--export-secret-keys"],
                    stderr=subprocess.PIPE,
                    timeout=15,
                ).decode()

    def test_accepts_supported_real_ssh_keys(self):
        for name, value in self.ssh.items():
            if name != "rsa1024":
                with self.subTest(name=name):
                    credentials.validate_signing_key(value)

    def test_ed448_fixture_signs_and_rejects_tampered_content(self):
        """The public test-only fixture must remain a usable Ed448 signing key."""
        with tempfile.TemporaryDirectory(prefix="ed448-test-", dir="/tmp") as home:
            args = [
                "gpg", "--no-options", "--compliance=gnupg", "--homedir", home,
                "--batch", "--no-tty", "--pinentry-mode", "loopback",
                "--passphrase", "",
            ]
            try:
                subprocess.run(
                    args + ["--import"], input=self.pgp["ed448"].encode(),
                    check=True, capture_output=True, timeout=15,
                )
                metadata = subprocess.check_output(
                    args + ["--with-colons", "--list-secret-keys"],
                    stderr=subprocess.PIPE, timeout=15,
                ).decode()
                credentials._check_pgp_metadata(metadata)
                fingerprint = (
                    "CA45CAE879F19EAAF88BD03A5A7234CCD3A0E3BE6B42A0FFD43B3253497F2A26"
                )
                self.assertIn(f"fpr:::::::::{fingerprint}:", metadata)
                message = Path(home) / "message.txt"
                signature = Path(home) / "message.sig"
                message.write_bytes(b"Public synthetic Ed448 fixture regression\n")
                subprocess.run(
                    args + ["--local-user", fingerprint, "--output", str(signature),
                            "--detach-sign", str(message)],
                    check=True, capture_output=True, timeout=15,
                )
                verified = subprocess.run(
                    args + ["--status-fd", "1", "--verify", str(signature), str(message)],
                    check=True, capture_output=True, timeout=15,
                )
                self.assertIn(f"[GNUPG:] VALIDSIG {fingerprint} ".encode(), verified.stdout)
                message.write_bytes(b"Tampered fixture regression\n")
                rejected = subprocess.run(
                    args + ["--status-fd", "1", "--verify", str(signature), str(message)],
                    check=False, capture_output=True, timeout=15,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(b"[GNUPG:] BADSIG ", rejected.stdout)
            finally:
                subprocess.run(
                    ["gpgconf", "--homedir", home, "--kill", "gpg-agent"],
                    capture_output=True, timeout=15, check=True,
                )

    def test_pem_rsa_with_and_without_passphrase(self):
        for passphrase in ("", "synthetic-fixture-passphrase"):
            with (
                self.subTest(encrypted=bool(passphrase)),
                tempfile.TemporaryDirectory() as tmp,
            ):
                path = Path(tmp) / "key"
                subprocess.run(
                    [
                        "ssh-keygen",
                        "-q",
                        "-t",
                        "rsa",
                        "-b",
                        "2048",
                        "-m",
                        "PEM",
                        "-N",
                        passphrase,
                        "-f",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                with patch.dict(
                    os.environ, {"RENOVATE_GIT_PRIVATE_KEY_PASSPHRASE": passphrase}
                ):
                    credentials.validate_signing_key(path.read_text())

    def test_rejects_real_weak_ssh_key(self):
        with self.assertRaisesRegex(ValueError, "RSA >=2048"):
            credentials.validate_signing_key(self.ssh["rsa1024"])

    def test_accepts_supported_real_pgp_keys(self):
        for name in (
            "rsa2048",
            "ed25519",
            "ed448",
            "nistp256",
            "ed25519-ecdh",
            "nist-ecdh",
        ):
            with self.subTest(name=name):
                credentials.validate_signing_key(self.pgp[name])

    def test_ed448_representations_and_encryption_subkey_metadata(self):
        """Accept known GnuPG representations without mixing curves and algorithms."""

        def metadata(curve, bits, algorithm, kind="sec", capabilities="sc"):
            fields = [""] * 18
            fields[0], fields[1] = kind, "u"
            fields[2], fields[3] = str(bits), str(algorithm)
            fields[11], fields[16] = capabilities, curve
            return ":".join(fields) + "\n"

        for bits in (448, 456):
            credentials._check_pgp_metadata(metadata("ed448", bits, 22))
        for primary, subkey in (
            (("ed25519", 255, 22), ("cv25519", 255, 18)),
            (("ed448", 456, 22), ("cv448", 448, 18)),
            (("nistp256", 256, 19), ("nistp384", 384, 18)),
        ):
            credentials._check_pgp_metadata(
                metadata(*primary) + metadata(*subkey, kind="ssb", capabilities="e")
            )
        for curve, bits, algorithm in (
            ("ed448", 447, 22),
            ("ed448", 449, 22),
            ("ed448", 455, 22),
            ("ed448", 457, 22),
            ("ed448", 448, 18),
            ("ed448", 456, 19),
            ("ed25519", 255, 19),
            ("cv25519", 255, 22),
            ("cv448", 456, 18),
            ("nistp256", 256, 22),
        ):
            with (
                self.subTest(curve=curve, bits=bits, algorithm=algorithm),
                self.assertRaises(ValueError),
            ):
                credentials._check_pgp_metadata(metadata(curve, bits, algorithm))

    def test_accepts_base64_pgp_as_pinned_renovate_does(self):
        value = base64.b64encode(self.pgp["ed25519"].encode()).decode()
        credentials.validate_signing_key(value)

    def test_rejects_weak_pgp_primary_and_subkeys(self):
        for name in ("rsa1024", "weak-subkey"):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "RSA >=2048"),
            ):
                credentials.validate_signing_key(self.pgp[name])

    def test_rejects_multiple_pgp_identities(self):
        with self.assertRaisesRegex(ValueError, "RSA >=2048"):
            credentials.validate_signing_key(self.pgp["ed25519"] + self.pgp["rsa2048"])

    def test_rejects_nonprivate_unsupported_and_malformed_values(self):
        for value in (
            "",
            "fixture-secret",
            "x" * (1024 * 1024 + 1),
            "-----BEGIN PGP PUBLIC KEY BLOCK-----\nnot-a-key",
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nfixture-secret",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nfixture-secret",
        ):
            with self.subTest(size=len(value)), self.assertRaises(ValueError) as caught:
                credentials.validate_signing_key(value)
            self.assertNotIn("fixture-secret", str(caught.exception))

    def test_temporary_material_is_removed_after_success_and_failure(self):
        with tempfile.TemporaryDirectory() as parent:
            factory = tempfile.TemporaryDirectory
            with patch.object(
                credentials.tempfile,
                "TemporaryDirectory",
                side_effect=lambda **kw: factory(dir=parent, **kw),
            ):
                credentials.validate_signing_key(self.ssh["ed25519"])
                self.assertEqual(list(Path(parent).iterdir()), [])
                with self.assertRaises(ValueError):
                    credentials.validate_signing_key(self.ssh["rsa1024"])
                self.assertEqual(list(Path(parent).iterdir()), [])

    def test_weak_key_is_rejected_before_github_request(self):
        with (
            patch.dict(
                os.environ,
                {
                    "RENOVATE_TOKEN": "test-token",
                    "RENOVATE_GIT_PRIVATE_KEY": self.ssh["rsa1024"],
                },
            ),
            patch.object(credentials.urllib.request, "urlopen") as request,
        ):
            with self.assertRaisesRegex(SystemExit, "RSA >=2048"):
                credentials.main()
            request.assert_not_called()

    def test_external_failure_is_bounded_and_does_not_echo_secrets(self):
        for error in (
            FileNotFoundError("fixture-secret"),
            subprocess.TimeoutExpired(["tool"], 15, output=b"fixture-secret"),
            subprocess.CalledProcessError(1, ["tool"], stderr=b"fixture-secret"),
        ):
            with (
                patch.dict(
                    os.environ,
                    {
                        "RENOVATE_TOKEN": "test-token",
                        "RENOVATE_GIT_PRIVATE_KEY": "fixture-secret",
                    },
                ),
                patch.object(credentials.subprocess, "run", side_effect=error) as run,
            ):
                with self.assertRaises(ValueError) as caught:
                    credentials._key_command(["tool"])
                self.assertNotIn("fixture-secret", str(caught.exception))
                self.assertEqual(run.call_args.kwargs["timeout"], 15)
                self.assertNotIn("RENOVATE_TOKEN", run.call_args.kwargs["env"])
                self.assertNotIn(
                    "RENOVATE_GIT_PRIVATE_KEY", run.call_args.kwargs["env"]
                )

    def test_invalid_metadata_fails_closed(self):
        for metadata in (
            "",
            "bad",
            "256 SHA256:" + "a" * 43 + " (DSA)",
            "128 SHA256:" + "a" * 43 + " (ECDSA)",
            "256 SHA256:" + "a" * 43 + " (ED25519)\nextra",
        ):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                credentials._check_ssh_metadata(metadata)
        for metadata in ("", "sec:bad", "sec:u:1024:17:key:::::::s:::::dsa:\n"):
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                credentials._check_pgp_metadata(metadata)


if __name__ == "__main__":
    unittest.main()
