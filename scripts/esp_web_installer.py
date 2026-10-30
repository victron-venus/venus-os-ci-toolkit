#!/usr/bin/env python3
"""Build a deterministic ESP Web Tools site from release inputs."""

import argparse
import hashlib
import html
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
from urllib.parse import urlsplit

WEB_TOOLS_VERSION = "10.4.0"
# Bootloader offsets and extended-header chip IDs from esptool v5.1.0 targets.
# ESP8266 has no ESP32 extended-header chip ID.
IMAGE_LAYOUTS = {
    "ESP8266": (0, None),
    "ESP32": (0x1000, 0),
    "ESP32-S2": (0x1000, 2),
    "ESP32-S3": (0, 9),
    "ESP32-C2": (0, 12),
    "ESP32-C3": (0, 5),
    "ESP32-C5": (0x2000, 23),
    "ESP32-C6": (0, 13),
    "ESP32-C61": (0, 20),
    "ESP32-H2": (0, 16),
    "ESP32-P4": (0x2000, 18),
}
CHIPS = set(IMAGE_LAYOUTS)
CHANNELS = {"stable", "beta", "rc", "nightly"}
MAX_BINARY = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
PRIVATE_MARKERS = re.compile(
    rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})|AKIA[0-9A-Z]{16}"
)


def require(ok, message):
    if not ok:
        raise ValueError(message)


def fields(value, allowed, required, context):
    require(isinstance(value, dict), f"{context} must be an object")
    require(set(value) <= set(allowed), f"Unknown {context} field")
    require(set(required) <= set(value), f"Missing {context} field")


def text(value, context, maximum=240):
    require(isinstance(value, str) and 0 < len(value) <= maximum, f"Invalid {context}")
    require(not any(ord(c) < 32 for c in value), f"Control character in {context}")
    require(
        not PRIVATE_MARKERS.search(value.encode()), f"Credential marker in {context}"
    )
    return value


def public_url(value, context):
    text(value, context, 500)
    url = urlsplit(value)
    require(
        url.scheme == "https"
        and url.hostname
        and not url.username
        and not url.password
        and not url.query
        and url.port in (None, 443),
        f"{context} requires public HTTPS without credentials/query",
    )
    host = url.hostname.lower()
    require(
        "." in host and not host.endswith((".local", ".internal", ".localhost")),
        f"Non-public host in {context}",
    )
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return value
    raise ValueError(f"IP addresses are not accepted in {context}")


def local_file(root, name, maximum):
    require(
        isinstance(name, str) and not name.startswith("/"),
        "Expected relative input path",
    )
    parts = name.split("/")
    require(
        all(
            re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", p)
            and p not in (".", "..")
            and not p.lower().startswith(("secret", "private"))
            for p in parts
        ),
        "Unsafe/private input path",
    )
    path = root
    for part in parts:
        path /= part
        require(not path.is_symlink(), "Input symlinks are not accepted")
    require(
        path.is_file() and path.resolve().is_relative_to(root),
        "Input missing or outside root",
    )
    before = path.stat()
    require(0 < before.st_size <= maximum, "Input file size invalid")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    require(
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
        "Input changed during read",
    )
    require(len(data) == before.st_size, "Input changed or exceeds size limit")
    require(not PRIVATE_MARKERS.search(data), "Credential marker found in public input")
    return data


def validate_factory(data, chip, partition_offset):
    """Reject obvious archives/OTA-only files; not full image or hardware validation."""

    def at(offset, count):
        require(
            offset >= 0 and offset + count <= len(data),
            "Factory image missing boot/partition/application region",
        )
        return data[offset : offset + count]

    # Merged images may contain padding before the chip-specific bootloader.
    boot_offset, chip_id = IMAGE_LAYOUTS[chip]
    header = at(boot_offset, 24)
    require(
        header[0] == 0xE9 and 1 <= header[1] <= 16 and header[2] <= 3,
        "Invalid ESP boot header; use the complete factory binary",
    )
    if chip == "ESP8266":
        require(len(data) >= 256, "ESP8266 firmware is incomplete")
        return
    require(
        struct.unpack_from("<H", header, 12)[0] == chip_id,
        "Bootloader chip ID does not match the hardware profile",
    )
    application = None
    for i in range(96):
        row = at(partition_offset + i * 32, 32)
        magic, kind, _subtype, offset, size = struct.unpack_from("<HBBII", row)
        if magic in (0xFFFF, 0xEBEB):
            break
        require(magic == 0x50AA, "Invalid ESP32 partition table")
        if kind == 0 and application is None:
            require(
                offset >= partition_offset + 0x1000 and offset + size <= MAX_BINARY,
                "Invalid application partition bounds",
            )
            application = at(offset, 24)
    require(
        application is not None
        and application[0] == 0xE9
        and 1 <= application[1] <= 16,
        "Factory image has no valid application partition",
    )
    require(
        struct.unpack_from("<H", application, 12)[0] == chip_id,
        "Application chip ID does not match the hardware profile",
    )


def load_inputs(root, catalog, version=None, channel=None):
    require(
        root.is_dir() and not root.is_symlink(),
        "Input root must be a directory, not a symlink",
    )
    root = root.resolve()
    doc = json.loads(local_file(root, catalog, 128 * 1024))
    fields(
        doc,
        {
            "schema",
            "name",
            "version",
            "channel",
            "project_url",
            "release_url",
            "profiles",
        },
        {"schema", "name", "project_url", "profiles"},
        "catalog",
    )
    require(
        type(doc["schema"]) is int and doc["schema"] == 1, "Unsupported catalog schema"
    )
    name = text(doc["name"], "name", 100)
    require(
        not version or not doc.get("version") or version == doc["version"],
        "Release version differs from catalog",
    )
    version = version or doc.get("version")
    require(
        isinstance(version, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}", version),
        "An explicit release version is required",
    )
    require(
        not channel or not doc.get("channel") or channel == doc["channel"],
        "Release channel differs from catalog",
    )
    channel = channel or doc.get("channel")
    require(
        isinstance(channel, str) and channel in CHANNELS,
        "Explicit stable/beta/rc/nightly channel required",
    )
    project_url = public_url(doc["project_url"], "project_url")
    release_url = (
        public_url(doc["release_url"], "release_url")
        if doc.get("release_url")
        else None
    )
    profiles = doc["profiles"]
    require(
        isinstance(profiles, list) and 1 <= len(profiles) <= 24,
        "Expected 1..24 hardware profiles",
    )
    seen, total, prepared = set(), 0, []
    for profile in profiles:
        fields(
            profile,
            {
                "id",
                "label",
                "board",
                "chip_family",
                "provisioning",
                "description",
                "setup_url",
                "parts",
                "partition_offset",
            },
            {"id", "label", "board", "chip_family", "provisioning", "parts"},
            "profile",
        )
        ident = profile["id"]
        require(
            isinstance(ident, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", ident)
            and ident not in seen,
            "Profile IDs must be unique lowercase slugs",
        )
        seen.add(ident)
        chip = profile["chip_family"]
        require(isinstance(chip, str) and chip in CHIPS, "Unsupported ESP chip family")
        provisioning = profile["provisioning"]
        require(
            isinstance(provisioning, str)
            and provisioning in {"improv", "manual", "none"},
            "Unknown provisioning method",
        )
        metadata = {
            "id": ident,
            "label": text(profile["label"], "label", 120),
            "board": text(profile["board"], "board"),
            "chip_family": chip,
            "provisioning": provisioning,
            "manifest": f"manifests/{ident}.json",
        }
        if "description" in profile:
            metadata["description"] = text(profile["description"], "description", 700)
        if "setup_url" in profile:
            metadata["setup_url"] = public_url(profile["setup_url"], "setup_url")
        rows = profile["parts"]
        require(
            isinstance(rows, list) and len(rows) == 1,
            "Use one merged factory binary per profile; overlapping/split parts are not supported",
        )
        row = rows[0]
        fields(row, {"path", "offset", "sha256"}, {"path", "offset"}, "part")
        require(
            isinstance(row["path"], str) and row["path"].endswith(".bin"),
            "Only explicit .bin inputs accepted",
        )
        require(
            type(row["offset"]) is int and row["offset"] == 0,
            "Merged factory image must start at offset 0",
        )
        data = local_file(root, row["path"], MAX_BINARY)
        digest = hashlib.sha256(data).hexdigest()
        if "sha256" in row:
            require(row["sha256"] == digest, "Binary SHA256 mismatch")
        total += len(data)
        require(total <= MAX_TOTAL, "Combined firmware inputs exceed size limit")
        partition_offset = profile.get("partition_offset", 0x8000)
        require(
            type(partition_offset) is int
            and 0x1000 <= partition_offset < MAX_BINARY
            and partition_offset % 4096 == 0,
            "Invalid partition table offset",
        )
        validate_factory(data, chip, partition_offset)
        prepared.append(
            (
                metadata,
                {
                    "path": f"firmware/{ident}/{Path(row['path']).name}",
                    "offset": 0,
                    "data": data,
                    "sha256": digest,
                },
            )
        )
    return {
        "schema": 1,
        "name": name,
        "version": version,
        "channel": channel,
        "project_url": project_url,
        "release_url": release_url,
        "web_tools_version": WEB_TOOLS_VERSION,
    }, prepared


def generate(root, catalog, output, version=None, channel=None):
    metadata, profiles = load_inputs(root, catalog, version, channel)
    require(
        not output.exists() and not output.is_symlink(),
        "Output must be a new directory",
    )
    require(
        output.parent.is_dir() and output.parent.resolve() == output.parent.absolute(),
        "Output parent must exist without symlink components",
    )
    templates = Path(__file__).resolve().parent.parent / "templates/esp-web-installer"
    staging = Path(tempfile.mkdtemp(prefix=".esp-installer-", dir=output.parent))
    try:
        checksums, inventory = [], []

        def write(name, raw):
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            checksums.append(f"{hashlib.sha256(raw).hexdigest()}  {name}\n")

        def write_json(name, value):
            write(
                name,
                (
                    json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n"
                ).encode(),
            )

        for profile, part in profiles:
            write(part["path"], part["data"])
            entry = {key: part[key] for key in ("path", "offset", "sha256")}
            manifest = {
                "name": f"{metadata['name']} — {profile['label']}",
                "version": metadata["version"],
                "new_install_prompt_erase": True,
                "builds": [
                    {
                        "chipFamily": profile["chip_family"],
                        "parts": [{"path": "../" + entry["path"], "offset": 0}],
                    }
                ],
            }
            if profile["provisioning"] != "improv":
                manifest["new_install_improv_wait_time"] = 0
            write_json(profile["manifest"], manifest)
            inventory.append({**profile, "parts": [entry]})
        write_json("catalog.json", {**metadata, "profiles": inventory})
        page = (
            (templates / "index.html")
            .read_text()
            .replace("__TITLE__", html.escape(metadata["name"]))
        )
        write("index.html", page.encode())
        for name in ("app.js", "style.css"):
            write(name, (templates / name).read_bytes())
        (staging / "SHA256SUMS").write_text("".join(sorted(checksums)))
        require(
            not output.exists() and not output.is_symlink(),
            "Output appeared during build",
        )
        staging.rename(output)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {
        "output": str(output),
        "version": metadata["version"],
        "channel": metadata["channel"],
        "profiles": len(profiles),
        "web_tools_version": WEB_TOOLS_VERSION,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument(
        "--catalog", default="web-flasher.json", help="Path relative to input directory"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--channel", choices=sorted(CHANNELS))
    args = parser.parse_args()
    print(
        json.dumps(
            generate(
                args.input_dir,
                args.catalog,
                args.output.absolute(),
                args.version,
                args.channel,
            )
        )
    )


if __name__ == "__main__":
    main()
