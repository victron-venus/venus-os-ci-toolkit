#!/usr/bin/env python3
"""Qualify a native, unpublished OCI builder on a disposable hosted worker."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile

import build_oci_tools as source
import build_runner_image_artifacts as runner
import oci_builder_assembly as assembly
import prepare_oci_promotion as promotion

ROOT = Path(__file__).resolve().parents[1]
REQUIRE = promotion.require
MAX_ROOTFS = 512 * 1024 * 1024
INDEX_FILENAME = "index.json"
DOCKER_ARCHIVE = "builder.docker.tar"
ENTRYPOINT = ["/usr/bin/buildkitd", "--oci-worker=true", "--containerd-worker=false",
              "--oci-worker-snapshotter=native", "--oci-worker-net=host",
              "--oci-worker-binary=/usr/bin/buildkit-runc"]


def save(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def admit(environment):
    arch = source.hosted_arch(environment)
    REQUIRE(environment.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "Manual qualification required")
    REQUIRE(environment.get("GITHUB_REPOSITORY") == promotion.REPO, "Canonical repository required")
    REQUIRE(environment.get("GITHUB_REF") == "refs/heads/main", "Default branch required")
    commit = environment.get("GITHUB_SHA", "")
    REQUIRE(re.fullmatch(r"[0-9a-f]{40}", commit)
            and environment.get("REVIEWED_COMMIT") == commit, "Exact reviewed recipe required")
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        REQUIRE(re.fullmatch(r"[1-9]\d*", environment.get(key, ""), flags=re.ASCII), "Invalid workflow identity")
    REQUIRE(environment["GITHUB_RUN_ATTEMPT"] == "1", "Retries need a fresh reviewed dispatch")
    return arch, commit


def validate_image(item, arch, expected_id):
    REQUIRE(item["Id"] == expected_id and item["Architecture"] == arch and item["Os"] == "linux",
            "Builder image identity or native platform differs")
    config = item["Config"]
    REQUIRE(config["Entrypoint"] == ENTRYPOINT and config["User"] == "0:0",
            "Builder execution configuration differs")
    REQUIRE("PATH=/usr/bin" in config["Env"] and not config.get("Cmd"), "Unexpected builder command")
    REQUIRE(set(config.get("Volumes", {})) == {"/var/lib/buildkit"}, "Unexpected image volumes")


def check_scan(report, expected_id, required_binaries):
    REQUIRE(report.get("ArtifactType") == "container_image", "Full image scan required")
    REQUIRE(report.get("Metadata", {}).get("ImageID") in {expected_id, expected_id[7:]},
            "Scanner image identity differs")
    results = report.get("Results", [])
    REQUIRE(any(row.get("Class") == "os-pkgs" and row.get("Packages") for row in results),
            "Final operating-system inventory missing")
    covered = {Path(row.get("Target", "")).name for row in results
               if row.get("Type") == "gobinary" and row.get("Packages")}
    REQUIRE(required_binaries <= covered, "Final executable inventory incomplete")
    blockers = [v for row in results for v in row.get("Vulnerabilities", [])
                if v.get("Severity") in {"HIGH", "CRITICAL"}]
    REQUIRE(not blockers, "Final image has blocking vulnerabilities")
    return {"passed": True, "binaries": sorted(covered), "blocking_findings": len(blockers)}


def scan_image(supervisor, scanner, work, report_path, expected_id):
    # Trivy accepts Docker tar archives or OCI layout directories, not OCI tar archives.
    # The caller has already verified this same-solve export against the OCI bytes.
    supervisor.run([str(scanner), "image", "--input", str(work / DOCKER_ARCHIVE), "--scanners", "vuln",
                    "--list-all-pkgs", "--format", "json", "--output", str(report_path),
                    "--cache-dir", str(work / "scanner-cache"), "--ignorefile", "/dev/null",
                    "--timeout", "10m"], seconds=660)
    return check_scan(json.loads(report_path.read_text()), expected_id,
                      {"buildkitd", "buildctl", "buildkit-runc", "oci-cgroup-reader"})


def verify_oci(path, arch, expected_files, check):
    """Bind every blob and final selected file without extracting the filesystem."""
    REQUIRE(0 < path.stat().st_size <= MAX_ROOTFS, "Oversized qualification OCI archive")
    archive_sha = runner.oci.digest_file(path)
    archive = runner.oci.Archive(path, archive_sha, path.stat().st_size)
    try:
        REQUIRE(runner.oci.json_document(archive.read("oci-layout")) == {"imageLayoutVersion": "1.0.0"},
                "Unexpected OCI layout")
        index = runner.oci.json_document(archive.read(INDEX_FILENAME))
        REQUIRE(index.get("schemaVersion") == 2 and len(index["manifests"]) == 1, "Ambiguous OCI root")
        descriptor = index["manifests"][0]
        if descriptor.get("mediaType") == runner.oci.INDEX:
            index = archive.json_descriptor(descriptor, runner.oci.INDEX)
            REQUIRE(index.get("schemaVersion") == 2 and len(index["manifests"]) == 1,
                    "Exactly one native image required")
            descriptor = index["manifests"][0]
        manifest = archive.json_descriptor(descriptor, runner.oci.MANIFEST)
        REQUIRE(manifest.get("schemaVersion") == 2, "Invalid OCI manifest")
        config = archive.json_descriptor(manifest["config"], runner.oci.CONFIG)
        REQUIRE(config.get("os") == "linux" and config.get("architecture") == arch,
                "OCI image native platform differs")
        REQUIRE(config["rootfs"]["type"] == "layers"
                and len(config["rootfs"]["diff_ids"]) == len(manifest["layers"]), "OCI layer inventory differs")
        tree, expanded = {}, 0
        for layer, expected_diff in zip(manifest["layers"], config["rootfs"]["diff_ids"], strict=True):
            name = archive.descriptor(layer)
            with archive.tar.extractfile(archive.files[name]) as raw:
                entries, size, diff = runner.oci.read_effective_layer(raw, layer, MAX_ROOTFS - expanded, check)
            REQUIRE(diff == expected_diff, "OCI uncompressed layer digest differs")
            expanded += size
            tree = runner.oci.merge_effective_layer(tree, entries)
        files = {name: runner.oci.effective_regular(tree, name)["sha256"] for name in expected_files}
        REQUIRE(files == expected_files, "Actual OCI payload differs")
        REQUIRE(archive.used == set(archive.files) - {INDEX_FILENAME, "oci-layout"}, "Unreferenced OCI blobs")
        archive.unchanged()
        return {"archive_sha256": archive_sha, "archive_bytes": path.stat().st_size,
                "manifest": descriptor["digest"], "config": manifest["config"]["digest"],
                "layers": manifest["layers"], "diff_ids": config["rootfs"]["diff_ids"],
                "runtime_config": config["config"], "architecture": arch,
                "files": files, "uncompressed_bytes": expanded}
    finally:
        archive.close()


def inspect_image(sup, image):
    values = json.loads(sup.run(["docker", "image", "inspect", image], capture=True))
    REQUIRE(len(values) == 1, "Ambiguous image")
    return values[0]


def verify_docker_export(path, oci_proof, tag):
    """Prove that the importable same-solve archive represents the verified OCI image."""
    REQUIRE(0 < path.stat().st_size <= MAX_ROOTFS, "Oversized Docker export")
    with tarfile.open(path, "r:") as archive:
        files = {}
        for entry in archive:
            name = entry.name.rstrip("/")
            REQUIRE(name == str(PurePosixPath(name)) and not name.startswith("/")
                    and ".." not in PurePosixPath(name).parts and name not in files,
                    "Unsafe or duplicate Docker export member")
            REQUIRE(entry.isfile() or entry.isdir(), "Special Docker export member")
            REQUIRE(entry.isdir() or entry.name == name, "Docker regular-file path has a trailing slash")
            REQUIRE(not entry.issparse() and 0 <= entry.size <= MAX_ROOTFS
                    and len(files) < 5000, "Excessive Docker export")
            files[name] = entry
        def read_json(name):
            REQUIRE(name in files and files[name].isfile() and files[name].size <= 1024 * 1024,
                    "Invalid Docker metadata")
            return runner.oci.json_document(archive.extractfile(files[name]).read())
        manifest = read_json("manifest.json")
        REQUIRE(len(manifest) == 1 and manifest[0]["RepoTags"] == [tag], "Docker export identity differs")
        config_path = manifest[0]["Config"]
        config = read_json(config_path)
        with archive.extractfile(files[config_path]) as stream:
            REQUIRE("sha256:" + runner.oci.digest_stream(stream) == oci_proof["config"],
                    "Docker and OCI configuration bytes differ")
        REQUIRE(config["rootfs"]["diff_ids"] == oci_proof["diff_ids"], "Docker and OCI rootfs differ")
        layers = manifest[0]["Layers"]
        REQUIRE(len(layers) == len(oci_proof["diff_ids"]), "Docker export layer count differs")
        for name, expected in zip(layers, oci_proof["diff_ids"], strict=True):
            REQUIRE(name in files and files[name].isfile(), "Missing Docker layer")
            with archive.extractfile(files[name]) as stream:
                REQUIRE("sha256:" + runner.oci.digest_stream(stream) == expected, "Docker layer bytes differ")
    return {"archive_sha256": runner.oci.digest_file(path), "archive_bytes": path.stat().st_size,
            "config": oci_proof["config"], "diff_ids": oci_proof["diff_ids"], "same_solve": True}


def exercise_builder(sup, name, image, arch, observed, work, recipe, record):
    def check():
        current = runner.inspect_builder(sup, name, name, image)
        REQUIRE(current["Id"] == observed["Id"], "Builder identity changed")
        runner.cgroup.sample(current["State"]["Pid"], record["cgroup_proof"])
    check()
    result = subprocess.run(["docker", "exec", observed["Id"], "/usr/bin/oci-cgroup-reader",
                             "/proc/self/cgroup"], env=sup.env, capture_output=True, timeout=15)
    REQUIRE(result.returncode == 2 and result.stdout == b""
            and result.stderr == b"arguments are not accepted\n", "Container helper accepted an argument")
    bases = re.findall(r"^FROM (docker.io/library/ubuntu@sha256:[0-9a-f]{64})$", recipe, re.M)
    REQUIRE(len(bases) == 1, "Pinned fixture base missing")
    context = work / "cache-proof"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "FROM " + bases[0] + " AS proof\nARG EXPECT_PREVIOUS\n"
        "RUN --mount=type=cache,id=qualification,target=/cache "
        "if [ \"$EXPECT_PREVIOUS\" = 1 ]; then test \"$(cat /cache/marker)\" = verified; fi && "
        "printf verified > /cache/marker && printf 'OCI_BUILDER_OK\\n' > /proof\n"
        "FROM scratch\nCOPY --from=proof /proof /proof\n")
    exports = []
    for previous in (0, 1):
        target = work / ("cache-proof-" + str(previous) + ".oci.tar")
        sup.run(["docker", "buildx", "build", "--builder", name, "--platform", "linux/" + arch,
                 "--progress", "plain", "--no-cache", "--provenance=false",
                 "--build-arg", "EXPECT_PREVIOUS=" + str(previous),
                 "--output", "type=oci,dest=" + str(target), str(context)], seconds=180, inspect=check)
        check()
        exports.append(verify_oci(target, arch, {"proof": hashlib.sha256(b"OCI_BUILDER_OK\n").hexdigest()},
                                  sup.check))
    return {"helper_argument_rejected": True, "multi_stage": True, "native_platform": arch,
            "registry_tls": True, "cache_persisted": True, "oci_exports": exports}


def missing_image(result, tag):
    messages = {"Error response from daemon: No such image: " + tag,
                "Error: No such image: " + tag}
    return result.returncode == 1 and result.stdout.strip() in {"", "[]"} and result.stderr.strip() in messages


def begin_import(sup, tag, archive, record):
    result = subprocess.run(["docker", "image", "inspect", tag], env=sup.env,
                            capture_output=True, text=True, timeout=20)
    REQUIRE(missing_image(result, tag), "Import requires a proven absent image tag")
    record["import_tag_absent_before_load"] = True
    record["import_started"] = True
    sup.run(["docker", "load", "--input", str(archive)], seconds=180)


def remove_import(sup, tag, image, *, tag_absent_before_load=False):
    REQUIRE(tag_absent_before_load is True, "Import cleanup requires prior tag absence proof")
    result = subprocess.run(["docker", "image", "inspect", tag], env=sup.env,
                            capture_output=True, text=True, timeout=20)
    if missing_image(result, tag):
        return {"absent": True}
    REQUIRE(result.returncode == 0, "Cannot determine imported image ownership")
    items = json.loads(result.stdout)
    REQUIRE(len(items) == 1 and items[0]["Id"] == image, "Imported image tag changed")
    subprocess.run(["docker", "image", "rm", tag], env=sup.env, capture_output=True, timeout=30, check=True)
    return {"removed": True, "image": image, "tag": tag}


def prepare_notices(stage, context, metadata):
    archive = stage / metadata["archive"]
    REQUIRE(archive.parent == stage and runner.oci.digest_file(archive) == metadata["archive_sha256"],
            "Original notice archive changed")
    destination = context / "licenses"
    destination.mkdir(mode=0o755, exist_ok=False)
    selected, size = {}, 0
    prefixes = ("output/evidence/buildkit/notices/", "output/evidence/runc/notices/",
                "output/evidence/native-notices/")
    exact = {"output/evidence/buildkit/UPSTREAM-LICENSE", "output/evidence/runc/UPSTREAM-LICENSE"}
    with tarfile.open(archive, "r:gz") as source_archive:
        for entry in source_archive:
            if not (entry.name.startswith(prefixes) or entry.name in exact) or entry.isdir():
                continue
            relative = entry.name.removeprefix("output/evidence/")
            REQUIRE(entry.isfile() and not entry.issparse() and 0 <= entry.size <= 2 * 1024 * 1024
                    and str(PurePosixPath(relative)) == relative and ".." not in PurePosixPath(relative).parts
                    and relative not in selected, "Unsafe or duplicate notice")
            size += entry.size
            REQUIRE(size <= 32 * 1024 * 1024 and len(selected) < 5000, "Notice inventory exceeds bound")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source_archive.extractfile(entry) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing)
            target.chmod(0o644)
            selected[relative] = runner.oci.digest_file(target)
    REQUIRE(all(name.removeprefix("output/evidence/") in selected for name in exact)
            and any(name.startswith("native-notices/") for name in selected), "Missing upstream/native notices")
    shutil.copyfile(ROOT / "LICENSE", destination / "project-LICENSE")
    (destination / "project-LICENSE").chmod(0o644)
    selected["project-LICENSE"] = runner.oci.digest_file(destination / "project-LICENSE")
    return {"usr/share/licenses/oci-tools/" + name: digest for name, digest in selected.items()}


def main():
    policy = json.loads((ROOT / ".github/oci-tools/builder-qualification.json").read_text())
    REQUIRE(policy.get("schema") == 1 and policy.get("reviewed") is True,
            "A complete, independently reviewed image recipe is required")
    arch, commit = admit(os.environ)
    REQUIRE(subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == commit,
            "Checkout differs from reviewed source")
    temporary = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    stage = temporary / "oci-promotion"
    lock = json.loads((ROOT / ".github/oci-tools/promotion.json").read_text())
    promotion.validate_lock(lock)
    index = json.loads((stage / INDEX_FILENAME).read_text())
    REQUIRE(index["source"] == lock["source"] and index["qualification_run"] == lock["qualification_run"]
            and index["published"] is False and index["builder_image_qualified"] is False,
            "Unexpected staged input")
    work = temporary / ("oci-builder-" + arch)
    work.mkdir(mode=0o700, exist_ok=False)
    output = work / "evidence"
    output.mkdir()
    env = runner.clean_env(work / "home")
    plugin = Path(env["DOCKER_CONFIG"]) / "cli-plugins"
    plugin.mkdir(parents=True)
    client = temporary / "victron-go-tools-buildx/output/cli-plugins/docker-buildx"
    helper = temporary / "oci-builder-inputs/oci-cgroup-reader"
    # Separate workflow steps build and scan these exact files before execution.
    tool_proof = json.loads((temporary / "oci-builder-inputs/acceptance.json").read_text())
    REQUIRE(tool_proof["source"] == commit and tool_proof["architecture"] == arch
            and tool_proof["passed"] is True, "Native helper/client acceptance missing")
    REQUIRE(tool_proof["buildx_sha256"] == runner.oci.digest_file(client)
            and tool_proof["reader_sha256"] == runner.oci.digest_file(helper), "Scanned helper/client changed")
    shutil.copyfile(client, plugin / "docker-buildx")
    (plugin / "docker-buildx").chmod(0o755)
    context = stage / arch
    expected = {"usr/bin/" + ("buildkit-runc" if tool == "runc" else tool): digest
                for tool, digest in lock["architectures"][arch]["binaries"].items() if tool != "oci-probe"}
    for tool in ("buildkitd", "buildctl", "runc"):
        REQUIRE(runner.oci.digest_file(context / "bin" / tool)
                == lock["architectures"][arch]["binaries"][tool], "Staged binary changed")
    REQUIRE((context / "Dockerfile").read_bytes() == (ROOT / ".github/oci-tools/builder.Dockerfile").read_bytes(),
            "Staged recipe differs")
    shutil.copyfile(helper, context / "bin/oci-cgroup-reader")
    expected["usr/bin/oci-cgroup-reader"] = tool_proof["reader_sha256"]
    expected.update(prepare_notices(stage, context, index["architectures"][arch]))
    paths = [temporary, ROOT] + [Path(p) for p in ("/var/lib/docker", "/var/lib/containerd") if Path(p).exists()]
    sup = runner.Supervisor(env, paths, output / "commands.log", output / "disk.jsonl")
    name = "oci-proof-" + os.environ["GITHUB_RUN_ID"] + "-" + arch
    tag = "local/oci-proof:" + os.environ["GITHUB_RUN_ID"] + "-" + arch
    record = {"source": commit, "architecture": arch, "qualified": False, "published": False,
              "inputs": lock, "tools": tool_proof, "limits": runner.LIMITS}
    builder_started = False
    volume_absent = False
    image = None
    def observe(value):
        with (output / "cgroup.jsonl").open("a") as stream:
            stream.write(json.dumps(value) + "\n")
    try:
        sup.check(runner.START_FREE, force_record=True)
        record["docker_version"] = sup.run(["docker", "version", "--format", "{{json .}}"], capture=True)
        record["buildx_version"] = sup.run(["docker", "buildx", "version"], capture=True)
        source_epoch = subprocess.check_output(["git", "show", "-s", "--format=%ct", "HEAD"],
                                               cwd=ROOT, text=True).strip()
        record["source_epoch"] = source_epoch
        assembly.assemble(sup, context / "bin", context, work, tag, arch, observe, record,
                          source_epoch=source_epoch)
        record["oci"] = verify_oci(work / "builder.oci.tar", arch, expected, sup.check)
        record["docker_export"] = verify_docker_export(work / DOCKER_ARCHIVE, record["oci"], tag)
        image = record["oci"]["config"]
        manifest = json.loads((ROOT / ".github/oci-tools/sources.json").read_text())
        scanner = source.install_scanner(work, arch, manifest)
        scan_path = output / "image-trivy.json"
        record["scan"] = scan_image(sup, scanner, work, scan_path, image)
        record["scan_sha256"] = runner.oci.digest_file(scan_path)
        begin_import(sup, tag, work / DOCKER_ARCHIVE, record)
        info = inspect_image(sup, tag)
        validate_image(info, arch, image)
        REQUIRE(info["RootFS"]["Layers"] == record["oci"]["diff_ids"], "Loaded rootfs differs from verified OCI")
        record["image"] = info
        REQUIRE(runner.probe_volume(sup, runner.state_volume(name)) is None, "State volume already exists")
        volume_absent = True
        config = work / "buildkitd.toml"
        config.write_text('[worker.oci]\n  max-parallelism = 1\n')
        builder_started = True
        sup.run(["docker", "buildx", "create", "--name", name, "--driver", "docker-container",
                 "--buildkitd-config", str(config), "--driver-opt", "image=" + image,
                 "--driver-opt", "memory=4g", "--driver-opt", "memory-swap=4g",
                 "--driver-opt", "cpu-period=100000", "--driver-opt", "cpu-quota=200000",
                 "--driver-opt", "restart-policy=no", "--driver-opt", "env.RUNNER_ARTIFACT_OWNER=" + name])
        sup.run(["docker", "buildx", "inspect", "--builder", name, "--bootstrap"], seconds=180)
        observed = runner.inspect_builder(sup, name, name, image)
        record["builder_id"] = observed["Id"]
        recipe = (ROOT / "deploy/runner-reserve/Dockerfile").read_text()
        record["cgroup_proof"] = runner.qualify_cgroup(sup, name, name, image, observed, work, recipe,
                                                       observe, architecture=arch,
                                                       cgroup_reader="/usr/bin/oci-cgroup-reader")
        runner.check_cgroup_receipt(record)
        record["runtime"] = exercise_builder(sup, name, image, arch, observed, work, recipe, record)
        record["runtime_passed"] = True
    except BaseException as error:
        record["failure_type"] = type(error).__name__
        raise
    finally:
        try:
            if builder_started:
                record["cleanup"] = runner.remove_builder(sup, name, name, image,
                                                           volume_absent_before=volume_absent)
            if record.get("import_started"):
                record["import_cleanup"] = remove_import(sup, tag, image,
                    tag_absent_before_load=record.get("import_tag_absent_before_load"))
            record["qualified"] = record.get("runtime_passed") is True
        except BaseException as error:
            record["cleanup_failure_type"] = type(error).__name__
            raise
        finally:
            save(output / "acceptance.json", record)


if __name__ == "__main__":
    main()
