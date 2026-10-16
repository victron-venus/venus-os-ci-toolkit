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


def chain(directory: Path, weak: str | None) -> None:
    for name in ("root", "intermediate", "leaf"):
        openssl(directory, "req", "-new", "-newkey", f"rsa:{1024 if name == weak else 2048}",
                "-nodes", "-keyout", f"{name}.key", "-out", f"{name}.csr",
                "-subj", f"/CN={'localhost' if name == 'leaf' else name}")
        ca = name != "leaf"
        extension = f"basicConstraints=critical,CA:{str(ca).upper()}\n"
        extension += "keyUsage=critical," + ("keyCertSign,cRLSign\n" if ca else "digitalSignature,keyEncipherment\n")
        if not ca:
            extension += "subjectAltName=DNS:localhost\nextendedKeyUsage=serverAuth\n"
        (directory / f"{name}.ext").write_text(extension)
        signer = ("-signkey", "root.key") if name == "root" else (
            "-CA", "root.pem" if name == "intermediate" else "intermediate.pem",
            "-CAkey", "root.key" if name == "intermediate" else "intermediate.key")
        openssl(directory, "x509", "-req", "-in", f"{name}.csr", *signer, "-set_serial",
                str({"root": 1, "intermediate": 2, "leaf": 3}[name]), "-days", "1", "-sha256",
                "-extfile", f"{name}.ext", "-out", f"{name}.pem")
    openssl(directory, "verify", "-CAfile", "root.pem", "-untrusted", "intermediate.pem", "leaf.pem")
    (directory / "chain.pem").write_bytes((directory / "leaf.pem").read_bytes() + (directory / "intermediate.pem").read_bytes())


def probe(binary: Path, weak: str | None, version: ssl.TLSVersion) -> None:
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
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = context.maximum_version = version
        context.set_ciphers("DEFAULT:@SECLEVEL=0")
        context.load_cert_chain(directory / "chain.pem", directory / "leaf.key")
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for strict in (False, True):
                home = directory / f"home-{strict}"
                home.mkdir()
                roots = directory / "empty-roots"
                roots.mkdir(exist_ok=True)
                env = {**os.environ, "HOME": str(home), "GODEBUG": f"fips140={'on' if strict else 'off'}",
                       "SSL_CERT_FILE": str(directory / "root.pem"), "SSL_CERT_DIR": str(roots),
                       "NO_PROXY": "localhost,127.0.0.1"}
                before = len(requests)
                result = subprocess.run([str(binary), "initialize", "--mirror",
                                         f"https://localhost:{server.server_port}"], env=env,
                                        text=True, capture_output=True, timeout=30)
                count = len(requests) - before
                if weak and strict:
                    if count or result.returncode == 0 or "x509" not in result.stderr:
                        raise RuntimeError(f"Weak {weak} chain was not rejected before HTTP: {result.stderr}")
                elif not count or "404" not in result.stderr:
                    raise RuntimeError(f"Positive transport calibration failed: {result.stderr}")
                print(f"{version.name} weak={weak} strict={strict}: HTTP requests={count}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


def check(binary: Path) -> None:
    if sys.platform != "linux":
        raise RuntimeError("This test requires Linux: Go on macOS uses the system trust store")
    for version in (ssl.TLSVersion.TLSv1_2, ssl.TLSVersion.TLSv1_3):
        for weak in (None, "root", "intermediate", "leaf"):
            probe(binary.resolve(), weak, version)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    check(parser.parse_args().binary)
