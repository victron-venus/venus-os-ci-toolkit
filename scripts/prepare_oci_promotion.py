#!/usr/bin/env python3
"""Stage two reviewed OCI candidates without rebuilding, executing or publishing."""

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import selectors
import shutil
import subprocess
import tarfile
import time
import zipfile

from build_oci_tools import hosted_arch, require

ROOT = Path(__file__).resolve().parents[1]
REPO = "victron-venus/venus-os-ci-toolkit"
TOOLS = {"buildctl", "buildkitd", "runc", "oci-probe"}
MAX_ARCHIVE = 96 * 1024 * 1024
MAX_EXPANDED = 512 * 1024 * 1024


def sha(data):
    return hashlib.sha256(data).hexdigest()


def gh(path, limit=MAX_ARCHIVE):
    """Bound stdout while downloading; never put credentials in arguments/logs."""
    with selectors.DefaultSelector() as selector:
        process = subprocess.Popen(["gh", "api", f"repos/{REPO}/{path}"],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        chunks, size = [], 0
        deadline = time.monotonic() + 180
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0 and selector.select(remaining), "GitHub request timed out")
                chunk = os.read(process.stdout.fileno(), 1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                require(size <= limit, "GitHub response exceeds the reviewed size limit")
                chunks.append(chunk)
            require(process.wait(timeout=30) == 0, "GitHub request failed")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            process.stdout.close()
    return b"".join(chunks)


def api(path):
    return json.loads(gh(path, 2 * 1024 * 1024))


def validate_lock(lock):
    require(lock.get("schema") == 1 and lock.get("reviewed") is True,
            "Promotion inputs require independent review")
    require(lock.get("repository") == REPO, "Unexpected candidate repository")
    require(re.fullmatch(r"[0-9a-f]{40}", lock.get("source") or ""), "Full source SHA required")
    require(re.fullmatch(r"[0-9a-f]{64}", lock.get("sources_sha256") or ""), "Input digest required")
    require(type(lock.get("qualification_run")) is int and lock["qualification_run"] > 0,
            "Exact qualification run required")
    require(set(lock.get("architectures", {})) == {"amd64", "arm64"}, "Both native architectures required")
    for arch, value in lock["architectures"].items():
        require(set(value["binaries"]) == TOOLS, "Incomplete binary inventory: " + arch)
        require(all(re.fullmatch(r"[0-9a-f]{64}", v) for v in value["binaries"].values()),
                "Invalid binary digest")
        for kind in ("candidate", "runtime"):
            artifact = value[kind]
            require(type(artifact["id"]) is int and artifact["id"] > 0, "Artifact ID required")
            require(re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]), "Artifact digest required")
            require(type(artifact["bytes"]) is int and 0 < artifact["bytes"] <= MAX_ARCHIVE,
                    "Invalid artifact size")


def artifact(lock, arch, kind):
    expected = lock["architectures"][arch][kind]
    info = api(f"actions/artifacts/{expected['id']}")
    name = ("oci-candidate-" if kind == "candidate" else "oci-runtime-evidence-") + arch
    require(info["id"] == expected["id"] and info["name"] == name and not info["expired"],
            "Wrong or expired artifact")
    require(info["workflow_run"]["id"] == lock["qualification_run"]
            and info["workflow_run"]["head_sha"] == lock["source"], "Artifact source/run mismatch")
    require(info["digest"] == "sha256:" + expected["sha256"]
            and info["size_in_bytes"] == expected["bytes"], "Artifact metadata changed")
    data = gh(f"actions/artifacts/{expected['id']}/zip", expected["bytes"])
    require(len(data) == expected["bytes"] and sha(data) == expected["sha256"], "Artifact bytes changed")
    return data


def zip_members(data, limit):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and len(names) <= 50, "Duplicate/excess ZIP members")
        require(sum(i.file_size for i in archive.infolist()) <= limit, "Oversized ZIP content")
        return {name: archive.read(name) for name in names}


def verify_runtime(data, source, arch):
    members = zip_members(data, 2 * 1024 * 1024)
    result = json.loads(members["runtime.json"])
    require(result["commit"] == source and result["architecture"] == arch
            and result["passed"] is True, "Runtime qualification failed or mismatched")
    require(result["checks"] == ["baseline", "restricted", "buildkit-add-run-export-and-traversal"],
            "Incomplete runtime qualification")
    observed = json.loads(members["runtime-logs/escape-result.json"])
    require(observed["outside_write"] is False and observed["timed_out"] is False,
            "Confinement was not proved")
    return result


def verify_candidate(data, lock, arch):
    zipped = zip_members(data, MAX_ARCHIVE)
    require(set(zipped) == {"candidate.tar.gz"}, "Unexpected candidate ZIP contents")
    payload = zipped["candidate.tar.gz"]
    selected = {}
    wanted = {"output/candidate.json", "output/evidence/security/gate.json"}
    wanted.update("output/bin/" + name for name in TOOLS)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        names, total = set(), 0
        for entry in archive:
            path = PurePosixPath(entry.name)
            canonical = str(path)
            require(path.parts and not path.is_absolute() and ".." not in path.parts
                    and path.parts[0] == "output" and canonical == entry.name.rstrip("/")
                    and canonical not in names, "Unsafe or duplicate candidate member")
            require(entry.isfile() or entry.isdir(), "Links and special members are forbidden")
            require(entry.isdir() or entry.name == canonical, "Trailing slash on a regular file")
            require(not entry.mode & 0o7000, "Special permission bits are forbidden")
            names.add(canonical)
            total += entry.size
            require(len(names) <= 50000 and 0 <= entry.size <= 128 * 1024 * 1024
                    and total <= MAX_EXPANDED, "Oversized candidate")
            if entry.name in wanted:
                require(entry.isfile(), "Selected input must be regular")
                require(entry.name.startswith("output/bin/") or entry.size <= 1024 * 1024,
                        "Oversized candidate metadata")
                selected[entry.name] = archive.extractfile(entry).read()
    require(set(selected) == wanted, "Missing candidate evidence or binary")
    candidate = json.loads(selected["output/candidate.json"])
    require(candidate["commit"] == lock["source"] and candidate["architecture"] == arch
            and candidate["sources_sha256"] == lock["sources_sha256"] and candidate["release"] is False,
            "Candidate provenance mismatch")
    expected = lock["architectures"][arch]["binaries"]
    require(candidate["binaries"] == expected, "Candidate inventory changed")
    require(json.loads(selected["output/evidence/security/gate.json"])["passed"] is True,
            "Candidate security gate failed")
    binaries = {name: selected["output/bin/" + name] for name in TOOLS}
    require({name: sha(value) for name, value in binaries.items()} == expected, "Binary bytes changed")
    return payload, binaries


def main():
    hosted_arch()
    require(os.geteuid() != 0, "Staging candidate bytes does not require root")
    lock = json.loads((ROOT / ".github/oci-tools/promotion.json").read_text())
    validate_lock(lock)
    run = api(f"actions/runs/{lock['qualification_run']}")
    require(run["head_sha"] == lock["source"] and run["run_attempt"] == 1
            and run["event"] == "workflow_dispatch" and run["status"] == "completed"
            and run["conclusion"] == "success"
            and run["path"] == ".github/workflows/oci-tool-qualification.yml", "Unqualified workflow run")
    jobs = api(f"actions/runs/{lock['qualification_run']}/jobs?per_page=100")
    require(jobs["total_count"] == len(jobs["jobs"]), "Incomplete job inventory")
    for arch in ("amd64", "arm64"):
        for title in ("Native source and security", "Reviewed disposable OCI runtime"):
            found = [j for j in jobs["jobs"] if j["name"] == f"{title} ({arch})"]
            require(len(found) == 1 and found[0]["conclusion"] == "success", "Required job did not pass")
    destination = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True) / "oci-promotion"
    destination.mkdir(mode=0o700, exist_ok=False)
    index = {"source": lock["source"], "qualification_run": lock["qualification_run"],
             "published": False, "builder_image_qualified": False, "architectures": {}}
    for arch in ("amd64", "arm64"):
        runtime = verify_runtime(artifact(lock, arch, "runtime"), lock["source"], arch)
        payload, binaries = verify_candidate(artifact(lock, arch, "candidate"), lock, arch)
        filename = f"oci-tools-{lock['source'][:12]}-linux-{arch}.tar.gz"
        (destination / filename).write_bytes(payload)
        context = destination / arch
        (context / "bin").mkdir(parents=True)
        for name in ("buildctl", "buildkitd", "runc"):
            path = context / "bin" / name
            path.write_bytes(binaries[name])
            path.chmod(0o755)
        shutil.copyfile(ROOT / ".github/oci-tools/builder.Dockerfile", context / "Dockerfile")
        index["architectures"][arch] = {
            "archive": filename, "archive_sha256": sha(payload), "archive_bytes": len(payload),
            "binaries": lock["architectures"][arch]["binaries"], "runtime": runtime,
            "candidate_artifact": lock["architectures"][arch]["candidate"],
            "runtime_artifact": lock["architectures"][arch]["runtime"],
        }
    (destination / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print("Prepared reviewed bytes and image contexts; publication and image qualification remain separate.")


if __name__ == "__main__":
    main()
