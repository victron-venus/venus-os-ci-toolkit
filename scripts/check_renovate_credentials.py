#!/usr/bin/env python3
"""Fail early when a classic bot token cannot update workflow files."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def validate_scopes(scope_header: str | None) -> bool:
    """Return whether classic workflow scope was verified; absence is unverified."""
    if scope_header is None:
        return False
    scopes = {scope.strip() for scope in scope_header.split(",")}
    if "workflow" not in scopes:
        raise ValueError(
            "BOT_PAT is missing the workflow scope. Update the organization secret "
            "with a bot token allowed to write repository contents and workflows. "
            "Renovate otherwise skips rejected pushes without failing its run."
        )
    return True


KEY_ERROR = "Signing key must use Ed25519, supported ECC >=256, or RSA >=2048"


def _key_command(args: list[str], data: bytes | None = None) -> str:
    """Inspect public metadata without forwarding tokens or printing key material."""
    environment = {
        name: os.environ[name] for name in ("PATH", "SYSTEMROOT") if name in os.environ
    }
    environment["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            args,
            input=data,
            capture_output=True,
            env=environment,
            timeout=15,
            check=True,
        )
        return result.stdout.decode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise ValueError(
            "Signing key inspection failed or the key is unsupported"
        ) from None


def _check_ssh_metadata(metadata: str) -> None:
    lines = metadata.splitlines()
    if len(lines) != 1:
        raise ValueError(KEY_ERROR)
    fields = lines[0].split()
    try:
        bits = int(fields[0])
        kind = fields[-1]
        fingerprint = fields[1]
    except (IndexError, ValueError):
        raise ValueError(KEY_ERROR) from None
    accepted = (
        (kind == "(ED25519)" and bits == 256)
        or (kind == "(ECDSA)" and bits in (256, 384, 521))
        or (kind == "(RSA)" and bits >= 2048)
    )
    if not accepted or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", fingerprint):
        raise ValueError(KEY_ERROR)


def _check_pgp_metadata(metadata: str) -> None:
    keys = [
        line.split(":")
        for line in metadata.splitlines()
        if line.startswith(("sec:", "ssb:"))
    ]
    if sum(fields[0] == "sec" for fields in keys) != 1:
        raise ValueError(KEY_ERROR)
    curves = {
        "nistp256": 256,
        "nistp384": 384,
        "nistp521": 521,
        "ed25519": 255,
        "cv25519": 255,
        "ed448": 448,
        "cv448": 448,
    }
    has_signer = False
    for fields in keys:
        try:
            bits, algorithm = int(fields[2]), int(fields[3])
            curve = fields[16]
            capabilities = fields[11]
        except (IndexError, ValueError):
            raise ValueError(KEY_ERROR) from None
        accepted = (algorithm in (1, 2, 3) and bits >= 2048) or (
            algorithm in (18, 19, 22) and curve in curves and bits == curves[curve]
        )
        if not accepted:
            raise ValueError(KEY_ERROR)
        has_signer |= "s" in capabilities.lower()
    if not has_signer:
        raise ValueError("Signing key has no supported signing capability")


def _ssh_key_metadata(path: Path, value: str) -> str:
    if not value.startswith("-----BEGIN OPENSSH PRIVATE KEY-----"):
        # PEM/PKCS8 does not carry a public envelope. Export only the public key;
        # a configured passphrase goes through stdin, never process arguments.
        passphrase = os.environ.get("RENOVATE_GIT_PRIVATE_KEY_PASSPHRASE", "")
        public = _key_command(
            ["openssl", "pkey", "-in", str(path), "-passin", "stdin", "-pubout"],
            (passphrase + "\n").encode("utf-8"),
        )
        path = path.with_suffix(".pub")
        path.write_text(public, encoding="utf-8")
        public = _key_command(["ssh-keygen", "-i", "-m", "PKCS8", "-f", str(path)])
        path.write_text(public, encoding="utf-8")
    return _key_command(["ssh-keygen", "-lf", str(path), "-E", "sha256"])


def validate_signing_key(value: str) -> None:
    """Check SSH or armored OpenPGP key strength without importing credentials."""
    if not value or len(value) > 1024 * 1024:
        raise ValueError("Signing key is empty or exceeds the inspection limit")
    # Renovate also accepts a canonical Base64 encoding of armored PGP material.
    try:
        decoded = base64.b64decode(value, validate=True)
        if base64.b64encode(decoded).decode("ascii") == value:
            value = decoded.decode("utf-8")
    except ValueError:
        pass
    value = value.strip()
    with tempfile.TemporaryDirectory(prefix="renovate-key-") as temporary:
        directory = Path(temporary)
        if value.startswith("-----BEGIN PGP PRIVATE KEY BLOCK-----"):
            metadata = _key_command(
                [
                    "gpg",
                    "--no-options",
                    "--no-autostart",
                    "--homedir",
                    str(directory),
                    "--batch",
                    "--no-tty",
                    "--with-colons",
                    "--import-options",
                    "show-only",
                    "--dry-run",
                    "--import",
                ],
                value.encode("utf-8"),
            )
            _check_pgp_metadata(metadata)
        elif value.startswith(
            (
                "-----BEGIN OPENSSH PRIVATE KEY-----",
                "-----BEGIN RSA PRIVATE KEY-----",
                "-----BEGIN EC PRIVATE KEY-----",
                "-----BEGIN PRIVATE KEY-----",
                "-----BEGIN ENCRYPTED PRIVATE KEY-----",
            )
        ):
            path = directory / "key"
            path.touch(mode=0o600)
            path.write_text(value + "\n", encoding="utf-8")
            # The OpenSSH public envelope is inspectable even for encrypted keys;
            # leave passphrase handling to Renovate and never put it in argv.
            _check_ssh_metadata(_ssh_key_metadata(path, value))
        else:
            raise ValueError("Expected an SSH or armored OpenPGP private signing key")


def main() -> None:
    for name in ("RENOVATE_TOKEN", "RENOVATE_GIT_PRIVATE_KEY"):
        if not os.environ.get(name):
            raise SystemExit(f"Missing required credential: {name}")
    try:
        validate_signing_key(os.environ["RENOVATE_GIT_PRIVATE_KEY"])
    except ValueError as error:
        raise SystemExit(str(error)) from None
    request = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {os.environ['RENOVATE_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            workflow_scope_verified = validate_scopes(
                response.headers.get("X-OAuth-Scopes")
            )
    except (urllib.error.URLError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print("Bot token authenticated; signing key strength is verified.")
    if workflow_scope_verified:
        print("The classic workflow scope is present.")
    else:
        print(
            "::warning::No X-OAuth-Scopes header was returned; workflow write permissions "
            "remain unverified. For a fine-grained token, confirm Contents and Workflows "
            "write permissions on every configured target repository."
        )


if __name__ == "__main__":
    main()
