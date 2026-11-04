#!/usr/bin/env python3
"""One-time hosted source-only lock preparation; this branch is not for merging."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import build_oci_tools as source
import build_runner_go_tools as client

LEGACY = "golang.org/x/crypto/openpgp"


def admit(env):
    commit = env.get("GITHUB_SHA", "")
    source.require(env.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
        and env.get("GITHUB_REPOSITORY") == "victron-venus/venus-os-ci-toolkit"
        and env.get("GITHUB_REF") == "refs/heads/prep/oci-builder-source-locks"
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("RUNNER_ENVIRONMENT") == "github-hosted"
        and env.get("RUNNER_OS") == "Linux" and env.get("RUNNER_ARCH") == "X64"
        and re.fullmatch(r"[0-9a-f]{40}", commit)
        and env.get("REVIEWED_COMMIT") == commit,
        "Exact reviewed disposable hosted preparation required")
    return commit


def main():
    commit = admit(os.environ)
    source.require(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == commit,
                   "Checkout differs from reviewed source")
    name = os.environ.get("PREPARE_COMPONENT")
    source.require(name in {"buildx", "buildkit"}, "Unexpected preparation component")
    stage = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True) / ("builder-lock-" + name)
    stage.mkdir(mode=0o700, exist_ok=False)
    review = stage / "review"
    review.mkdir()
    manifest = json.loads((client.ROOT / ".github/oci-tools/source-preparation.json").read_text())
    spec = manifest["components"][name]
    source.require(spec["repository"] == {"buildx": "docker/buildx", "buildkit": "moby/buildkit"}[name]
                   and re.fullmatch(r"[0-9a-f]{40}", spec["commit"])
                   and spec["root"] == name + "-" + spec["commit"], "Unexpected upstream source")
    record = {"source": commit, "run": os.environ["GITHUB_RUN_ID"], "attempt": 1,
              "compiler": spec["go_version"], "input": spec, "updates": spec["updates"],
              "prepared": False, "qualified": False, "published": False}
    source.write_json(review / "preparation.json", record)
    runtime = client.go_environment("amd64")
    runtime.update(GOFLAGS="-mod=mod", GOCACHE=str(stage / "go-cache"),
                   GOMODCACHE=str(stage / "modules"), GOMEMLIMIT="2GiB")
    source.require(source.run(["go", "version"], env=runtime).split()[2] == spec["go_version"],
                   "Exact source-preparation compiler required")
    archive = stage / (name + ".tar.gz")
    source.download(f"https://codeload.github.com/{spec['repository']}/tar.gz/{spec['commit']}",
                    archive, spec["archive_sha256"], spec["compressed_bytes"])
    tree = stage / "source"
    client.unpack(archive, tree, spec)
    for file in ("go.mod", "go.sum"):
        source.require(source.digest(tree / file) == spec["original_locks"][file], "Upstream lock drift")
    if name == "buildx":
        client.retain_namesgenerator(tree, spec)
    archive.unlink()
    edits = ["-require=" + module + "@" + version for module, version in spec["updates"].items()]
    if name == "buildx":
        edits.append("-droprequire=github.com/docker/docker")
    commands = [["mod", "edit", *edits], ["mod", "tidy"], ["mod", "verify"], ["mod", "vendor"]]
    for index, args in enumerate(commands):
        source.run(["go", *args], cwd=tree, env=runtime, log=review / f"resolve-{index}.log", timeout=600)
    runtime["GOFLAGS"] = "-mod=readonly"
    modules = source.locked_modules(source.run(["go", "list", "-m", "-json", "all"], cwd=tree, env=runtime))
    source.write_json(review / "modules.json", modules)
    graph = source.run(["go", "mod", "graph"], cwd=tree, env=runtime)
    source.require(len(graph.encode()) <= 2 * 1024 * 1024, "Oversized module graph")
    (review / "module-graph.txt").write_text(graph)
    packages = list(client.json_stream(source.run(["go", "list", "-deps", "-json", *spec["main_packages"]],
                                                 cwd=tree, env=runtime, timeout=600)))
    inventory = [{"path": item["ImportPath"], "imports": item.get("Imports", []),
                  "module": {key: item.get("Module", {}).get(key) for key in ("Path", "Version")}}
                 for item in packages]
    source.write_json(review / "production-packages.json", inventory)
    importers = [item for item in inventory if any(i == LEGACY or i.startswith(LEGACY + "/")
                                                  for i in item["imports"])]
    source.write_json(review / "legacy-openpgp-importers.json", importers)
    record.update(prepared=True, production_packages=len(packages), legacy_openpgp_importers=len(importers),
                  vendor_sha256=source.tree_digest(tree / "vendor"),
                  output_locks={file: source.digest(tree / file) for file in ("go.mod", "go.sum")},
                  modules_sha256=source.digest(review / "modules.json"))
    for name in ("go.mod", "go.sum"):
        shutil.copyfile(tree / name, review / name)
    source.write_json(review / "preparation.json", record)


if __name__ == "__main__":
    main()
