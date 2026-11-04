#!/usr/bin/env python3
"""Build and assess the native helper/client for an unpublished builder test."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

import build_oci_tools as source
import build_runner_go_tools as client
from oci_vulnerability import evaluate

ROOT = Path(__file__).resolve().parents[1]
require = source.require


def helper(arch, stage):
    require(subprocess.check_output(["go", "version"], text=True).split()[2] == "go1.27.3",
            "Exact helper compiler required")
    stage.mkdir(mode=0o700, exist_ok=False)
    directory = ROOT / "tools/oci-cgroup-reader"
    env = source.environment("buildkit", arch)
    source.run(["go", "test", "-count=1", "-v", "./..."], cwd=directory, env=env,
               log=stage / "helper-tests.log")
    source.run(["go", "build", "-trimpath", "-p", "2", "-o", stage / "oci-cgroup-reader", "."],
               cwd=directory, env=env, log=stage / "helper-build.log")
    proof = {"architecture": arch, "compiler": "go1.27.3", "unit_tests_passed": True,
             "sha256": source.digest(stage / "oci-cgroup-reader"), "sources": {
                 path.name: source.digest(path) for path in directory.iterdir() if path.is_file()}}
    source.write_json(stage / "helper.json", proof)


def scan(arch, stage):
    temporary = Path(os.environ["RUNNER_TEMP"])
    buildx = temporary / "victron-go-tools-buildx/output/cli-plugins/docker-buildx"
    helper_binary = stage / "oci-cgroup-reader"
    helper_proof = json.loads((stage / "helper.json").read_text())
    require(helper_proof["architecture"] == arch and helper_proof["unit_tests_passed"] is True
            and helper_proof["sha256"] == source.digest(helper_binary), "Helper proof differs")
    manifest = json.loads(client.MANIFEST.read_text())
    client_proof = json.loads((buildx.parent.parent / "notices/buildx/provenance.json").read_text())
    require(client_proof["source"] == manifest["components"]["buildx"]
            and client_proof["target_architecture"] == arch
            and client_proof["go_version"].split()[2] == manifest["go_version"]
            and client_proof["binaries"] == {"cli-plugins/docker-buildx": source.digest(buildx)},
            "Fresh Buildx proof differs")
    binaries = stage / "scan-binaries"
    binaries.mkdir()
    shutil.copyfile(buildx, binaries / "docker-buildx")
    shutil.copyfile(helper_binary, binaries / "oci-cgroup-reader")
    assessments = {}
    for name, root, go_version in (("docker-buildx", "github.com/docker/buildx", manifest["go_version"]),
                                   ("oci-cgroup-reader", "github.com/victron-venus/venus-os-ci-toolkit/oci-cgroup-reader",
                                    "go1.27.3")):
        raw = source.run(["govulncheck", "-json", "-mode=binary", binaries / name],
                         log=stage / (name + "-govulncheck.jsonl"))
        assessed = evaluate(raw, "binary", completed=True, expected_root=root, expected_go=go_version)
        source.write_json(stage / (name + "-assessment.json"), assessed)
        require(assessed["passed"], "Builder client/helper vulnerability scan failed")
        assessments[name] = assessed
    scanner = source.install_scanner(stage, arch, json.loads(source.MANIFEST.read_text()))
    report = stage / "tools-trivy.json"
    source.run([scanner, "rootfs", "--cache-dir", stage / "trivy-cache", "--scanners", "vuln",
                "--list-all-pkgs", "--format", "json", "--output", report,
                "--ignorefile", "/dev/null", binaries], log=stage / "tools-trivy.log")
    data = json.loads(report.read_text())
    covered = {Path(row["Target"]).name for row in data.get("Results", [])
               if row.get("Type") == "gobinary" and row.get("Packages")}
    require(covered == {"docker-buildx", "oci-cgroup-reader"}, "Incomplete builder tool scan")
    blockers = [v for row in data["Results"] for v in row.get("Vulnerabilities", [])
                if v.get("Severity") in {"HIGH", "CRITICAL"}]
    require(not blockers, "Builder client/helper has blocking vulnerabilities")
    observed = subprocess.run([str(helper_binary)], capture_output=True, timeout=10, check=False)
    require(observed.returncode == 0 and observed.stdout == Path("/proc/1/cgroup").read_bytes()
            and observed.stderr == b"", "Actual helper did not read the fixed process path")
    rejected = subprocess.run([str(helper_binary), "/proc/self/cgroup"],
                              capture_output=True, timeout=10, check=False)
    require(rejected.returncode == 2 and rejected.stdout == b"", "Actual helper accepted an argument")
    helper_proof.update(constant_path_matches=True, alternate_path_argument_rejected=True)
    source.write_json(stage / "acceptance.json", {
        "source": os.environ["GITHUB_SHA"], "architecture": arch, "passed": True,
        "buildx_sha256": source.digest(buildx), "reader_sha256": source.digest(helper_binary),
        "client_compiler": manifest["go_version"], "helper_compiler": "go1.27.3",
        "client_source": manifest["components"]["buildx"]["commit"],
        "client_manifest_sha256": source.digest(client.MANIFEST),
        "scanner_sha256": source.digest(scanner), "helper_proof": helper_proof,
        "trivy_sha256": source.digest(report), "govulncheck": assessments})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("helper", "scan"))
    args = parser.parse_args()
    arch = source.hosted_arch()
    stage = Path(os.environ["RUNNER_TEMP"]) / "oci-builder-inputs"
    (helper if args.mode == "helper" else scan)(arch, stage)


if __name__ == "__main__":
    main()
