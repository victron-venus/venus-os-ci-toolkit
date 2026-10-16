#!/usr/bin/env python3
"""Build the reviewed, patched Cosign from a clean, pinned upstream checkout."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess

SOURCE = "11926fa5bbbbde47e88fc006b625a17769b743b2"
GO_VERSION = "go1.27.2"
ROOT = Path(__file__).resolve().parents[1]


def build_environment() -> dict[str, str]:
    """Use authenticated module downloads and bound compiler concurrency."""
    return {**os.environ, "GOTOOLCHAIN": "local", "GODEBUG": "fips140=on",
            "GOMAXPROCS": "2", "GOFLAGS": "-p=2", "GOENV": "off", "GOWORK": "off",
            "GOPROXY": "https://proxy.golang.org", "GOSUMDB": "sum.golang.org",
            "GONOSUMDB": "", "GONOPROXY": "", "GOPRIVATE": "", "GOVCS": "*:off"}


def run(args: list[str], source: Path, env: dict[str, str]) -> str:
    return subprocess.check_output(args, cwd=source, env=env, text=True).strip()


def build(source: Path, output: Path) -> None:
    """Reject dirty/wrong source and toolchains before building or installing."""
    source, output = source.resolve(), output.resolve()
    env = build_environment()
    if output.is_relative_to(source):
        raise ValueError("Output must be outside the upstream source checkout")
    if run(["git", "rev-parse", "HEAD"], source, env) != SOURCE:
        raise ValueError("Unexpected Cosign source commit")
    if run(["git", "status", "--porcelain", "--untracked-files=all"], source, env):
        raise ValueError("Cosign source checkout must be clean")
    if run(["go", "env", "GOVERSION"], source, env) != GO_VERSION:
        raise ValueError("Release signer requires Go 1.27.2 exactly")
    original = {name: (source / name).read_bytes() for name in ("go.mod", "go.sum")}
    locked = {name: (ROOT / ".github/cosign" / name).read_bytes() for name in original}
    try:
        for name in original:
            (source / name).write_bytes(locked[name])
        subprocess.run(["go", "mod", "download"], cwd=source, env=env, check=True)
        subprocess.run(["go", "mod", "verify"], cwd=source, env=env, check=True)
        if any((source / name).read_bytes() != data for name, data in locked.items()):
            raise ValueError("Module download changed the reviewed lock files")
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["go", "build", "-mod=readonly", "-trimpath", "-o", str(output),
                        "./cmd/cosign"], cwd=source, env=env, check=True)
        subprocess.run(["go", "version", "-m", str(output)], env=env, check=True)
        subprocess.run([str(output), "version"], env=env, check=True)
    finally:
        for name, data in original.items():
            (source / name).write_bytes(data)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
