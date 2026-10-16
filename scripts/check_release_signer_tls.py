#!/usr/bin/env python3
"""Calibrate actual Cosign TLS against synthetic loopback certificate chains."""
from __future__ import annotations

import argparse
import http.server
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading


def openssl(directory: Path, *args: str) -> None:
    subprocess.run(["openssl", *args], cwd=directory, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


ROOT_CERTIFICATE = "root.pem"
INTERMEDIATE_CERTIFICATE = "intermediate.pem"


def create_certificate(directory: Path, name: str, weak: str | None) -> None:
    bits = 1024 if name == weak else 2048
    common_name = "localhost" if name == "leaf" else name
    openssl(directory, "req", "-new", "-newkey", f"rsa:{bits}",
            "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
            "-subj", f"/CN={common_name}")
    ca = name != "leaf"
    extension = f"basicConstraints=critical,CA:{str(ca).upper()}\n"
    extension += "keyUsage=critical," + ("keyCertSign,cRLSign\n" if ca else "digitalSignature,keyEncipherment\n")
    if not ca:
        extension += "subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n"
    (directory / f"{name}.ext").write_text(extension)
    parents = {"intermediate": "root", "leaf": "intermediate"}
    if name == "root":
        signer = ("-signkey", "root.key")
    else:
        parent = parents[name]
        signer = ("-CA", f"{parent}.pem", "-CAkey", f"{parent}.key")
    openssl(directory, "x509", "-req", "-in", f"{name}.csr", *signer, "-set_serial",
            str({"root": 1, "intermediate": 2, "leaf": 3}[name]), "-days", "1", "-sha256",
            "-extfile", f"{name}.ext", "-out", f"{name}.pem")


def chain(directory: Path, weak: str | None) -> None:
    for name in ("root", "intermediate", "leaf"):
        create_certificate(directory, name, weak)
    openssl(directory, "verify", "-CAfile", ROOT_CERTIFICATE, "-untrusted", INTERMEDIATE_CERTIFICATE, "leaf.pem")
    (directory / "chain.pem").write_bytes((directory / "leaf.pem").read_bytes() + (directory / INTERMEDIATE_CERTIFICATE).read_bytes())


def server_context(directory: Path, version: ssl.TLSVersion) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    if version == ssl.TLSVersion.TLSv1_3:
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_3
    elif version == ssl.TLSVersion.TLSv1_2:
        context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
    else:
        raise ValueError("The fixture only supports TLS 1.2 and 1.3")
    # Test-only loopback server must present the deliberately weak chain.
    # The production Cosign client remains strict and must reject it before HTTP.
    context.set_ciphers("DEFAULT:@SECLEVEL=0")
    context.load_cert_chain(directory / "chain.pem", directory / "leaf.key")
    return context


def probe_command(binary: Path, client: str, home: Path, port: int, env: dict) -> list[str]:
    if client == "gh":
        # Never send an inherited operator credential, even to loopback.
        env.update({"GH_TOKEN": "synthetic-fixture-token", "GITHUB_TOKEN": "synthetic-fixture-token",
                    "GH_ENTERPRISE_TOKEN": "synthetic-fixture-token", "GITHUB_ENTERPRISE_TOKEN": "synthetic-fixture-token",
                    "GH_CONFIG_DIR": str(home), "GH_HOST": "github.com"})
        return [str(binary), "api", f"https://localhost:{port}/fixture"]
    if client == "downloader":
        return [str(binary), f"https://localhost:{port}/fixture", str(home / "download")]
    return [str(binary), "initialize", "--mirror", f"https://localhost:{port}"]


def probe(binary: Path, weak: str | None, version: ssl.TLSVersion, client: str = "cosign") -> None:
    with tempfile.TemporaryDirectory(prefix="cosign-tls-") as temporary:
        directory = Path(temporary)
        chain(directory, weak)
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_error(404, "Synthetic transport fixture")

            def log_message(self, *_args):
                # Only request counts are evidence; fixture request logs add no information.
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = server_context(directory, version)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for strict in ((True,) if client == "downloader" else (False, True)):
                home = directory / f"home-{strict}"
                home.mkdir()
                roots = directory / "empty-roots"
                roots.mkdir(exist_ok=True)
                # Go 1.27 fixture-only trust override avoids modifying the macOS keychain.
                env = {**os.environ, "HOME": str(home), "GODEBUG": f"fips140={'on' if strict else 'off'},x509sslcertoverrideplatform=1",
                       "SSL_CERT_FILE": str(directory / ROOT_CERTIFICATE), "SSL_CERT_DIR": str(roots),
                       "NO_PROXY": "localhost,127.0.0.1"}
                before = len(requests)
                command = probe_command(binary, client, home, server.server_port, env)
                result = subprocess.run(command, env=env,
                                        text=True, capture_output=True, timeout=30)
                count = len(requests) - before
                if weak and strict:
                    if count or result.returncode == 0 or "no FIPS compatible certificate chains found" not in result.stderr:
                        raise RuntimeError(f"Weak {weak} chain was not rejected before HTTP: {result.stderr}")
                elif not count or "404" not in result.stderr:
                    raise RuntimeError(f"Positive transport calibration failed: {result.stderr}")
                print(f"{version.name} weak={weak} strict={strict}: HTTP requests={count}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def check(binary: Path, client: str = "cosign") -> None:
    if sys.platform not in {"linux", "darwin"}:
        raise RuntimeError("This test supports Linux and macOS")
    for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
        for weak in (None, "root", "intermediate", "leaf"):
            probe(binary.resolve(), weak, version, client)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    check(parser.parse_args().binary)
