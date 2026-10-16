#!/usr/bin/env python3
"""Prepare reviewed browser locks and a required/extended consumer inventory."""

import argparse
import json
from pathlib import Path

MANIFEST = Path(__file__).with_name("browser-toolchains.json")


def render():
    """Keep application dependencies separate from the browser generations."""
    manifest = json.loads(MANIFEST.read_text())
    current = manifest["current_playwright"]
    files = {}
    targets = []
    for version, toolchain in manifest["playwright"].items():
        directory = f"tools/browser-compatibility/playwright-{version.replace('.', '-')}"
        package = {"name": "isolated-browser-compatibility", "private": True,
                   "dependencies": {"playwright": version}}
        packages = {"": {"name": package["name"], "dependencies": package["dependencies"]}}
        for name, metadata in toolchain["packages"].items():
            pinned = metadata.get("version", version)
            entry = {"version": pinned, "resolved": f"https://registry.npmjs.org/{name}/-/{name}-{pinned}.tgz", **metadata}
            if name == "playwright":
                entry["dependencies"] = {"playwright-core": version}
            packages["node_modules/" + name] = entry
        files[directory + "/package.json"] = package
        files[directory + "/package-lock.json"] = {
            "name": package["name"], "lockfileVersion": 3, "requires": True, "packages": packages,
        }
        for os_name in ("ubuntu-latest", "windows-2022", "macos-15"):
            for engine, browser in toolchain["browsers"].items():
                required = version == current and (
                    os_name == "ubuntu-latest" or
                    (os_name == "windows-2022" and engine == "chromium") or
                    (os_name == "macos-15" and engine == "webkit")
                )
                targets.append({
                    "id": f"{os_name}-{engine}-{version.replace('.', '-')}",
                    "os": os_name, "engine": engine, "playwright": version,
                    "browser_version": browser["version"], "toolchain": directory,
                    "tier": "required" if required else "extended",
                })
        if version == current:
            for os_name in ("ubuntu-latest", "windows-2022", "macos-15"):
                channels = ("chrome", "msedge", "chrome-beta", "msedge-beta") if os_name == "ubuntu-latest" else ("chrome", "msedge")
                for channel in channels:
                    targets.append({
                        "id": f"{os_name}-{channel}", "os": os_name,
                        "engine": "chromium", "channel": channel, "playwright": version,
                        "browser_version": "installed", "toolchain": directory, "tier": "extended",
                    })
    files[".github/browser-compatibility.json"] = targets
    return {name: json.dumps(value, indent=2) + "\n" for name, value in files.items()}


def main():
    """Write only this generator's files, or compare without changing anything."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    root = args.directory.resolve(strict=True)
    files = render()
    targets = [(root / name, text) for name, text in files.items()]
    for path, _ in targets:
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            parser.error("Browser configuration cannot traverse symlinks outside the consumer")
    drift = [str(path.relative_to(root)) for path, text in targets if not path.exists() or path.read_text() != text]
    if args.check and drift:
        parser.exit(1, "Browser compatibility configuration differs: " + ", ".join(drift) + "\n")
    if not args.check:
        for path, text in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
    print(f"{'Verified' if args.check else 'Prepared'} {len(files)} browser compatibility files")


if __name__ == "__main__":
    main()
