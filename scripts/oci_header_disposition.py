"""Finite applicability of specific kernel defects to exact userspace inputs.

Headers can contain compiled inline code. The review must identify the affected
implementation for every CVE; filename suffixes alone never establish safety.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tarfile

CONNTRACK_HEADER = "usr/include/linux/netfilter/nf_conntrack_common.h"


def need(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_exported_conntrack(data):
    need(b"#define _NF_CONNTRACK_COMMON_H" in data and b"__KERNEL__" not in data
         and b"NF_CT_EXPECT_DEAD" not in data,
         "Exported conntrack header differs from reviewed userspace boundary")
    return {"verified": True, "sha256": hashlib.sha256(data).hexdigest(),
            "kernel_guard_absent": True, "kernel_dead_flag_absent": True}


def normal_path(value, root=False):
    need(isinstance(value, str) and value and "\\" not in value,
         "Unsafe header file path")
    path = Path(value)
    if root and value == ".":
        return path
    need(not path.is_absolute() and ".." not in path.parts
         and path.as_posix() == value and value != ".",
         "Non-normal header file path")
    return path


def tar_inventory(payload):
    """Read bounded tar metadata/bytes without extracting any archive member."""
    need(len(payload) <= 16 * 1024 * 1024, "Oversized header tar")
    entries = []
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            normal_path(name, root=True)
            need(name not in seen and len(seen) < 2048, "Duplicate or excessive header members")
            seen.add(name)
            need(member.type in (tarfile.DIRTYPE, tarfile.REGTYPE, tarfile.AREGTYPE),
                 "Unreviewed link or special header member")
            need(not member.linkname and not member.pax_headers, "Unreviewed header member metadata")
            mode = 0o755 if member.isdir() else 0o644
            need(member.mode == mode and member.uid == member.gid == 0,
                 "Unexpected header archive permissions")
            entry = {"path": name, "type": "directory" if member.isdir() else "file",
                     "mode": mode, "uid": member.uid, "gid": member.gid, "size": member.size}
            if member.isreg():
                need(0 <= member.size <= 2 * 1024 * 1024, "Oversized header member")
                with archive.extractfile(member) as stream:
                    data = stream.read(member.size + 1)
                need(len(data) == member.size, "Truncated header member")
                need(not data.startswith((b"\x7fELF", b"!<arch>")), "Executable or object archive in headers")
                if name == CONNTRACK_HEADER:
                    verify_exported_conntrack(data)
                entry["sha256"] = hashlib.sha256(data).hexdigest()
            else:
                need(member.size == 0, "Directory contains header payload")
            entries.append(entry)
    return sorted(entries, key=lambda item: item["path"])


def inspect_deb(path, package):
    """Inventory the exact locked deb, including control scripts and all types.

dpkg-deb only decompresses to stdout; no archive extraction or package install
occurs here. Verify the small pinned input before invoking the decompressor.
"""
    need(path.stat().st_size == package["size"] <= 2 * 1024 * 1024
         and sha256(path) == package["sha256"], "Header archive identity changed")
    data = path.read_bytes()
    need(data.startswith(b"!<arch>\n"), "Invalid deb archive")
    offset = 8
    members = []
    while offset < len(data):
        header = data[offset:offset + 60]
        need(len(header) == 60 and header[58:] == b"`\n", "Invalid deb member")
        name = header[:16].decode("ascii").strip().removesuffix("/")
        size = int(header[48:58])
        need(0 <= size <= len(data) - offset - 60, "Invalid deb member size")
        content = data[offset + 60:offset + 60 + size]
        members.append({"name": name, "size": size, "mode": int(header[40:48], 8),
                        "sha256": hashlib.sha256(content).hexdigest()})
        if name == "debian-binary":
            need(content == b"2.0\n", "Unexpected deb format")
        offset += 60 + size + size % 2
    need(offset == len(data) and [m["name"] for m in members]
         == ["debian-binary", "control.tar.zst", "data.tar.zst"], "Unexpected deb member set")
    contents = {}
    for kind, option in (("control", "--ctrl-tarfile"), ("data", "--fsys-tarfile")):
        result = subprocess.run(["dpkg-deb", option, str(path)], check=True, timeout=30,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        contents[kind] = tar_inventory(result.stdout)
    need([e["path"] for e in contents["control"]] == [".", "control", "md5sums"],
         "Unreviewed package control script or metadata")
    files = [e for e in contents["data"] if e["type"] == "file"]
    summary = {"schema": 1, "archive_members": members,
               "control_members": contents["control"],
               "data_directories": [e for e in contents["data"] if e["type"] == "directory"],
               "payload_count": len(files), "payload_bytes": sum(e["size"] for e in files),
               "links": [], "special_members": [], "maintainer_scripts": []}
    return summary, files


def verify_archive(path, binding, files, inputs):
    summary, observed = inspect_deb(path, binding)
    expected_path = inputs / f"linux-libc-dev-archive-{binding['architecture']}.json"
    need(sha256(expected_path) == binding["archive_inventory_sha256"], "Archive inventory digest changed")
    need(summary == json.loads(expected_path.read_text()) and observed == files,
         "Header archive members differ from reviewed inventory")
    return summary


def load_policy(inputs, expected_sha, arch, packages):
    path = inputs / "header-disposition.json"
    need(sha256(path) == expected_sha, "Header disposition changed without source binding")
    policy = json.loads(path.read_text())
    need(policy["schema"] == 1 and type(policy["reviewed"]) is bool, "Invalid header review state")
    need(policy["distro"] == {"Family": "ubuntu", "Name": "24.04"}, "Unexpected header distro")
    ids = policy["cve_ids"]
    need(isinstance(ids, list) and ids and all(isinstance(v, str)
         and re.fullmatch(r"CVE-[0-9]{4}-[0-9]{4,}", v) for v in ids)
         and ids == sorted(set(ids)), "Header disposition requires a finite unique CVE list")
    binding = policy["packages"][arch]
    candidates = [p for p in packages if p["name"] == "linux-libc-dev"]
    need(len(candidates) == 1, "Expected exactly one header package")
    package = candidates[0]
    need(arch in ("amd64", "arm64") and binding["architecture"] == arch and package["source"] == "linux",
         "Unexpected header source or architecture")
    need({k: package[k] for k in ("name", "version", "architecture", "sha256", "size")}
         == {k: binding[k] for k in ("name", "version", "architecture", "sha256", "size")},
         "Header package differs from reviewed archive")
    inventory = inputs / f"linux-libc-dev-files-{arch}.json"
    need(sha256(inventory) == binding["files_sha256"], "Header file inventory changed")
    files = json.loads(inventory.read_text())
    names = [f["path"] for f in files]
    need(names and names == sorted(set(names)), "Invalid header file inventory")
    for item in files:
        path = normal_path(item["path"])
        need(item["type"] == "file" and item["mode"] == 0o644 and item["uid"] == item["gid"] == 0,
             "Unexpected header file metadata")
        need((item["path"].startswith("usr/include/") and path.suffix == ".h")
             or item["path"] in ("usr/share/doc/linux-libc-dev/changelog.Debian.gz",
                                  "usr/share/doc/linux-libc-dev/copyright"),
             "Unexpected content in reviewed headers")
        need(0 <= item["size"] <= 2 * 1024 * 1024, "Unbounded header input")
        need(re.fullmatch("[0-9a-f]{64}", item["sha256"]), "Invalid header file digest")
    need(len(files) == binding["file_count"] and sum(f["size"] for f in files) == binding["total_bytes"],
         "Header file inventory counters differ")
    conntrack = [f for f in files if f["path"] == CONNTRACK_HEADER]
    need(len(conntrack) == 1 and binding["exported_conntrack"] == {
        "verified": True, "sha256": conntrack[0]["sha256"],
        "kernel_guard_absent": True, "kernel_dead_flag_absent": True,
    }, "Exported conntrack evidence differs from inventory")
    need(sha256(inputs / f"linux-libc-dev-archive-{arch}.json") == binding["archive_inventory_sha256"],
         "Header archive inventory changed")
    review_path = inputs / "header-cve-evidence.json"
    need(sha256(review_path) == policy["review_evidence"], "Per-CVE evidence changed")
    review = json.loads(review_path.read_text())
    need(sorted(review["records"]) == ids, "Per-CVE evidence does not cover exact finding set")
    return policy, binding, files


def open_beneath(root, relative):
    """Open a regular input without following any path-component symlink."""
    parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = Path(relative).parts
        if not parts:
            return os.dup(parent)
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)


def verify_installed(root, files, owned_paths, directories):
    """Verify actual bytes and reject extra owned payloads, including symlinks."""
    expected = {item["path"] for item in files}
    expected_dirs = {item["path"]: item for item in directories}
    observed = set()
    seen = set()
    for raw in owned_paths:
        path = raw.removeprefix("/")
        normal_path(path, root=True)
        need(path not in seen, "Duplicate installed package path")
        seen.add(path)
        metadata = (root / path).lstat()
        if stat.S_ISDIR(metadata.st_mode):
            need(path in expected_dirs, "Unexpected installed directory")
            directory = expected_dirs[path]
            need(metadata.st_uid == directory["uid"] and metadata.st_gid == directory["gid"]
                 and stat.S_IMODE(metadata.st_mode) == directory["mode"],
                 f"Installed header directory ownership or mode changed: {path}; "
                 f"actual={metadata.st_uid}:{metadata.st_gid}:{stat.S_IMODE(metadata.st_mode):04o}; "
                 f"expected={directory['uid']}:{directory['gid']}:{directory['mode']:04o}")
            continue
        need(stat.S_ISREG(metadata.st_mode) and path in expected, "Unexpected installed header payload")
        observed.add(path)
    need(observed == expected, "Incomplete installed header inventory")
    need(seen == expected | set(expected_dirs), "Incomplete installed directory inventory")
    conntrack = None
    for item in files:
        with os.fdopen(open_beneath(root, item["path"]), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            need(stat.S_ISREG(metadata.st_mode) and stat.S_IMODE(metadata.st_mode) == item["mode"]
                 and metadata.st_size == item["size"], "Header file type, mode or size changed")
            need(metadata.st_uid == item["uid"] and metadata.st_gid == item["gid"],
                 f"Installed header file ownership changed: {item['path']}; "
                 f"actual={metadata.st_uid}:{metadata.st_gid}; expected={item['uid']}:{item['gid']}")
            data = stream.read(item["size"] + 1)
            need(len(data) == item["size"] and hashlib.sha256(data).hexdigest() == item["sha256"],
                 "Installed header bytes differ from reviewed archive")
            need(not data.startswith((b"\x7fELF", b"!<arch>")), "Executable or link archive in headers")
            if item["path"] == CONNTRACK_HEADER:
                conntrack = verify_exported_conntrack(data)
    need(conntrack, "Missing exported conntrack header evidence")
    return {"verified": True, "file_count": len(files), "bytes": sum(f["size"] for f in files),
            "exported_conntrack": conntrack, "ownership_verified": True,
            "directory_count": len(expected_dirs)}


def installed_metadata(root, owned_paths):
    """Capture metadata before enforcing the guard, so failures stay diagnosable."""
    observed = []
    for raw in owned_paths:
        path = raw.removeprefix("/")
        normal_path(path, root=True)
        metadata = (root / path).lstat()
        observed.append({"path": path, "uid": metadata.st_uid, "gid": metadata.st_gid,
                         "mode": stat.S_IMODE(metadata.st_mode),
                         "type": stat.S_IFMT(metadata.st_mode), "size": metadata.st_size})
    return observed


def harden_hosted_metadata(root, files, directories, actions):
    """Restore only the five observed hosted-image mode deviations, via open FDs.

    Parents become non-writable before opening their children. Ownership is never
    repaired: a replaced object or any unexpected starting mode fails closed.
    """
    expected_dirs = {entry["path"]: entry for entry in directories}
    expected_files = {entry["path"]: entry for entry in files}
    paths = (".", "usr", "usr/share", "usr/share/doc", "usr/share/doc/linux-libc-dev",
             "usr/share/doc/linux-libc-dev/changelog.Debian.gz",
             "usr/share/doc/linux-libc-dev/copyright")
    for name in paths:
        is_directory = name in expected_dirs
        entry = expected_dirs[name] if is_directory else expected_files[name]
        expected_mode = entry["mode"]
        need(expected_mode == (0o755 if is_directory else 0o644), "Unexpected hardening target mode")
        fd = open_beneath(root, name)
        try:
            before = os.fstat(fd)
            need((stat.S_ISDIR if is_directory else stat.S_ISREG)(before.st_mode)
                 and before.st_uid == entry["uid"] and before.st_gid == entry["gid"],
                 "Hosted hardening target type or ownership changed: " + name)
            mode = stat.S_IMODE(before.st_mode)
            allowed_modes = (expected_mode,) if name in (".", "usr") else (expected_mode, 0o777)
            need(mode in allowed_modes, "Unreviewed hosted hardening starting mode: " + name)
            if not is_directory:
                need(before.st_nlink == 1, "Hosted notice has an unreviewed hard link: " + name)
                need(before.st_size == entry["size"], "Hosted notice size changed: " + name)
                with os.fdopen(os.dup(fd), "rb") as stream:
                    data = stream.read(entry["size"] + 1)
                need(len(data) == entry["size"] and hashlib.sha256(data).hexdigest() == entry["sha256"],
                     "Hosted notice bytes changed: " + name)
            action = {"path": name, "uid": before.st_uid, "gid": before.st_gid,
                      "before_mode": mode, "target_mode": expected_mode, "changed": False}
            actions.append(action)
            if mode != expected_mode:
                if not is_directory:
                    need(os.fstat(fd).st_nlink == 1, "Hosted notice hard link appeared before repair: " + name)
                os.fchmod(fd, expected_mode)
                action["changed"] = True
            after = os.fstat(fd)
            if not is_directory:
                need(after.st_nlink == 1, "Hosted notice hard link appeared during repair: " + name)
            need(stat.S_IMODE(after.st_mode) == expected_mode
                 and (after.st_uid, after.st_gid) == (before.st_uid, before.st_gid),
                 "Hosted hardening did not restore expected metadata: " + name)
            action["after_mode"] = stat.S_IMODE(after.st_mode)
        finally:
            os.close(fd)


def matches_header_package(package, binding):
    version = ((str(package["Epoch"]) + ":" if package.get("Epoch") else "")
               + package.get("Version", "")
               + ("-" + package["Release"] if package.get("Release") else ""))
    return (package.get("Name") == binding["name"]
            and package.get("ID") == binding["name"] + "@" + binding["version"]
            and version == binding["version"] and package.get("Arch") == binding["architecture"]
            and package.get("SrcName") == "linux")


def classify(data, policy, binding, receipt, policy_sha):
    """Return exact reviewed findings only; every other finding stays blocking."""
    if not policy["reviewed"]:
        return []
    need(isinstance(policy.get("review_evidence"), str)
         and re.fullmatch("[0-9a-f]{64}", policy["review_evidence"]),
         "Reviewed header disposition lacks its evidence hash")
    need(receipt.get("verified") is True and receipt.get("policy_sha256") == policy_sha
         and receipt.get("package") == binding
         and receipt.get("archive_verified") is True
         and receipt.get("ownership_verified") is True
         and receipt.get("exported_conntrack") == binding["exported_conntrack"]
         and receipt.get("file_count") == binding["file_count"]
         and receipt.get("bytes") == binding["total_bytes"], "Missing exact installed-header verification")
    need(data.get("Metadata", {}).get("OS") == policy["distro"], "Header scan distro changed")
    allowed = []
    header_packages = [p for result in data.get("Results", []) if result.get("Class") == "os-pkgs"
                       for p in result.get("Packages", []) if p.get("Name") == binding["name"]]
    need(len(header_packages) == 1 and matches_header_package(header_packages[0], binding),
         "Header scanner package source or identity differs")
    for result_index, result in enumerate(data.get("Results", [])):
        if result.get("Class") != "os-pkgs" or result.get("Type") != "ubuntu":
            continue
        own_packages = [p for p in result.get("Packages", []) if p.get("Name") == binding["name"]]
        if len(own_packages) != 1 or not matches_header_package(own_packages[0], binding):
            continue
        for finding_index, finding in enumerate(result.get("Vulnerabilities", [])):
            if (finding.get("PkgName") == binding["name"]
                    and finding.get("PkgID") == binding["name"] + "@" + binding["version"]
                    and finding.get("InstalledVersion") == binding["version"]
                    and finding.get("DataSource", {}).get("ID") == "ubuntu"
                    and finding.get("VulnerabilityID") in policy["cve_ids"]
                    and finding.get("Severity") in ("HIGH", "CRITICAL")):
                allowed.append({"id": finding["VulnerabilityID"], "package": binding["name"],
                                "version": binding["version"], "architecture": binding["architecture"],
                                "severity": finding["Severity"], "status": "not_affected",
                                "result_index": result_index, "finding_index": finding_index,
                                "reason": policy["reason"], "review_evidence": policy["review_evidence"]})
    return allowed


def remaining_findings(data, dispositions):
    """Return an assessment view; never modify or omit the saved raw report."""
    positions = {(d["result_index"], d["finding_index"]) for d in dispositions}
    return {**data, "Results": [
        {**r, "Vulnerabilities": [v for vi, v in enumerate(r.get("Vulnerabilities", []))
                                  if (ri, vi) not in positions]}
        for ri, r in enumerate(data.get("Results", []))]}
