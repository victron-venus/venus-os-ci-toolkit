#!/usr/bin/env python3
"""Verify the upstream signed fixture and tamper rejection; never publish/sign."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def check(binary: Path) -> None:
    binary = binary.resolve()
    fixture = ROOT / "tests/fixtures/cosign"
    with tempfile.TemporaryDirectory(prefix="cosign-validation-") as directory:
        temporary = Path(directory)
        env = {**os.environ, "HOME": directory, "GODEBUG": "fips140=on"}
        subprocess.run([str(binary), "initialize"], env=env, check=True)
        command = [str(binary), "verify-blob", "--bundle",
                   str(fixture / "cosign_checksums.txt.sigstore.json"),
                   "--certificate-identity", "keyless@projectsigstore.iam.gserviceaccount.com",
                   "--certificate-oidc-issuer", "https://accounts.google.com"]
        source = fixture / "cosign_checksums.txt"
        subprocess.run([*command, str(source)], env=env, check=True)
        tampered = temporary / "tampered.txt"
        tampered.write_bytes(source.read_bytes() + b"changed\n")
        result = subprocess.run([*command, str(tampered)], env=env, capture_output=True, text=True)
        if result.returncode == 0 or "invalid signature" not in result.stderr:
            raise RuntimeError("Modified manifest did not fail specifically at signature verification")
        print("Upstream signature accepted; modified manifest rejected at signature verification")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    check(parser.parse_args().binary)
