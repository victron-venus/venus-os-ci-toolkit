#!/usr/bin/env python3
"""Remove unused npm copies from the pinned runner, retaining its action engines."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil

VERSIONS = {"node20": "10.8.2", "node24": "11.19.1"}


def require(value, message):
    if not value:
        raise ValueError(message)


def prune(runner):
    runner = Path(runner)
    require(runner.is_dir() and not runner.is_symlink(), "Invalid runner directory")
    plans = []
    for runtime, version in VERSIONS.items():
        root = runner / "externals" / runtime
        for directory in (runner / "externals", root, root / "bin", root / "lib",
                          root / "lib/node_modules", root / "lib/node_modules/npm"):
            require(directory.is_dir() and not directory.is_symlink(),
                    "Unexpected embedded Node directory")
        node = root / "bin/node"
        require(node.is_file() and not node.is_symlink(), "Missing action Node binary")
        npm = root / "lib/node_modules/npm"
        manifest = json.loads((npm / "package.json").read_text())
        require(manifest.get("name") == "npm" and manifest.get("version") == version,
                "Unexpected embedded npm version; review the new runner first")
        for cli in ("npm", "npx"):
            link = root / "bin" / cli
            require(link.is_symlink() and os.readlink(link) ==
                    f"../lib/node_modules/npm/bin/{cli}-cli.js", "Unexpected npm launcher")
        plans.append((node, hashlib.sha256(node.read_bytes()).hexdigest(), npm, root))
    # Validate every runtime before deleting anything. Never prune Node or corepack.
    for node, digest, npm, root in plans:
        for cli in ("npm", "npx"):
            (root / "bin" / cli).unlink()
        shutil.rmtree(npm)
        require(hashlib.sha256(node.read_bytes()).hexdigest() == digest,
                "Action Node binary changed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runner", type=Path)
    prune(parser.parse_args().runner)
