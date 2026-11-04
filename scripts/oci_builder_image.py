"""Shared verification and owned import of already-built native OCI images."""

import json
from pathlib import PurePosixPath
import subprocess
import tarfile

if __package__:
    from . import runner_image_oci as oci
else:
    import runner_image_oci as oci

REQUIRE = oci.require
MAX_ROOTFS = 512 * 1024 * 1024
INDEX_FILENAME = "index.json"
ENTRYPOINT = ["/usr/bin/buildkitd", "--oci-worker=true", "--containerd-worker=false",
              "--oci-worker-snapshotter=native", "--oci-worker-net=host",
              "--oci-worker-binary=/usr/bin/buildkit-runc"]


def validate_image(item, arch, expected_id):
    REQUIRE(item["Id"] == expected_id and item["Architecture"] == arch and item["Os"] == "linux",
            "Builder image identity or native platform differs")
    config = item["Config"]
    REQUIRE(config["Entrypoint"] == ENTRYPOINT and config["User"] == "0:0",
            "Builder execution configuration differs")
    REQUIRE("PATH=/usr/bin" in config["Env"] and not config.get("Cmd"), "Unexpected builder command")
    REQUIRE(set(config.get("Volumes", {})) == {"/var/lib/buildkit"}, "Unexpected image volumes")


def verify_oci(path, arch, expected_files, check):
    """Bind every blob and final selected file without extracting the filesystem."""
    REQUIRE(0 < path.stat().st_size <= MAX_ROOTFS, "Oversized qualification OCI archive")
    archive_sha = oci.digest_file(path)
    archive = oci.Archive(path, archive_sha, path.stat().st_size)
    try:
        REQUIRE(oci.json_document(archive.read("oci-layout")) == {"imageLayoutVersion": "1.0.0"},
                "Unexpected OCI layout")
        index = oci.json_document(archive.read(INDEX_FILENAME))
        REQUIRE(index.get("schemaVersion") == 2 and len(index["manifests"]) == 1, "Ambiguous OCI root")
        descriptor = index["manifests"][0]
        if descriptor.get("mediaType") == oci.INDEX:
            index = archive.json_descriptor(descriptor, oci.INDEX)
            REQUIRE(index.get("schemaVersion") == 2 and len(index["manifests"]) == 1,
                    "Exactly one native image required")
            descriptor = index["manifests"][0]
        manifest = archive.json_descriptor(descriptor, oci.MANIFEST)
        REQUIRE(manifest.get("schemaVersion") == 2, "Invalid OCI manifest")
        config = archive.json_descriptor(manifest["config"], oci.CONFIG)
        REQUIRE(config.get("os") == "linux" and config.get("architecture") == arch,
                "OCI image native platform differs")
        REQUIRE(config["rootfs"]["type"] == "layers"
                and len(config["rootfs"]["diff_ids"]) == len(manifest["layers"]), "OCI layer inventory differs")
        tree, expanded = {}, 0
        for layer, expected_diff in zip(manifest["layers"], config["rootfs"]["diff_ids"], strict=True):
            name = archive.descriptor(layer)
            with archive.tar.extractfile(archive.files[name]) as raw:
                entries, size, diff = oci.read_effective_layer(raw, layer, MAX_ROOTFS - expanded, check)
            REQUIRE(diff == expected_diff, "OCI uncompressed layer digest differs")
            expanded += size
            tree = oci.merge_effective_layer(tree, entries)
        files = {name: oci.effective_regular(tree, name)["sha256"] for name in expected_files}
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
            return oci.json_document(archive.extractfile(files[name]).read())
        manifest = read_json("manifest.json")
        REQUIRE(len(manifest) == 1 and manifest[0]["RepoTags"] == [tag], "Docker export identity differs")
        config_path = manifest[0]["Config"]
        config = read_json(config_path)
        with archive.extractfile(files[config_path]) as stream:
            REQUIRE("sha256:" + oci.digest_stream(stream) == oci_proof["config"],
                    "Docker and OCI configuration bytes differ")
        REQUIRE(config["rootfs"]["diff_ids"] == oci_proof["diff_ids"], "Docker and OCI rootfs differ")
        layers = manifest[0]["Layers"]
        REQUIRE(len(layers) == len(oci_proof["diff_ids"]), "Docker export layer count differs")
        for index, (name, expected) in enumerate(zip(layers, oci_proof["diff_ids"], strict=True)):
            REQUIRE(name in files and files[name].isfile(), "Missing Docker layer")
            with archive.extractfile(files[name]) as stream:
                actual = "sha256:" + oci.digest_stream(stream)
            if actual != expected:
                # Modern Docker layouts reference the original content blobs,
                # including compressed layers. The OCI verifier already bound
                # each blob's digest/size to the uncompressed diff ID in order.
                verified = oci_proof.get("layers", [])
                REQUIRE(len(verified) == len(layers), "Verified OCI layer inventory differs")
                descriptor = verified[index]
                REQUIRE(descriptor.get("mediaType") in {oci.LAYER, oci.LAYER + "+gzip"}
                        and actual == descriptor.get("digest")
                        and type(descriptor.get("size")) is int and files[name].size == descriptor["size"],
                        "Docker layer bytes differ")
    return {"archive_sha256": oci.digest_file(path), "archive_bytes": path.stat().st_size,
            "config": oci_proof["config"], "diff_ids": oci_proof["diff_ids"], "same_solve": True}


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

