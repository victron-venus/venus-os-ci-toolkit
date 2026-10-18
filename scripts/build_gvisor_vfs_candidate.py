#!/usr/bin/env python3
"""Fixed public-source artifact build; no candidate runtime is ever executed."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import struct
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

BASE = "60faa657ca99f3398e04c8dbc580a848df20f2f2"
PATCH_SHA = "97e0de464f0675e1820f81798d614aeb297f8ea88d3289ac6349120813a8ac75"
LICENSE_SHA = "0fbab5c58efbdf6d31e8085214f2dd821659c03d73cff3ed2b08e98826ea1cd9"
IMAGE = ("us-central1-docker.pkg.dev/gvisor-presubmit/gvisor-presubmit-images/"
         "default_x86_64@sha256:667ce7798657a8fd80d737119b478977dc60708e140b41768099784f6d58f945")
VERSION = "vfs-candidate-20261012-97e0de464f06"
MODIFICATION_NOTICE = (
    "Modification notice for the gVisor VFS candidate\n"
    "This file is added for this candidate; it is not an upstream NOTICE file.\n\n"
    "Original project: gVisor, https://github.com/google/gvisor\n"
    f"Upstream source commit: {BASE} (release-20261012.0)\n"
    f"Candidate version marker: {VERSION}\n"
    f"Applied patch: vfs-final-symlink.patch, SHA256 {PATCH_SHA}\n"
    "Changes: final-symlink and trailing-slash resolution, the /tmp mount\n"
    "companion change, and two public syscall regression test cases.\n"
    "Changed files: pkg/sentry/vfs/resolving_path.go, runsc/boot/vfs.go,\n"
    "test/syscalls/linux/open.cc. See the included patch for exact changes.\n"
    "Public upstream review: https://github.com/google/gvisor/pull/14789\n"
    "The original Apache-2.0 LICENSE is included unchanged. No root NOTICE\n"
    "file exists in the pinned upstream source. This artifact is a modified\n"
    "candidate, not an official upstream release or runtime qualification.\n"
)
PATCH_FILES = {"pkg/sentry/vfs/resolving_path.go", "runsc/boot/vfs.go",
               "test/syscalls/linux/open.cc"}
MEMBERS = {"runsc", "containerd-shim-runsc-v1", "gvisor-bin/gvisor_sentry",
           "gvisor-bin/checkpointgofer", "gvisor-bin/gvisor-sentry-prewarmer",
           "gvisor-bin/runsc-fd-parking", "gvisor-bin/runsc-metric-server"}
BUILD_TARGETS = ["//debian:gvisor-release-tar-bz2", "//:release",
                 "//test/syscalls/linux:open_test"]
TEST_TARGETS = ["//pkg/sentry/vfs:vfs_test",
                "//runsc/prewarmer:prewarmer_single_page_test"]
LABEL = "org.gvisor.candidate.owner"
MAX_ARTIFACT = 500_000_000
DISK_FLOOR = 2_000_000_000
WORK_SECONDS = 90 * 60


class BuildError(RuntimeError):
    """A failed closed build gate."""


def require(condition, message):
    if not condition:
        raise BuildError(message)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def clean_env(home):
    """Never forward the Actions token, event, host config, or secret environment."""
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false",
        "GIT_SSH_COMMAND": "/bin/false",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
        "DOCKER_CONFIG": str(Path(home) / "docker"),
    }


def validate_event(env, event):
    require(env.get("GITHUB_ACTIONS") == "true", "GitHub Actions is required")
    require(env.get("RUNNER_ENVIRONMENT") == "github-hosted", "Hosted runner required")
    require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Manual dispatch required")
    require(env.get("RUNNER_ARCH") == "X64", "Native x64 runner required")
    repo = event.get("repository", {})
    require(repo.get("private") is False, "Public repository required")
    require(repo.get("full_name") == env.get("GITHUB_REPOSITORY"), "Repository mismatch")
    branch = repo.get("default_branch")
    require(isinstance(branch, str) and branch, "Default branch is missing")
    require(env.get("GITHUB_REF") == "refs/heads/" + branch, "Default branch required")
    require(re.fullmatch(r"[0-9a-f]{40}", env.get("GITHUB_SHA", "")), "Invalid workflow SHA")
    for name in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        require(re.fullmatch(r"[1-9][0-9]*", env.get(name, "")), "Invalid run identity")


class Supervisor:
    """Monitors only owned work; disk/deadline aborts are failures, never retries."""

    def __init__(self, env, paths, seconds=WORK_SECONDS):
        self.env, self.paths = env, paths
        self.deadline = time.monotonic() + seconds
        self.minimum_free = None

    def check(self, floor=DISK_FLOOR):
        require(time.monotonic() < self.deadline, "Work deadline exceeded")
        free = min(shutil.disk_usage(path).free for path in self.paths)
        self.minimum_free = free if self.minimum_free is None else min(self.minimum_free, free)
        require(free >= floor, "Free disk below admission/abort threshold")
        return free

    def run(self, argv, cwd=None, capture=False, extra_env=None):
        self.check()
        env = dict(self.env)
        env.update(extra_env or {})
        # Captures are bounded metadata commands, never build/pull output.
        if capture:
            timeout = min(45, max(1, self.deadline - time.monotonic()))
            result = subprocess.run(argv, cwd=cwd, env=env, check=True,
                                    text=True, capture_output=True, timeout=timeout)
            require(len(result.stdout) < 2_000_000, "Metadata output exceeded bound")
            self.check()
            return result.stdout.strip()
        proc = subprocess.Popen(argv, cwd=cwd, env=env, start_new_session=True)
        try:
            while proc.poll() is None:
                self.check()
                time.sleep(1)
            require(proc.returncode == 0, "Owned command failed with exit " + str(proc.returncode))
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait(timeout=5)


def container_args(name, owner, source, build, script, patch, uid, gid):
    require(uid > 0 and gid > 0, "Nonroot identity required")
    return ["docker", "create", "--name", name, "--label", LABEL + "=" + owner,
            "--hostname", "gvisor-builder", "--user", f"{uid}:{gid}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--cpus", "3", "--memory", "12000000000", "--memory-swap", "12000000000",
            "--pids-limit", "1024", "--ulimit", "nofile=8192:8192", "--read-only",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=1073741824,mode=1777",
            "--mount", f"type=bind,source={source},target=/workspace",
            "--mount", f"type=bind,source={build},target=/build",
            "--mount", f"type=bind,source={script},target=/control/build.py,readonly",
            "--mount", f"type=bind,source={patch},target=/control/vfs-final-symlink.patch,readonly",
            "--workdir", "/workspace", "--network", "bridge",
            "--env", "HOME=/build/home", "--env", "LANG=C.UTF-8",
            "--env", "LC_ALL=C.UTF-8", "--env", "GOMAXPROCS=2",
            "--env", "BUILD_IN_CONTAINER=1", "--env", "GIT_CONFIG_NOSYSTEM=1",
            "--env", "GIT_CONFIG_GLOBAL=/dev/null", "--entrypoint", "/usr/bin/python3",
            IMAGE, "/control/build.py", "inside"]


def owned_container_id(info, name, owner):
    require(isinstance(info, list) and len(info) == 1, "Unknown container identity")
    item = info[0]
    require(item.get("Name") == "/" + name, "Container name mismatch")
    require(item.get("Config", {}).get("Labels", {}).get(LABEL) == owner,
            "Container ownership label mismatch")
    cid = item.get("Id", "")
    require(re.fullmatch(r"[0-9a-f]{64}", cid), "Invalid container ID")
    return cid


def cleanup_container(env, name, owner):
    # Never use prune, a name wildcard, host directory deletion, or a runtime reset.
    result = subprocess.run(["docker", "inspect", name], env=env, text=True,
                            capture_output=True, timeout=20)
    if result.returncode != 0:
        # A failed inspect is not proof of absence; retain a cleanup failure.
        raise BuildError("Container cleanup could not establish owned identity")
    cid = owned_container_id(json.loads(result.stdout), name, owner)
    subprocess.run(["docker", "rm", "--force", cid], env=env, check=True, timeout=20)
    result = subprocess.run(["docker", "container", "ls", "--all", "--no-trunc",
                             "--filter", "label=" + LABEL + "=" + owner,
                             "--format", "{{.ID}}"], env=env, text=True,
                            capture_output=True, check=True, timeout=20)
    require(not result.stdout.strip(), "Owned container remained after cleanup")


def elf_metadata(data, require_static=True):
    require(len(data) >= 64 and data[:7] == b"\x7fELF\x02\x01\x01", "Not ELF64 little-endian")
    elf_type, machine = struct.unpack_from("<HH", data, 16)
    require(machine == 62 and elf_type in (2, 3), "Not an x86-64 executable")
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phsize, count = struct.unpack_from("<HH", data, 54)
    require(phsize >= 56 and 0 < count < 128 and phoff + phsize * count <= len(data),
            "Invalid ELF program headers")
    types = [struct.unpack_from("<I", data, phoff + i * phsize)[0] for i in range(count)]
    needed = False
    for i, kind in enumerate(types):
        if kind == 2:  # PT_DYNAMIC; DT_NEEDED would require an external library.
            offset = struct.unpack_from("<Q", data, phoff + i * phsize + 8)[0]
            length = struct.unpack_from("<Q", data, phoff + i * phsize + 32)[0]
            require(offset + length <= len(data) and length % 16 == 0, "Invalid dynamic segment")
            needed |= any(struct.unpack_from("<q", data, p)[0] == 1
                          for p in range(offset, offset + length, 16))
    if require_static:
        require(3 not in types and not needed, "Unexpected dynamic interpreter/library")
    return {"class": "ELF64", "machine": "x86-64", "type": elf_type,
            "interpreter_present": 3 in types, "needed_library_present": needed}


def validate_stamp(path, expected_version):
    path = Path(path)
    require(path.is_file() and path.stat().st_size <= 65_536, "Missing/oversize stable status file")
    data = path.read_bytes()
    values = [line.split(maxsplit=1)[1:] for line in data.decode("utf-8").splitlines()
              if line.split(maxsplit=1)[:1] == ["STABLE_VERSION"]]
    require(values == [[expected_version]], "Missing, duplicate or wrong STABLE_VERSION")
    return {"stable_version": expected_version, "file_sha256": hashlib.sha256(data).hexdigest(),
            "check": "exact STABLE_VERSION in generated Bazel stable-status.txt"}


def validate_provenance_files(directory):
    expected = {"LICENSE": LICENSE_SHA, "vfs-final-symlink.patch": PATCH_SHA,
                "MODIFICATIONS.txt": hashlib.sha256(MODIFICATION_NOTICE.encode()).hexdigest()}
    for name, wanted in expected.items():
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "Missing or invalid provenance file: " + name)
        require(digest(path) == wanted, "Provenance file digest mismatch: " + name)
    return expected


def validate_bundle(path, expected_version=None):
    records, total = {}, 0
    require(Path(path).stat().st_size <= MAX_ARTIFACT, "Archive too large")
    with tarfile.open(path, "r:bz2") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            if member.isdir():
                require(name.rstrip("/") in (".", "gvisor-bin"), "Unexpected archive directory")
                continue
            require(name in MEMBERS and name not in records, "Unexpected/duplicate archive entry")
            require(member.isfile() and member.mode == 0o755, "Invalid archive file type/mode")
            require(0 < member.size <= 128_000_000, "Invalid member size")
            total += member.size
            require(total <= 400_000_000, "Expanded bundle exceeds bound")
            stream = archive.extractfile(member)
            require(stream is not None, "Missing archive data")
            data = stream.read(member.size + 1)
            require(len(data) == member.size, "Truncated member")
            record = elf_metadata(data)
            if name == "runsc" and expected_version is not None:
                marker_count = data.count(expected_version.encode("ascii"))
                require(marker_count > 0, "Candidate version marker missing from runsc bytes")
                record["candidate_version_marker"] = {
                    "expected": expected_version, "byte_occurrences": marker_count,
                    "check": "exact candidate marker byte substring, not runtime execution or symbol decoding",
                }
            record.update(size=member.size, sha256=hashlib.sha256(data).hexdigest(), mode="0755")
            records[name] = record
    require(set(records) == MEMBERS, "Incomplete seven-file bundle")
    require(records["gvisor-bin/gvisor-sentry-prewarmer"]["size"] <= 4096,
            "Prewarmer exceeds one page")
    return records


def validate_test_xml(path):
    root = ET.fromstring(Path(path).read_bytes())
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    require(suites, "Missing unit test suite")
    tests = 0
    for suite in suites:
        for key in ("failures", "errors", "skipped", "disabled"):
            require(int(suite.get(key, "0")) == 0, "Failed or skipped unit tests")
        tests += int(suite.get("tests", "0"))
    require(tests > 0, "No executed unit tests")
    require(not list(root.iter("skipped")), "Skipped unit test element")
    require(not list(root.iter("failure")) and not list(root.iter("error")), "Unit test failure")
    return tests


def bazel(command, *tail):
    return ["/usr/local/bin/bazel", "--host_jvm_args=-Xmx2g",
            "--output_user_root=/build/bazel", command, *tail]


def build_flags():
    return ["--config=x86_64", "-c", "opt", "--jobs=2", "--local_resources=cpu=3",
            "--local_resources=memory=8000", "--spawn_strategy=processwrapper-sandbox",
            "--strategy=Genrule=processwrapper-sandbox", "--repository_cache=/build/repository-cache",
            "--modify_execution_info=^(GoStandardLibraryAnalysis|GoStaticAnalysis)$=-no-sandbox",
            "--repo_env=TMPDIR=/build/tmp",
            "--symlink_prefix=/build/links/", "--lockfile_mode=update", "--color=no", "--curses=no"]


def inside():
    require(os.environ.get("BUILD_IN_CONTAINER") == "1" and os.getuid() > 0,
            "Only the owned nonroot build container may run inside mode")
    source, build = Path("/workspace"), Path("/build")
    for name in ("home", "links", "output"):
        (build / name).mkdir(exist_ok=True)
    output = build / "output"
    temporary = build / "tmp"
    temporary.mkdir(mode=0o700)  # Fresh owned directory; reject existing paths/symlinks.
    env = clean_env(build / "home")
    env["GOMAXPROCS"] = "2"
    # Gazelle preserves TMPDIR; Bazel also uses the client value for local actions.
    env["TMPDIR"] = str(temporary)
    sup = Supervisor(env, [source, build])
    before = sup.run(["git", "rev-parse", "HEAD^{tree}"], cwd=source, capture=True)
    require(sup.run(["git", "describe", "--exact-match", "--tags", "HEAD"],
                    cwd=source, capture=True) == VERSION, "Candidate tag mismatch")
    require(digest(source / "LICENSE") == LICENSE_SHA, "Upstream license digest mismatch")
    patch = Path("/control/vfs-final-symlink.patch")
    require(digest(patch) == PATCH_SHA, "Mounted patch digest mismatch")
    bazel_version = sup.run(bazel("version", "--gnu_format"), cwd=source, capture=True)
    require("bazel 8.8.1" in bazel_version, "Unexpected Bazel version")
    flags = build_flags()
    sup.run(bazel("build", *flags, *BUILD_TARGETS), cwd=source)
    # rules_go 0.63 defaults passing-test detail off; request the named events
    # its XML reporter needs. Empty/failed/skipped suites still fail validation.
    sup.run(bazel("test", *flags, "--nocache_test_results", "--test_output=errors",
                  "--test_env=GO_TEST_WRAP_TESTV=1", "--local_test_jobs=1",
                  "--test_timeout=300", *TEST_TARGETS), cwd=source)
    bindir = Path(sup.run(bazel("info", *flags, "bazel-bin"), cwd=source, capture=True)).resolve()
    testdir = Path(sup.run(bazel("info", *flags, "bazel-testlogs"), cwd=source, capture=True)).resolve()
    for path in (bindir, testdir):
        require(path.is_relative_to(build), "Unexpected Bazel output path")
    archive = bindir / "debian/gvisor.tar.bz2"
    members = validate_bundle(archive, expected_version=VERSION)
    shutil.copyfile(archive, output / "gvisor-candidate.tar.bz2")
    test_binary = bindir / "test/syscalls/linux/open_test"
    require(test_binary.stat().st_size <= 100_000_000, "Open test binary too large")
    open_meta = elf_metadata(test_binary.read_bytes(), require_static=False)
    shutil.copyfile(test_binary, output / "open_test")
    cases = {}
    for label in TEST_TARGETS:
        package, name = label.removeprefix("//").split(":")
        xml = testdir / package / name / "test.xml"
        cases[label] = validate_test_xml(xml)
        shutil.copyfile(xml, output / (name + ".xml"))
    sup.run(["git", "diff", "--exit-code", "HEAD", "--"], cwd=source)
    require(sup.run(["git", "rev-parse", "HEAD^{tree}"], cwd=source, capture=True) == before,
            "Source tree identity changed")
    lock = source / "MODULE.bazel.lock"
    require(lock.is_file() and not lock.is_symlink(), "Resolved module lockfile missing")
    require(lock.stat().st_size < 10_000_000, "Module lockfile too large")
    shutil.copyfile(lock, output / "MODULE.bazel.lock")
    output_base = Path(sup.run(bazel("info", *flags, "output_base"), cwd=source, capture=True)).resolve()
    require(output_base.is_relative_to(build), "Unexpected output base")
    output_path = Path(sup.run(bazel("info", *flags, "output_path"), cwd=source, capture=True)).resolve()
    require(output_path.is_relative_to(build), "Unexpected Bazel output path")
    stamp = output_path / "stable-status.txt"
    stamp_check = validate_stamp(stamp, VERSION)
    shutil.copyfile(stamp, output / "stable-status.txt")
    shutil.copyfile(source / "LICENSE", output / "LICENSE")
    shutil.copyfile(patch, output / "vfs-final-symlink.patch")
    (output / "MODIFICATIONS.txt").write_text(MODIFICATION_NOTICE)
    provenance_files = validate_provenance_files(output)
    sdk_records = []
    for sdk_go in sorted((output_base / "external").glob("*/bin/go")):
        resolved_go = sdk_go.resolve()
        require(resolved_go.is_relative_to(build), "Go SDK is outside the owned build tree")
        version = sup.run([str(resolved_go), "version"], capture=True,
                          extra_env={"GOTOOLCHAIN": "local"})
        require(version == "go version go1.27.2 linux/amd64", "Unexpected Go SDK version")
        sdk_records.append({"repository": sdk_go.parent.parent.name, "version": version,
                            "go_executable_sha256": digest(resolved_go)})
    require(sdk_records, "Resolved Go SDK identity not found")
    tools = {}
    for name in ("bazel", "gcc", "ld", "clang"):
        tool = shutil.which(name, path=env["PATH"])
        require(tool is not None, "Required builder tool missing")
        tools[name] = {"sha256": digest(Path(tool).resolve()), "path": tool}
    write_json(output / "build-receipt.json", {
        "status": "artifact-and-allowed-unit-tests-passed-not-runtime-qualified",
        "source_base": BASE, "patch_sha256": PATCH_SHA, "candidate_version": VERSION,
        "candidate_commit": sup.run(["git", "rev-parse", "HEAD"], cwd=source, capture=True),
        "candidate_tree": before, "builder": IMAGE, "bazel_version": bazel_version,
        "available_tools_not_claimed_all_used": tools, "build_targets": BUILD_TARGETS,
        "resolved_go_sdks": sdk_records,
        "executed_test_targets": TEST_TARGETS, "unit_test_cases": cases,
        "bundle_members": members, "open_test_elf": open_meta,
        "generated_source_stamp": stamp_check, "provenance_files": provenance_files,
        "candidate_runtime_executed": False, "boot_tests_executed": False,
    })


def stage_artifacts(directory, destination):
    allowed = {"gvisor-candidate.tar.bz2", "open_test", "build-receipt.json",
               "MODULE.bazel.lock", "vfs_test.xml", "prewarmer_single_page_test.xml",
               "LICENSE", "MODIFICATIONS.txt", "vfs-final-symlink.patch", "stable-status.txt"}
    require(directory.is_dir() and not directory.is_symlink(), "Invalid artifact directory")
    require({p.name for p in directory.iterdir()} == allowed, "Unexpected artifact file set")
    total, sums = 0, {}
    for name in sorted(allowed):
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "Invalid artifact file")
        total += path.stat().st_size
        require(total <= MAX_ARTIFACT - 1_000_000, "Artifact size budget exceeded")
        sums[name] = {"sha256": digest(path), "bytes": path.stat().st_size}
        if name == "gvisor-candidate.tar.bz2":
            h = hashlib.sha512()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1 << 20), b""):
                    h.update(chunk)
            sums[name]["sha512"] = h.hexdigest()
    validate_bundle(directory / "gvisor-candidate.tar.bz2", expected_version=VERSION)
    validate_stamp(directory / "stable-status.txt", VERSION)
    validate_provenance_files(directory)
    for name in ("vfs_test.xml", "prewarmer_single_page_test.xml"):
        validate_test_xml(directory / name)
    for name in sorted(allowed):
        shutil.copyfile(directory / name, destination / name)
    write_json(destination / "artifact-manifest.json", sums)


def host_build():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    validate_event(os.environ, event)
    require(platform.system() == "Linux" and platform.machine() == "x86_64", "Native Linux required")
    require(os.getuid() > 0 and os.getgid() > 0, "Nonroot hosted user required")
    token = os.environ["GITHUB_RUN_ID"] + "-" + os.environ["GITHUB_RUN_ATTEMPT"]
    temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    root = temp / ("gvisor-vfs-build-" + token)
    artifacts = temp / ("gvisor-vfs-artifacts-" + token)
    root.mkdir(mode=0o700)
    artifacts.mkdir(mode=0o700)
    home, source, build = (root / name for name in ("home", "source", "build"))
    for directory in (home, source, build):
        directory.mkdir()
    env = clean_env(home)
    sup = Supervisor(env, [root])
    container_name = "gvisor-vfs-" + token
    create_attempted, failure = False, None
    receipt = {"status": "failed", "workflow_sha": os.environ["GITHUB_SHA"],
               "source_base": BASE, "patch_sha256": PATCH_SHA, "builder": IMAGE,
               "candidate_runtime_executed": False, "cleanup": "not-needed"}
    def interrupted(signum, _frame):
        raise BuildError("Build interrupted by signal " + str(signum))

    previous_signals = {s: signal.signal(s, interrupted)
                        for s in (signal.SIGTERM, signal.SIGINT, signal.SIGALRM)}
    signal.alarm(WORK_SECONDS)
    try:
        script = Path(__file__).resolve()
        repo = script.parent.parent
        require(sup.run(["git", "rev-parse", "HEAD"], cwd=repo, capture=True) ==
                os.environ["GITHUB_SHA"], "Workflow checkout does not match event SHA")
        patch = repo / "patches/gvisor/vfs-final-symlink.patch"
        require(digest(patch) == PATCH_SHA, "Patch digest mismatch")
        info = json.loads(sup.run(["docker", "info", "--format", "{{json .}}"], capture=True))
        docker_root = Path(info["DockerRootDir"]).resolve(strict=True)
        sup.paths.append(docker_root)
        available = next(int(line.split()[1]) * 1024 for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))
        require(available >= 13_000_000_000 and (os.cpu_count() or 0) >= 4,
                "Hosted memory/CPU admission gate failed")
        receipt["preflight"] = {"free_bytes": sup.check(12_000_000_000),
                                "mem_available_bytes": available, "cpu": os.cpu_count(),
                                "runner_image": os.environ.get("ImageOS"),
                                "runner_image_version": os.environ.get("ImageVersion"),
                                "docker_version": info.get("ServerVersion")}
        sup.run(["docker", "pull", IMAGE])
        image = json.loads(sup.run(["docker", "image", "inspect", IMAGE], capture=True))[0]
        require(image.get("Architecture") == "amd64" and image.get("Os") == "linux", "Wrong builder platform")
        require(IMAGE in image.get("RepoDigests", []), "Pulled builder digest mismatch")
        receipt["image_size_bytes"] = image.get("Size")
        git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        sup.run([*git, "init", "--initial-branch=build"], cwd=source)
        sup.run([*git, "remote", "add", "origin", "https://github.com/google/gvisor.git"], cwd=source)
        sup.run([*git, "fetch", "--depth=1", "origin", BASE], cwd=source)
        sup.run([*git, "checkout", "--detach", "FETCH_HEAD"], cwd=source)
        require(sup.run([*git, "rev-parse", "HEAD"], cwd=source, capture=True) == BASE, "Base mismatch")
        sup.run([*git, "apply", "--check", str(patch)], cwd=source)
        sup.run([*git, "apply", str(patch)], cwd=source)
        changed = set(sup.run([*git, "diff", "--name-only"], cwd=source, capture=True).splitlines())
        require(changed == PATCH_FILES, "Unexpected patched paths")
        sup.run([*git, "add", "--", *sorted(PATCH_FILES)], cwd=source)
        identity = {"GIT_AUTHOR_NAME": "gVisor candidate builder", "GIT_COMMITTER_NAME": "gVisor candidate builder",
                    "GIT_AUTHOR_EMAIL": "candidate@example.invalid", "GIT_COMMITTER_EMAIL": "candidate@example.invalid",
                    "GIT_AUTHOR_DATE": "2026-10-18T00:00:00Z", "GIT_COMMITTER_DATE": "2026-10-18T00:00:00Z"}
        sup.run([*git, "commit", "-m", "Apply public VFS candidate " + PATCH_SHA], cwd=source, extra_env=identity)
        sup.run([*git, "-c", "tag.gpgSign=false", "tag", VERSION], cwd=source)
        receipt["candidate_commit"] = sup.run([*git, "rev-parse", "HEAD"], cwd=source, capture=True)
        receipt["candidate_tree"] = sup.run([*git, "rev-parse", "HEAD^{tree}"], cwd=source, capture=True)
        receipt["free_before_bazel_bytes"] = sup.check(8_000_000_000)
        args = container_args(container_name, token, source, build, script, patch, os.getuid(), os.getgid())
        create_attempted = True
        cid = sup.run(args, capture=True)
        require(re.fullmatch(r"[0-9a-f]{64}", cid), "Unexpected create output")
        sup.run(["docker", "start", "--attach", cid])
        state = json.loads(sup.run(["docker", "inspect", container_name], capture=True))
        require(owned_container_id(state, container_name, token) == cid, "Container ID changed")
        require(state[0]["State"]["ExitCode"] == 0 and not state[0]["State"].get("OOMKilled"),
                "Builder container failed or was OOM killed")
        sup.run([*git, "diff", "--exit-code", "HEAD", "--"], cwd=source)
        require(sup.run([*git, "rev-parse", "HEAD^{tree}"], cwd=source, capture=True) ==
                receipt["candidate_tree"], "Source tree changed during build")
        stage_artifacts(build / "output", artifacts)
        receipt["status"] = "artifact-and-allowed-unit-tests-passed-not-runtime-qualified"
    except Exception as exc:  # Report unexpected faults as failures, without dumping context.
        # No environment/command dump and no arbitrary exception output in artifacts.
        failure = exc
        receipt["error_type"] = type(exc).__name__
        if isinstance(exc, BuildError):
            receipt["gate"] = str(exc)
    finally:
        signal.alarm(0)  # The separate cleanup/upload reserve starts here.
        if create_attempted:
            try:
                cleanup_container(env, container_name, token)
                receipt["cleanup"] = "owned-container-removed-and-absence-verified"
            except Exception as exc:  # A cleanup fault must never yield successful delivery.
                receipt["cleanup"] = "failed-closed"
                receipt["cleanup_error_type"] = type(exc).__name__
                receipt["status"] = "failed"
                failure = failure or exc
        receipt["minimum_observed_free_bytes"] = sup.minimum_free
        receipt["temporary_files"] = "owned-files-left-for-hosted-VM-disposal"
        write_json(artifacts / "host-receipt.json", receipt)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)
    require(failure is None, "Build/cleanup failed; inspect public receipt and streamed log")


def main():
    require(len(sys.argv) == 2 and sys.argv[1] in ("build", "inside"), "Expected build or inside mode")
    if sys.argv[1] == "inside":
        inside()
    else:
        host_build()


if __name__ == "__main__":
    main()
