#!/usr/bin/env python3
"""Record the exact version input and digest of a completed native binary."""

import hashlib
import json
import sys
from pathlib import Path

import tomllib


def main() -> None:
    """Write metadata for the final output paths selected by the packager."""
    root = Path(__file__).resolve().parents[1]
    binary = Path(sys.argv[1])
    output = Path(sys.argv[2])
    if binary.is_symlink() or not binary.is_file():
        raise ValueError("Binary must be a regular file")
    if (root / "VERSION").is_file():
        version = (root / "VERSION").read_text(encoding="utf-8").strip()
    else:
        version = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))[
            "package"
        ]["version"]
    metadata = {
        "version": version,
        "binary": binary.name,
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    output.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
