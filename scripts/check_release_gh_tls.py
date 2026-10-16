#!/usr/bin/env python3
"""Exercise the actual GitHub CLI using the calibrated loopback TLS fixture."""
import argparse
from pathlib import Path

from check_release_signer_tls import check

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    parser.add_argument("--downloader", action="store_true")
    args = parser.parse_args()
    check(args.binary, "downloader" if args.downloader else "gh")
