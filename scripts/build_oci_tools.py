#!/usr/bin/env python3
"""Direct source builds of candidate OCI tools; never bootstraps through Docker.

Linux builds run only on disposable GitHub-hosted workers. The separate runtime
qualifier is opt-in and must be reviewed before its first privileged execution.
"""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from build_runner_go_tools import digest, json_stream, require, unpack, verify_openpgp_absent
from oci_vulnerability import evaluate as evaluate_govulncheck
import oci_header_disposition as header_review

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / ".github/oci-tools"
MANIFEST = INPUTS / "sources.json"
TAGS = {
    "buildkit": "osusergo netgo static_build seccomp",
    "runc": "apparmor seccomp netgo cgo static_build osusergo",
}
UNIT_PACKAGES = {
    "buildkit": ["./solver/llbsolver/file", "./frontend/dockerfile/parser",
                 "./util/pgpsign", "./qualification/archive"],
    "runc": ["./libcontainer/specconv", "./libcontainer/configs/...",
             "./libcontainer/capabilities"],
}
OPENPGP = "golang.org/x/crypto/openpgp"
TRANSIENT_HTTP_ERRORS = (500, 502, 503, 504)
NATIVE_PACKAGE_ARCHIVES = {
    "amd64": "https://archive.ubuntu.com/ubuntu/",
    "arm64": "https://ports.ubuntu.com/ubuntu-ports/",
}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def tree_digest(root):
    """Portable digest of vendor paths and bytes, independent of timestamps."""
    value = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "Vendor symlink requires separate review")
        if path.is_file():
            value.update(path.relative_to(root).as_posix().encode() + b"\0")
            value.update(bytes.fromhex(digest(path)))
    return value.hexdigest()


def locked_modules(text):
    result = []
    for module in json_stream(text):
        require("Replace" not in module, "Module replacements require separate review")
        require(not module.get("Error"), "Incomplete module graph")
        # Sum/GoModSum fields depend on which zip files happen to be cached.
        # Their exact values are already bound by the complete go.sum file.
        result.append({key: module[key] for key in ("Path", "Version", "Main") if key in module})
    return sorted(result, key=lambda item: item["Path"])


def hosted_arch(environment=None):
    env = os.environ if environment is None else environment
    require(env.get("GITHUB_ACTIONS") == "true" and env.get("RUNNER_OS") == "Linux"
            and env.get("RUNNER_ENVIRONMENT") == "github-hosted",
            "Candidate execution requires a disposable GitHub-hosted Linux worker")
    arch = {"X64": "amd64", "ARM64": "arm64"}.get(env.get("RUNNER_ARCH"))
    require(arch is not None, "Unsupported native architecture")
    return arch


def stage_dir():
    hosted_arch()
    root = Path(os.environ["RUNNER_TEMP"])
    require(root.is_absolute() and root.is_dir() and not root.is_symlink(),
            "Invalid worker temporary directory")
    stage = root / "oci-tool-qualification"
    require(not stage.is_symlink(), "Stage must not be a symlink")
    stage.mkdir(mode=0o700, exist_ok=True)
    return stage


def environment(component, arch):
    env = dict(os.environ, GOTOOLCHAIN="local", GOWORK="off", GOENV="off",
               GOFLAGS="-mod=readonly", GOPROXY="https://proxy.golang.org",
               GOSUMDB="sum.golang.org", GOMAXPROCS="2", GOOS="linux", GOARCH=arch,
               GOAMD64="v1", GOARM64="v8.0", CGO_ENABLED="1" if component == "runc" else "0",
               CC="gcc-13", CXX="g++-13")
    for key in ("GOPRIVATE", "GONOSUMDB", "GONOPROXY", "GOINSECURE", "CGO_CFLAGS",
                "CGO_CPPFLAGS", "CGO_LDFLAGS", "CGO_CXXFLAGS", "PKG_CONFIG_PATH",
                "PKG_CONFIG_LIBDIR", "GOEXPERIMENT", "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH"):
        env.pop(key, None)
    return env


def run(args, *, cwd=None, env=None, log=None, timeout=1200, allowed=(0,)):
    print("+", " ".join(map(str, args)), flush=True)
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    if log:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        Path(log).write_text(result.stdout)
    require(result.returncode in allowed,
            f"Command failed ({result.returncode}); see {log or result.stdout[-2000:]}")
    return result.stdout


def download(url, target, sha256, size=None, *, opener=None):
    """Retry transient HTTP responses; publish only complete, verified bytes."""
    require(url.startswith("https://"), "HTTPS download required")
    opener = urllib.request.urlopen if opener is None else opener
    for attempt, delay in enumerate((2, 4, 8, 16, None), start=1):
        try:
            # Keep a failed transfer separate from the final name. Linking the
            # verified file preserves exclusive creation even across a race.
            with tempfile.TemporaryDirectory(dir=target.parent, prefix=".oci-download-") as temp:
                pending = Path(temp) / "payload"
                with opener(url, timeout=90) as response, pending.open("xb") as out:
                    total = 0
                    while block := response.read(1024 * 1024):
                        total += len(block)
                        require(total <= (size or 150 * 1024 * 1024), "Download exceeds bound")
                        out.write(block)
                require((size is None or total == size) and digest(pending) == sha256,
                        "Download identity mismatch")
                os.link(pending, target)
            return
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in TRANSIENT_HTTP_ERRORS or delay is None:
                raise
            print(f"Transient HTTP {error.code}; download retry {attempt + 1}/5 in {delay}s",
                  flush=True)
            time.sleep(delay)


class NativeArchiveRedirect(urllib.request.HTTPRedirectHandler):
    """Keep each native package request on its reviewed HTTPS origin."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        original = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        require(target.scheme == "https" and target.netloc == original.netloc,
                "Native package redirect changed its HTTPS origin")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_native_package(spec, item, target, *, archive_selected=False):
    """Return an install-local preference only after verified archive success."""
    arch = spec["architecture"]
    archive = NATIVE_PACKAGE_ARCHIVES.get(arch)
    snapshot = spec["snapshot"]
    expected = f"https://snapshot.ubuntu.com/ubuntu/{snapshot}/"
    require(type(archive_selected) is bool and archive is not None
            and spec["distribution"] == "ubuntu:24.04"
            and isinstance(snapshot, str) and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", snapshot)
            and spec["base_url"] == expected, "Unreviewed native package origin")
    filename = item["filename"]
    require(isinstance(filename, str) and re.fullmatch(r"pool/[A-Za-z0-9_+~./-]+[.]deb", filename)
            and all(part not in ("", ".", "..") for part in filename.split("/"))
            and item["architecture"] in (arch, "all"), "Invalid native package path")
    print(f"Fetching locked native package {target.name}", flush=True)
    open_native = urllib.request.build_opener(NativeArchiveRedirect()).open
    origin = archive if archive_selected else expected
    try:
        download(origin + filename, target, item["sha256"], item["size"], opener=open_native)
        return archive_selected
    except urllib.error.HTTPError as error:
        if archive_selected or error.code not in TRANSIENT_HTTP_ERRORS:
            raise
        print(f"Snapshot retries exhausted; official archive fallback for {target.name}", flush=True)
        download(archive + filename, target, item["sha256"], item["size"], opener=open_native)
        return True


def validate_locks(manifest):
    require(manifest["schema"] == 1 and manifest["go_version"] == "go1.27.4",
            "Unexpected recipe schema/toolchain")
    require(set(manifest["components"]) == {"buildkit", "runc"}, "Unexpected component set")
    require(type(manifest["locks_reviewed"]) is bool, "Explicit lock review status required")
    for arch in ("amd64", "arm64"):
        require(digest(INPUTS / f"native-{arch}.json") == manifest["native_locks"][arch],
                "Native input lock changed")
        header_review.load_policy(INPUTS, manifest["header_disposition_sha256"], arch,
                                  json.loads((INPUTS / f"native-{arch}.json").read_text())["packages"])
    for name, spec in manifest["components"].items():
        require(spec["repository"] == {"buildkit": "moby/buildkit", "runc": "opencontainers/runc"}[name],
                "Unexpected upstream")
        require(len(spec["commit"]) == 40 and all(c in "0123456789abcdef" for c in spec["commit"]),
                "Full source commit required")
        require(spec["root"] == name + "-" + spec["commit"], "Unexpected source root")
        require(spec["downstream_version"] == spec["release_tag"] + "+victron.security.2",
                "Candidate must identify downstream modifications")
        binding = json.loads((INPUTS / name / "inputs.json").read_text())
        require(binding == {"go_version": manifest["go_version"], "source": spec},
                "Locks must bind to the exact source and toolchain")
        require(set(spec["locked_files"]) == {"go.mod", "go.sum", "modules.json"},
                "Lock inventory must include exactly go.mod, go.sum and modules.json")
        for path, expected in spec["locked_files"].items():
            require(path in {"go.mod", "go.sum", "modules.json"}, "Unexpected lock path")
            require(digest(INPUTS / name / path) == expected, "Reviewed lock digest mismatch")
        if manifest["locks_reviewed"]:
            require(isinstance(spec["vendor_sha256"], str) and len(spec["vendor_sha256"]) == 64,
                    "A reviewed vendor digest is required")


def prepare_locks(stage, arch, manifest):
    """Generate reviewable lock artifacts on hosted CI, never auto-approve them."""
    result = copy.deepcopy(manifest)
    result["locks_reviewed"] = False
    target = stage / "prepared-inputs"
    target.mkdir(exist_ok=False)
    for name, spec in result["components"].items():
        env = environment(name, arch)
        require(run(["go", "version"], env=env).split()[2] == manifest["go_version"],
                "Wrong preparation compiler")
        source = stage / "preparation-source" / name
        archive = stage / (name + ".tar.gz")
        download(f"https://codeload.github.com/{spec['repository']}/tar.gz/{spec['commit']}",
                 archive, spec["archive_sha256"], spec["compressed_bytes"])
        unpack(archive, source, spec)
        archive.unlink()
        output = target / name
        output.mkdir()
        for file in ("go.mod", "go.sum"):
            require(digest(source / file) == spec["original_locks"][file], "Upstream lock drift")
            shutil.copyfile(INPUTS / name / file, source / file)
        for args in (["mod", "tidy"], ["mod", "verify"], ["mod", "vendor"]):
            run(["go", *args], cwd=source, env=dict(env, GOFLAGS="-mod=mod"),
                log=output / (args[1] + ".log"))
        modules = locked_modules(run(["go", "list", "-mod=readonly", "-m", "-json", "all"],
                                     cwd=source, env=env))
        write_json(output / "modules.json", modules)
        for file in ("go.mod", "go.sum"):
            shutil.copyfile(source / file, output / file)
        spec["locked_files"] = {file: digest(output / file) for file in ("go.mod", "go.sum", "modules.json")}
        spec["vendor_sha256"] = tree_digest(source / "vendor")
        write_json(output / "inputs.json", {"go_version": manifest["go_version"], "source": spec})
        shutil.rmtree(source)
    write_json(target / "sources.json", result)


def install_native(stage, arch, manifest):
    """Install an explicit closure of hash-pinned Ubuntu packages; no apt fetch."""
    spec = json.loads((INPUTS / f"native-{arch}.json").read_text())
    _, binding, files = header_review.load_policy(INPUTS, manifest["header_disposition_sha256"],
                                                  arch, spec["packages"])
    require(spec["architecture"] == arch, "Wrong native package architecture")
    os_release = Path("/etc/os-release").read_text()
    require('ID=ubuntu' in os_release and 'VERSION_ID="24.04"' in os_release,
            "Native lock requires Ubuntu 24.04")
    debs = stage / "debs"
    debs.mkdir(exist_ok=False)
    packages = []
    archive_selected = False
    for item in spec["packages"]:
        require(item["architecture"] in (arch, "all") and item["filename"].startswith("pool/")
                and ".." not in Path(item["filename"]).parts, "Invalid package input")
        # --no-download only accepts completed files in apt's archive cache.
        # Local .deb arguments alone still enqueue a file acquisition, which
        # that flag disables. apt's cache name preserves and quotes the epoch.
        version = item["version"].replace(":", "%3a")
        target = debs / f"{item['name']}_{version}_{item['architecture']}.deb"
        archive_selected = download_native_package(
            spec, item, target, archive_selected=archive_selected)
        if item["name"] == "linux-libc-dev":
            header_archive = header_review.verify_archive(target, binding, files, INPUTS)
        packages.append(target)
    run(["sudo", "env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=l",
         "apt-get", "-o", f"Dir::Cache::archives={debs}",
         "install", "--yes", "--no-download", "--no-install-recommends",
         "--allow-downgrades", "--no-remove", *packages], log=stage / "native-install.log", timeout=600)
    # Do not scan an invented OS inventory: these exact packages must be installed.
    status = []
    for item in spec["packages"]:
        observed = run(["dpkg-query", "-W", "-f=${Version}\t${Architecture}", item["name"]]).strip()
        require(observed == item["version"] + "\t" + item["architecture"],
                "Installed native package differs from lock: " + item["name"])
        status.append(run(["dpkg-query", "-s", item["name"]]).strip())
    evidence = stage / "output/evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    write_json(evidence / "native-inputs.json", spec)
    run(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${Architecture}\n"],
        log=evidence / "host-package-inventory.txt")
    # Separate scan root includes the actual native dependency closure + distro ID.
    scan = stage / "native-scan"
    (scan / "var/lib/dpkg").mkdir(parents=True)
    (scan / "etc").mkdir()
    (scan / "var/lib/dpkg/status").write_text("\n\n".join(status) + "\n")
    (scan / "etc/os-release").write_text(os_release)
    # Trivy's pinned Ubuntu analyzer identifies the distro from lsb-release.
    # Copy the worker's actual file, rather than fabricating scanner metadata.
    shutil.copyfile("/etc/lsb-release", scan / "etc/lsb-release")
    shutil.copyfile(scan / "etc/lsb-release", evidence / "native-lsb-release.txt")
    cc_files = {}
    for arg in ("libc.a", "libgcc.a", "crtbeginT.o", "crtend.o", "crt1.o", "crti.o", "crtn.o"):
        path = Path(run(["gcc-13", "-print-file-name=" + arg]).strip()).resolve()
        require(path.is_file(), "Missing native linker input: " + arg)
        cc_files[str(path)] = digest(path)
    libdir = Path(run(["pkg-config", "--variable=libdir", "libseccomp"]).strip())
    for path in (libdir / "libseccomp.a", Path(shutil.which("gcc-13")).resolve(),
                 Path(shutil.which("ld")).resolve()):
        cc_files[str(path)] = digest(path)
    write_json(evidence / "native-files.json", cc_files)
    # Hosted Ubuntu images make /usr/share writable, including preinstalled
    # package notices. Restore only the observed exact paths before verification.
    run(["sudo", "--preserve-env=GITHUB_ACTIONS,RUNNER_OS,RUNNER_ENVIRONMENT,RUNNER_ARCH,RUNNER_TEMP",
         "--", sys.executable, Path(__file__).resolve(), "harden"],
        log=evidence / "native-metadata-hardening.log")
    owned = run(["dpkg-query", "-L", "linux-libc-dev"]).splitlines()
    write_json(evidence / "installed-header-metadata.json", header_review.installed_metadata(Path("/"), owned))
    verified = header_review.verify_installed(Path("/"), files, owned, header_archive["data_directories"])
    write_json(evidence / "header-archive-verification.json", header_archive)
    write_json(evidence / "installed-header-verification.json", {
        **verified, "package": binding, "policy_sha256": manifest["header_disposition_sha256"],
        "archive_verified": True, "parser_sha256": digest(Path(header_review.__file__)),
    })
    for item in spec["packages"]:
        notice = Path("/usr/share/doc") / item["name"] / "copyright"
        require(notice.is_file(), "Missing native package copyright: " + item["name"])
        target = evidence / "native-notices" / item["name"]
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(notice, target)
    run(["gcc-13", "--version"], log=evidence / "gcc-version.txt")
    run(["pkg-config", "--modversion", "libseccomp"], log=evidence / "seccomp-version.txt")
    # apt creates root-owned cache metadata (notably partial/). Delete only
    # our downloaded archives here; the disposable worker owns final teardown.
    for package in packages:
        package.unlink(missing_ok=True)


def package_graph(source, env, tags, packages, log, tests=False):
    args = ["go", "list", "-mod=vendor", "-deps", "-json", "-tags", tags]
    if tests:
        args.append("-test")
    text = run([*args, *packages], cwd=source, env=env)
    graph = []
    for package in json_stream(text):
        require(not package.get("Error") and not package.get("DepsErrors"), "Incomplete package graph")
        entry = {k: package[k] for k in ("ImportPath", "Standard", "Imports", "CgoFiles", "CFiles",
                 "CXXFiles", "HFiles", "SFiles", "CgoCFLAGS", "CgoCPPFLAGS", "CgoCXXFLAGS",
                 "CgoLDFLAGS") if k in package}
        if package.get("Module"):
            entry["Module"] = {k: package["Module"][k] for k in ("Path", "Version") if k in package["Module"]}
        graph.append(entry)
    write_json(log, graph)
    return graph


def notices(source, graphs, evidence, env):
    modules = {p["Module"]["Path"] for graph in graphs for p in graph if p.get("Module")}
    inventory = {}
    for index, module in enumerate(sorted(modules)):
        base = source / "vendor" / module
        if not base.is_dir():
            # The only non-vendored module is the main upstream source.
            require(module in ("github.com/moby/buildkit", "github.com/opencontainers/runc"),
                    "Cannot find license source for " + module)
            base = source
        found = []
        while base != source / "vendor" and base.is_relative_to(source):
            found = [p for p in base.iterdir() if p.is_file()
                     and p.name.upper().startswith(("LICENSE", "NOTICE", "COPYING", "COPYRIGHT"))]
            if found or base == source:
                break
            base = base.parent
        require(found, "No license/notice for compiled module: " + module)
        saved = {}
        for path in found:
            require(path.stat().st_size < 2 * 1024 * 1024, "Oversized notice")
            target = evidence / "notices" / str(index) / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            saved[path.name] = digest(target)
        inventory[module] = {"directory": str(index), "files": saved}
    goroot = Path(run(["go", "env", "GOROOT"], env=env).strip())
    shutil.copyfile(goroot / "LICENSE", evidence / "notices/Go-LICENSE")
    write_json(evidence / "notices/modules.json", inventory)


def commands(name, spec, output):
    common = ["go", "build", "-mod=vendor", "-p", "2", "-trimpath", "-buildvcs=false"]
    if name == "runc":
        flags = ("-linkmode external -extldflags -static "
                 f"-X main.gitCommit={spec['commit']} -X main.extraVersion=victron.security.2")
        return {"runc": common + ["-x", "-tags", TAGS[name], "-ldflags", flags,
                                  "-o", str(output / "runc"), "."]}
    prefix = "github.com/moby/buildkit/version."
    flags = (f"-X {prefix}Version={spec['downstream_version']} "
             f"-X {prefix}Revision={spec['commit']}+victron.security.2 "
             f"-X {prefix}Package=github.com/moby/buildkit")
    return {tool: common + ["-tags", TAGS[name], "-ldflags", flags,
                           "-o", str(output / tool), "./cmd/" + tool]
            for tool in ("buildctl", "buildkitd")}


def build_component(name, spec, stage, arch, manifest):
    env = environment(name, arch)
    require(run(["go", "version"], env=env).split()[2] == manifest["go_version"],
            "Unexpected Go toolchain")
    require(os.geteuid() != 0, "Source compilation and unit tests must be unprivileged")
    source = stage / "source" / name
    archive = stage / (name + ".tar.gz")
    download(f"https://codeload.github.com/{spec['repository']}/tar.gz/{spec['commit']}",
             archive, spec["archive_sha256"], spec["compressed_bytes"])
    unpack(archive, source, spec)
    archive.unlink()
    for file in ("go.mod", "go.sum"):
        require(digest(source / file) == spec["original_locks"][file], "Source lock drift")
        shutil.copyfile(INPUTS / name / file, source / file)
    evidence = stage / "output/evidence" / name
    evidence.mkdir(parents=True, exist_ok=False)
    resolved = locked_modules(run(["go", "list", "-mod=readonly", "-m", "-json", "all"], cwd=source, env=env))
    require(resolved == json.loads((INPUTS / name / "modules.json").read_text()), "Resolved module graph drift")
    run(["go", "mod", "verify"], cwd=source, env=env, log=evidence / "module-verification.txt")
    run(["go", "mod", "vendor"], cwd=source, env=env, log=evidence / "vendor.txt")
    require(tree_digest(source / "vendor") == spec["vendor_sha256"], "Vendor bytes differ from reviewed resolution")
    if name == "buildkit":
        run(["go", "mod", "why", "-m", "golang.org/x/crypto/openpgp"], cwd=source,
            env=env, log=evidence / "openpgp-module-why.txt")
        shutil.copytree(ROOT / "tests/oci_fixtures/archive", source / "qualification/archive")
        shutil.copytree(ROOT / "tests/oci_fixtures/probe", source / "qualification/probe")
    tests = UNIT_PACKAGES[name]
    graph = package_graph(source, env, TAGS[name], tests, evidence / "test-packages.json", tests=True)
    run(["go", "test", "-mod=vendor", "-json", "-count=1", "-short", "-p", "2", "-timeout=10m",
         "-tags", TAGS[name], *tests], cwd=source, env=env, log=evidence / "unit-tests.jsonl")
    bins = stage / "output/bin"
    bins.mkdir(parents=True, exist_ok=True)
    builds = commands(name, spec, bins)
    if name == "buildkit":
        builds["oci-probe"] = ["go", "build", "-mod=vendor", "-trimpath", "-buildvcs=false",
                               "-o", str(bins / "oci-probe"), "./qualification/probe"]
    graphs = {}
    for tool, command in builds.items():
        graphs[tool] = package_graph(source, env, TAGS[name], [command[-1]], evidence / (tool + "-packages.json"))
        verify_openpgp_absent({package["ImportPath"] for package in graphs[tool]})
        run(command, cwd=source, env=env, log=evidence / (tool + "-build.log"))
        run(["go", "version", "-m", bins / tool], env=env, log=evidence / (tool + "-buildinfo.txt"))
        dynamic = run(["readelf", "-d", bins / tool], log=evidence / (tool + "-dynamic.txt"))
        require("(NEEDED)" not in dynamic, "Candidate unexpectedly links a dynamic library")
        run(["readelf", "-h", bins / tool], log=evidence / (tool + "-elf.txt"))
    for file in ("go.mod", "go.sum"):
        require(digest(source / file) == spec["locked_files"][file], "Build mutated reviewed locks")
    notices(source, [graph, *graphs.values()], evidence, env)
    shutil.copyfile(source / "LICENSE", evidence / "UPSTREAM-LICENSE")
    write_json(evidence / "provenance.json", {
        "source": spec, "architecture": arch, "go_version": manifest["go_version"],
        "commands": builds, "tests": tests,
        "binaries": {tool: digest(bins / tool) for tool in builds},
        "openpgp": {tool: [p["ImportPath"] for p in packages if p["ImportPath"].startswith(OPENPGP)]
                    for tool, packages in graphs.items()},
        "openpgp_test_packages": [p["ImportPath"] for p in graph if p["ImportPath"].startswith(OPENPGP)],
        "qualification": "candidate source build; security and runtime gates are separate",
    })


def install_scanner(stage, arch, manifest):
    spec = manifest["trivy"][arch]
    archive = stage / "trivy.tar.gz"
    download(spec["url"], archive, spec["sha256"])
    target = stage / "trivy"
    with tarfile.open(archive, "r:gz") as tf:
        member = tf.getmember("trivy")
        require(member.isreg() and member.size < 200 * 1024 * 1024, "Invalid scanner executable")
        with tf.extractfile(member) as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst)
    target.chmod(0o755)
    archive.unlink()
    return target


def trivy_failures(data, label, expected_packages=None):
    results = data.get("Results", [])
    if label == "binaries":
        covered = {Path(r["Target"]).name for r in results
                   if r.get("Type") == "gobinary" and r.get("Packages")}
        require(covered == {"buildctl", "buildkitd", "runc", "oci-probe"},
                "Trivy did not inventory every exact candidate binary")
    else:
        require(data.get("Metadata", {}).get("OS") == {"Family": "ubuntu", "Name": "24.04"}
                and any(r.get("Class") == "os-pkgs" and r.get("Packages") for r in results),
                "Trivy did not inventory native Ubuntu inputs")
        require(expected_packages, "Native scan requires the locked package inventory")
        packages = [p for r in results if r.get("Class") == "os-pkgs" for p in r.get("Packages", [])]
        observed = []
        for package in packages:
            version = ((str(package["Epoch"]) + ":" if package.get("Epoch") else "")
                       + package["Version"]
                       + ("-" + package["Release"] if package.get("Release") else ""))
            require(package.get("ID") == package["Name"] + "@" + version,
                    "Inconsistent native package identity")
            observed.append((package["Name"], version, package.get("Arch")))
        expected = [(p["name"], p["version"], p["architecture"]) for p in expected_packages]
        require(sorted(observed) == sorted(expected), "Native scan inventory differs from locked packages")
    return [f"{label}: {v['VulnerabilityID']} {v.get('PkgName')} {v.get('Severity')}"
            for result in results for v in result.get("Vulnerabilities", [])
            if v.get("Severity") in ("HIGH", "CRITICAL")]


def scan(stage, arch, manifest):
    scanner = install_scanner(stage, arch, manifest)
    evidence = stage / "output/evidence/security"
    evidence.mkdir(parents=True, exist_ok=False)
    failures = []
    not_affected = []
    for name in manifest["components"]:
        source = stage / "source" / name
        env = environment(name, arch)
        builds = commands(name, manifest["components"][name], stage / "output/bin")
        if name == "buildkit":
            builds["oci-probe"] = ["./qualification/probe"]
        for tool, command in builds.items():
            module = "github.com/opencontainers/runc" if name == "runc" else "github.com/moby/buildkit"
            source_root = module if name == "runc" else module + "/" + command[-1].removeprefix("./")
            for kind, args in (("source", ["-tags", TAGS[name], command[-1]]),
                               ("binary", ["-mode=binary", str(stage / "output/bin" / tool)])):
                try:
                    report = run(["govulncheck", "-json", *args], cwd=source,
                                 env=dict(env, GOFLAGS="-mod=vendor"),
                                 log=evidence / f"{tool}-{kind}.jsonl")
                    assessed = evaluate_govulncheck(report, kind, completed=True,
                                                   expected_root=module if kind == "binary" else source_root,
                                                   expected_go=manifest["go_version"])
                    write_json(evidence / f"{tool}-{kind}-assessment.json", assessed)
                    require(assessed["passed"], f"{tool} {kind}: vulnerability evidence requires action/review")
                except ValueError as error:
                    failures.append(str(error))
    cache = stage / "trivy-cache"
    for label, target in (("binaries", stage / "output/bin"), ("native", stage / "native-scan")):
        report = evidence / (label + "-trivy.json")
        run([scanner, "rootfs", "--cache-dir", cache, "--scanners", "vuln", "--list-all-pkgs",
             "--format", "json", "--output", report, target], log=evidence / (label + "-trivy.log"))
        data = json.loads(report.read_text())
        try:
            expected = (json.loads((INPUTS / f"native-{arch}.json").read_text())["packages"]
                        if label == "native" else None)
            raw_failures = trivy_failures(data, label, expected)
            if label == "native":
                policy, binding, _ = header_review.load_policy(
                    INPUTS, manifest["header_disposition_sha256"], arch, expected)
                receipt = json.loads((stage / "output/evidence/installed-header-verification.json").read_text())
                not_affected = header_review.classify(data, policy, binding, receipt,
                                                      manifest["header_disposition_sha256"])
                write_json(evidence / "header-applicability.json", {
                    "reviewed": policy["reviewed"], "raw_blocking_count": len(raw_failures),
                    "not_affected": not_affected, "policy_sha256": manifest["header_disposition_sha256"],
                })
                raw_failures = trivy_failures(header_review.remaining_findings(data, not_affected),
                                             label, expected)
            failures += raw_failures
        except ValueError as error:
            # Keep scanner identities and an explicit failed gate even when the
            # scanner output lacks a usable inventory.
            failures.append(str(error))
        run([scanner, "rootfs", "--cache-dir", cache, "--scanners", "vuln", "--format", "cyclonedx",
             "--output", evidence / (label + "-sbom.cdx.json"), target],
            log=evidence / (label + "-sbom.log"))
    run([scanner, "--version"], log=evidence / "trivy-version.txt")
    if (cache / "db/metadata.json").exists():
        shutil.copyfile(cache / "db/metadata.json", evidence / "trivy-db-metadata.json")
    write_json(evidence / "scanner-identities.json", {
        "trivy": digest(scanner), "govulncheck": digest(Path(shutil.which("govulncheck"))),
        "go": run(["go", "version"]).strip(),
    })
    write_json(evidence / "gate.json", {"passed": not failures, "failures": failures,
                                       "suppressed_findings": [], "reviewed_not_affected": not_affected})
    require(not failures, "Security gate failed; full reports retained, no automatic exemptions")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "prepare", "native", "harden", "build", "scan", "bundle"))
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())
    validate_locks(manifest)
    if args.action == "validate":
        return
    arch = hosted_arch()
    require(platform.machine() in {"amd64": ("x86_64",), "arm64": ("aarch64",)}[arch],
            "Cross architecture execution is prohibited")
    stage = stage_dir()
    if args.action == "prepare":
        prepare_locks(stage, arch, manifest)
        return
    require(manifest["locks_reviewed"], "Lock preparation/review must finish before qualification")
    if args.action == "native":
        install_native(stage, arch, manifest)
    elif args.action == "harden":
        require(os.geteuid() == 0, "Hosted metadata hardening requires root")
        packages = json.loads((INPUTS / f"native-{arch}.json").read_text())["packages"]
        _, binding, files = header_review.load_policy(INPUTS, manifest["header_disposition_sha256"], arch, packages)
        archive = json.loads((INPUTS / f"linux-libc-dev-archive-{arch}.json").read_text())
        actions = []
        try:
            header_review.harden_hosted_metadata(Path("/"), files, archive["data_directories"], actions)
        finally:
            write_json(stage / "output/evidence/native-metadata-hardening.json", {
                "package": binding, "policy_sha256": manifest["header_disposition_sha256"], "actions": actions,
            })
    elif args.action == "build":
        require((stage / "output/evidence/native-files.json").is_file(), "Native inputs not qualified")
        for name, spec in manifest["components"].items():
            build_component(name, spec, stage, arch, manifest)
    elif args.action == "scan":
        scan(stage, arch, manifest)
    else:
        output = stage / "output"
        require(json.loads((output / "evidence/security/gate.json").read_text())["passed"] is True,
                "Cannot bundle a failed security candidate")
        write_json(output / "candidate.json", {
            "commit": run(["git", "rev-parse", "HEAD"]).strip(), "architecture": arch,
            "sources_sha256": digest(MANIFEST),
            "binaries": {p.name: digest(p) for p in sorted((output / "bin").iterdir())},
            "release": False,
        })
        with tarfile.open(stage / "candidate.tar.gz", "w:gz") as archive:
            archive.add(output, arcname="output")


if __name__ == "__main__":
    main()
