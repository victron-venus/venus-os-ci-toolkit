#!/usr/bin/env python3
"""Opt-in rootful OCI qualification on a reviewed, disposable hosted worker."""

import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import time

from build_oci_tools import MANIFEST, digest, hosted_arch, require, run, stage_dir, write_json


def review_gate(env, head):
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Runtime tests are dispatch-only")
    require(env.get("QUALIFY_RUNTIME") == "true", "Runtime tests were not selected")
    reviewed = env.get("REVIEWED_COMMIT", "")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", reviewed)) and reviewed == head,
            "Recipe approval must name the exact checked-out commit")
    hosted_arch(env)


def verify_binaries(output, head, arch):
    candidate = json.loads((output / "candidate.json").read_text())
    require(candidate["commit"] == head and candidate["architecture"] == arch
            and candidate["sources_sha256"] == digest(MANIFEST), "Candidate provenance mismatch")
    require(json.loads((output / "evidence/security/gate.json").read_text())["passed"] is True,
            "Binary security gate did not pass")
    expected = {}
    for name in ("buildkit", "runc"):
        expected.update(json.loads((output / f"evidence/{name}/provenance.json").read_text())["binaries"])
    require(set(expected) == {"buildctl", "buildkitd", "runc", "oci-probe"}, "Incomplete candidate set")
    require(expected == candidate["binaries"], "Candidate binary inventory mismatch")
    for name, value in expected.items():
        require(digest(output / "bin" / name) == value, "Candidate changed after scan: " + name)


def run_container(root, binaries, mode):
    bundle = root / mode
    (bundle / "rootfs").mkdir(parents=True)
    shutil.copy2(binaries / "oci-probe", bundle / "rootfs/probe")
    runc = binaries / "runc"
    run([runc, "spec"], cwd=bundle)
    config = json.loads((bundle / "config.json").read_text())
    config["root"]["readonly"] = False
    config["process"].update(terminal=False, args=["/probe", mode], cwd="/")
    config["process"]["env"] = ["PATH=/", "GOMAXPROCS=1"]
    # Each container gets a fresh, isolated cgroup; no systemd/global policy changes.
    config["linux"]["cgroupsPath"] = "/oci-tool-qualification-" + str(os.getpid()) + "/" + mode
    if mode == "restricted":
        config["linux"]["seccomp"] = {
            "defaultAction": "SCMP_ACT_ALLOW",
            "syscalls": [{"names": ["getppid"], "action": "SCMP_ACT_ERRNO", "errnoRet": 1}],
        }
        config["linux"].setdefault("resources", {})["devices"] = [
            {"allow": False, "access": "rwm"},
            # runc must still create the device nodes while preparing the rootfs.
            # The probe tests read/write access, not the ability to create nodes.
            {"allow": True, "type": "a", "access": "m"},
            {"allow": True, "type": "c", "major": 1, "minor": 3, "access": "rwm"},
        ]
    else:
        config["linux"].pop("seccomp", None)
        config["linux"].setdefault("resources", {})["devices"] = [{"allow": True, "access": "rwm"}]
    write_json(bundle / "config.json", config)
    state = root / "runc-state"
    cid = "qualification-" + mode
    try:
        output = run([runc, "--root", state, "run", "--bundle", bundle, cid],
                     log=root / (mode + ".log"), timeout=60)
        require("OCI_PROBE_OK " + mode in output, "Container probe did not finish")
    finally:
        # Only this probe's ID and isolated state directory can be affected.
        subprocess.run([str(runc), "--root", str(state), "delete", "--force", cid],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)


def make_archive(path, entries):
    with tarfile.open(path, "w") as tf:
        for name, data, kind, link in entries:
            member = tarfile.TarInfo(name)
            member.type, member.linkname, member.mode = kind, link, 0o644
            member.size = len(data)
            tf.addfile(member, io.BytesIO(data) if data else None)


def require_confinement_failure(text):
    # BuildKit echoes the Dockerfile (and escape.tar) on unrelated failures.
    # Require a confinement diagnostic, not a fixture name or generic error.
    require(re.search(
        r"path traversal (?:detected|blocked|forbidden)|"
        r"(?:invalid|unsafe|forbidden) (?:archive )?(?:path|symlink|hardlink)\b|"
        r"(?:path|symlink|hardlink|entry)[^\n]*outside (?:the )?(?:root|destination|archive)|"
        r"(?:escapes?|escaped) (?:the )?(?:root|destination|archive)\b",
        text, re.IGNORECASE) is not None,
        "Negative ADD failed for an unproven reason")


def build_fixture(root, binaries):
    context = root / "context"
    context.mkdir()
    shutil.copy2(binaries / "oci-probe", context / "probe")
    make_archive(context / "data.tar", [
        ("file", b"archive-payload", tarfile.REGTYPE, ""),
        ("sym", b"", tarfile.SYMTYPE, "file"),
        ("hard", b"", tarfile.LNKTYPE, "file"),
    ])
    (context / "Dockerfile").write_text(
        'FROM scratch\nCOPY probe /probe\nADD data.tar /data/\nRUN ["/probe", "build"]\n')
    endpoint = "unix://" + str(root / "buildkit.sock")
    command = [str(binaries / "buildkitd"), "--addr", endpoint, "--root", str(root / "buildkit-state"),
               "--oci-worker=true", "--containerd-worker=false", "--oci-worker-snapshotter=native",
               "--oci-worker-net=host", "--oci-worker-binary", str(binaries / "runc")]
    with (root / "buildkitd.log").open("w") as log:
        daemon = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                require(daemon.poll() is None, "Candidate buildkitd exited before readiness")
                result = subprocess.run([str(binaries / "buildctl"), "--addr", endpoint, "debug", "workers"],
                                        capture_output=True, text=True, timeout=10)
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise ValueError("Candidate BuildKit readiness timed out")
            output = root / "export"
            common = [binaries / "buildctl", "--addr", endpoint, "build", "--frontend", "dockerfile.v0",
                      "--local", "context=" + str(context), "--local", "dockerfile=" + str(context),
                      "--opt", "force-network-mode=none", "--no-cache"]
            run([*common, "--output", "type=local,dest=" + str(output)], log=root / "build.log", timeout=180)
            require((output / "result").read_text() == "BUILD_PROBE_OK\n", "Wrong BuildKit result")
            require((output / "data/file").read_bytes() == b"archive-payload", "ADD extraction failed")
            # Exercise the daemon's real ADD path with a hostile parent traversal.
            sentinel = root / "host-sentinel"
            sentinel.write_bytes(b"unchanged")
            make_archive(context / "escape.tar", [
                ("../../../../../../../../../../" + str(sentinel).lstrip("/"), b"escaped", tarfile.REGTYPE, ""),
            ])
            (context / "Dockerfile").write_text('FROM scratch\nADD escape.tar /data/\n')
            result = subprocess.run(list(map(str, [*common, "--output", "type=local,dest=" + str(root / "escape-export")])),
                                    capture_output=True, text=True, timeout=120)
            (root / "escape-build.log").write_text(result.stdout + result.stderr)
            require(sentinel.read_bytes() == b"unchanged", "ADD escaped onto the worker")
            require(daemon.poll() is None, "Traversal probe crashed the daemon")
            run([binaries / "buildctl", "--addr", endpoint, "debug", "workers"],
                log=root / "post-traversal-workers.log", timeout=15)
            if result.returncode:
                require_confinement_failure(result.stderr + result.stdout)
            # Rejection or confinement is acceptable; record which actually happened.
            write_json(root / "escape-result.json", {"exit_code": result.returncode, "outside_write": False})
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=20)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=10)


def main():
    head = run(["git", "-c", "safe.directory=" + str(Path.cwd()), "rev-parse", "HEAD"]).strip()
    review_gate(os.environ, head)
    require(os.geteuid() == 0, "This isolated runtime qualification needs root")
    require(Path("/sys/fs/cgroup/cgroup.controllers").is_file(), "cgroup v2 is required; no silent skip")
    stage = stage_dir()
    output = stage / "output"
    verify_binaries(output, head, hosted_arch())
    root = stage / "runtime"
    root.mkdir(mode=0o700, exist_ok=False)
    result = {"passed": False, "commit": head, "architecture": hosted_arch(),
              "kernel": run(["uname", "-a"]).strip(), "checks": []}
    try:
        for mode in ("baseline", "restricted"):
            run_container(root, output / "bin", mode)
            result["checks"].append(mode)
        build_fixture(root, output / "bin")
        result["checks"].append("buildkit-add-run-export-and-traversal")
        result["passed"] = True
    finally:
        write_json(output / "evidence/runtime.json", result)
        # Keep bounded logs, not complete container filesystems in artifacts.
        logs = output / "evidence/runtime-logs"
        logs.mkdir(exist_ok=True)
        for path in root.iterdir():
            if path.is_file() and path.suffix in (".log", ".json"):
                shutil.copyfile(path, logs / path.name)


if __name__ == "__main__":
    main()
