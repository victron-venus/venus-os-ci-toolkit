#!/usr/bin/env python3
"""Build source-pinned Docker tools with reviewed Go dependency locks."""

import argparse
import hashlib
import json
import os
import posixpath
import re
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / ".github/containers/runner-go-tools.json"
LOCKS = ROOT / ".github/containers/runner-go-tools"
REPOSITORIES = {
    "containerd": "containerd/containerd",
    "buildx": "docker/buildx",
    "compose": "docker/compose",
}
BUILD_ROOT = Path("/opt/victron-go-tools-build")
OUTPUT = Path("/opt/victron-go-tools")
MAX_ARCHIVE = 100 * 1024 * 1024
MAX_SOURCE = 700 * 1024 * 1024


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_spec(name, spec):
    require(name in REPOSITORIES and spec["repository"] == REPOSITORIES[name],
            "Unexpected upstream repository")
    require(len(spec["commit"]) == 40 and all(c in "0123456789abcdef" for c in spec["commit"]),
            "A full upstream commit is required")
    require(spec["root"] == spec["repository"].split("/")[1] + "-" + spec["commit"],
            "Unexpected source root")
    require(spec["downstream_version"] == spec["release_tag"] + "+victron.security.1",
            "Downstream builds must be identified")


def archive_members(archive, spec):
    members, total = [], 0
    for member in archive:
        total += member.size
        require(len(members) < 60000 and total <= MAX_SOURCE, "Source archive exceeds limits")
        members.append(member)
    seen, found_links = set(), {}
    prefix = spec["root"]
    for member in members:
        path = PurePosixPath(member.name.rstrip("/"))
        require(bool(path.parts) and not path.is_absolute() and str(path) == member.name.rstrip("/")
                and ".." not in path.parts and path.parts[0] == prefix,
                "Unsafe source archive path")
        relative = str(PurePosixPath(*path.parts[1:]))
        require(relative not in seen, "Duplicate source archive path")
        seen.add(relative)
        require(member.isdir() or member.isreg() or member.issym(), "Unsupported source entry")
        require(not member.issparse() and member.size <= 32 * 1024 * 1024,
                "Unsupported source member size or sparse file")
        if member.issym():
            require(spec["allowed_symlinks"].get(relative) == member.linkname,
                    "Unexpected upstream symlink")
            resolved = posixpath.normpath(posixpath.join(str(PurePosixPath(relative).parent), member.linkname))
            require(not member.linkname.startswith("/") and resolved != ".."
                    and not resolved.startswith("../"), "Upstream symlink leaves source")
            found_links[relative] = member.linkname
    require(found_links == spec["allowed_symlinks"], "Upstream symlink inventory changed")
    for path in seen:
        require(not any(path.startswith(link + "/") for link in found_links),
                "Archive writes through a symlink")
    return members


def unpack(archive_path, destination, spec):
    require(digest(archive_path) == spec["archive_sha256"], "Source archive digest mismatch")
    require(not destination.exists(), "Source destination already exists")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive_members(archive, spec)
        destination.mkdir(parents=True, mode=0o700)
        links = []
        for member in members:
            parts = PurePosixPath(member.name.rstrip("/")).parts[1:]
            if not parts:
                continue
            output = destination.joinpath(*parts)
            if member.isdir():
                output.mkdir(parents=True, exist_ok=True)
            elif member.issym():
                links.append((output, member.linkname))
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, output.open("xb") as target:
                    shutil.copyfileobj(source, target)
                output.chmod(0o755 if member.mode & 0o111 else 0o644)
        for output, target in links:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.symlink_to(target)
        for output, _ in links:
            require(output.resolve().is_relative_to(destination.resolve()),
                    "Upstream symlink escapes source")


def install_locks(source, locks, spec):
    binding = json.loads((locks / "inputs.json").read_text())
    require(binding["source"] == {key: spec[key] for key in
            ("repository", "release_tag", "commit", "archive_sha256")},
            "Lock source binding changed; revalidate locks for the new source")
    require(binding["go_version"] == "go1.26.10"
            and binding["original_locks"] == spec["original_locks"]
            and binding["fixed_locks"] == spec["fixed_locks"]
            and binding.get("retained_namesgenerator") == spec.get("retained_namesgenerator")
            and binding.get("runtime_test_fix") == spec.get("runtime_test_fix"),
            "Lock preparation binding changed")
    for name in ("go.mod", "go.sum"):
        require(digest(source / name) == spec["original_locks"][name],
                "Upstream lock identity changed")
        require(digest(locks / name) == spec["fixed_locks"][name],
                "Reviewed lock identity changed")
    for name in ("go.mod", "go.sum"):
        shutil.copyfile(locks / name, source / name)


def fix_runtime_test(source, spec):
    """Keep the upstream preservation check valid under explicit CPU bounds."""
    fix = spec.get("runtime_test_fix")
    if fix is None:
        return
    require(spec["repository"] == "containerd/containerd", "Unexpected runtime test owner")
    path = source / "pkg/shim/shim_test.go"
    require(path.is_file() and not path.is_symlink() and digest(path) == fix["before_sha256"],
            "Runtime test source identity changed")
    original = path.read_bytes()
    before = b'func TestRuntimeWithNonEmptyMaxEnvProcs(t *testing.T) {\n'
    after = before + b'\texpected := runtime.GOMAXPROCS(0)\n'
    require(original.count(before) == 1, "Expected one runtime preservation test")
    changed = original.replace(before, after).replace(
        b'if oldGoMaxProcs2 != runtime.NumCPU() {', b'if oldGoMaxProcs2 != expected {').replace(
        b't.Fatal("the max number CPU should be equal to available CPUs")',
        b't.Fatal("setRuntime should preserve configured GOMAXPROCS")')
    require(hashlib.sha256(changed).hexdigest() == fix["after_sha256"],
            "Runtime test result identity changed")
    path.write_bytes(changed)


def verify_runtime_test(source, spec):
    fix = spec.get("runtime_test_fix")
    if fix is not None:
        require(digest(source / "pkg/shim/shim_test.go") == fix["after_sha256"],
                "Runtime test changed during build")


def retain_namesgenerator(source, spec):
    """Retain the exact frozen utility without importing the legacy daemon module."""
    retained = spec.get("retained_namesgenerator")
    if retained is None:
        return
    require(spec["repository"] == "docker/buildx", "Unexpected retained utility owner")
    require(set(retained["files"]) == {"names-generator.go", "LICENSE", "NOTICE"},
            "Retained utility inventory changed")
    vendor = source / "vendor/github.com/docker/docker"
    target = source / "internal/namesgenerator"
    require(not target.exists() and not target.is_symlink(), "Retained utility already exists")
    require(not target.parent.is_symlink(), "Retained utility parent is a symlink")
    payloads = {}
    for name, expected in retained["files"].items():
        path = vendor / ("pkg/namesgenerator/names-generator.go" if name.endswith(".go") else name)
        require(path.is_file() and not path.is_symlink() and digest(path) == expected,
                "Retained utility source identity changed")
        payloads[name] = path.read_bytes()
    importer = source / "store/util.go"
    require(importer.is_file() and not importer.is_symlink()
            and digest(importer) == retained["import_before_sha256"],
            "Retained utility import identity changed")
    before = b'"github.com/docker/docker/pkg/namesgenerator"'
    after = b'"github.com/docker/buildx/internal/namesgenerator"'
    original = importer.read_bytes()
    require(original.count(before) == 1, "Expected exactly one utility import")
    changed = original.replace(before, after)
    require(hashlib.sha256(changed).hexdigest() == retained["import_after_sha256"],
            "Retained utility import result changed")
    target.mkdir(parents=True)
    for name, payload in payloads.items():
        (target / name).write_bytes(payload)
    importer.write_bytes(changed)


def check_retained_graph(packages):
    require("github.com/docker/buildx/internal/namesgenerator" in packages
            and not any(p == "github.com/docker/docker" or
                        p.startswith("github.com/docker/docker/") for p in packages),
            "Legacy Docker module remains in the dependency graph")


def retained_utility_notices(source, destination, spec, packages):
    retained = spec.get("retained_namesgenerator")
    if retained is None:
        return
    check_retained_graph(packages)
    require(digest(source / "store/util.go") == retained["import_after_sha256"],
            "Retained utility import changed during build")
    directory = destination / "retained/namesgenerator"
    for name, expected in retained["files"].items():
        path = source / "internal/namesgenerator" / name
        require(path.is_file() and not path.is_symlink() and digest(path) == expected,
                "Retained utility content changed")
    directory.mkdir(parents=True)
    for name in ("LICENSE", "NOTICE"):
        path = source / "internal/namesgenerator" / name
        shutil.copyfile(path, directory / name)


def go_environment(architecture=None):
    env = dict(os.environ, GOTOOLCHAIN="local", GOWORK="off", GOFLAGS="-mod=readonly",
               GOPROXY="https://proxy.golang.org", GOSUMDB="sum.golang.org",
               GOENV="off", GOMAXPROCS="2", CGO_ENABLED="0", GOOS="linux",
               GOAMD64="v1", GOARM64="v8.0")
    for name in ("GOARCH", "GOPRIVATE", "GONOPROXY", "GONOSUMDB", "GOINSECURE",
                 "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(name, None)
    if architecture:
        require(architecture in ("amd64", "arm64"), "Unsupported target architecture")
        env["GOARCH"] = architecture
    return env


def run(arguments, source, *, architecture=None, capture=False):
    return subprocess.run(arguments, cwd=source, env=go_environment(architecture), check=True,
                          text=True, stdout=subprocess.PIPE if capture else None, timeout=1800)


def build_commands(name, spec, output):
    version, revision = spec["downstream_version"], spec["commit"] + "+victron.security.1"
    common = ["go", "build", "-p", "2", "-trimpath"]
    if name == "containerd":
        package = "github.com/containerd/containerd/v2"
        flags = f"-X {package}/version.Version={version} -X {package}/version.Revision={revision} "
        flags += f"-X {package}/version.Package={package} -s -w -extldflags -static"
        tags = "urfave_cli_no_docs,osusergo,netgo,static_build"
        return [common + ["-tags", tags + (",no_grpc" if tool.endswith("-v2") else ""),
                          "-ldflags", flags, "-o", str(output / "bin" / tool), "./cmd/" + tool]
                for tool in ("containerd", "ctr", "containerd-shim-runc-v2")]
    if name == "buildx":
        package = "github.com/docker/buildx"
        flags = f"-s -w -X {package}/version.Version={version} "
        flags += f"-X {package}/version.Revision={revision} -X {package}/version.Package={package}"
        return [common + ["-ldflags", flags, "-o", str(output / "cli-plugins/docker-buildx"), "./cmd/buildx"]]
    require(name == "compose", "Unknown component")
    return [common + ["-tags", "e2e", "-ldflags", f"-w -X github.com/docker/compose/v5/internal.Version={version}",
                      "-o", str(output / "cli-plugins/docker-compose"), "./cmd"]]


def test_packages(name, packages):
    # Match the upstream unit-test scope; daemon/root integration is a later gate.
    if name == "containerd":
        return [p for p in packages if "/integration" not in p]
    if name == "compose":
        return [p for p in packages if "/e2e" not in p]
    return packages


def json_stream(text):
    decoder = json.JSONDecoder()
    while text.strip():
        text = text.lstrip()
        value, offset = decoder.raw_decode(text)
        yield value
        text = text[offset:]


def preserve_notices(source, destination, name, spec, commands, architecture, test_command, output,
                     unit_dependencies):
    destination.mkdir(parents=True, exist_ok=False)
    modules, packages = {}, set()
    for command in commands:
        arguments = ["go", "list", "-deps", "-json"]
        if "-tags" in command:
            arguments += ["-tags", command[command.index("-tags") + 1]]
        arguments.append(command[-1])
        for package in json_stream(run(arguments, source, architecture=architecture, capture=True).stdout):
            packages.add(package["ImportPath"])
            module = package.get("Module")
            if module:
                modules[module["Path"]] = module
    retained_utility_notices(source, destination, spec, packages)
    inventory = []
    for index, module in enumerate(sorted(modules.values(), key=lambda m: m["Path"])):
        actual = module.get("Replace", module)
        require(actual.get("Dir"), "Compiled module directory is unavailable")
        directory = Path(actual["Dir"])
        files = [p for p in directory.iterdir()
                 if p.is_file() and not p.is_symlink()
                 and p.name.upper().startswith(("LICENSE", "NOTICE", "COPYING", "COPYRIGHT"))]
        require(files, "Dependency has no discoverable root license/notice: " + module["Path"])
        copied = {}
        for path in files:
            require(path.stat().st_size <= 2 * 1024 * 1024, "Oversized license file")
            target = destination / str(index) / path.name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, target)
            copied[path.name] = digest(target)
        inventory.append({"module": module["Path"], "version": module.get("Version"),
                          "directory": str(index), "files": copied})
    goroot = Path(run(["go", "env", "GOROOT"], source, capture=True).stdout.strip())
    shutil.copyfile(goroot / "LICENSE", destination / "Go-LICENSE")
    (destination / "provenance.json").write_text(json.dumps({
        "component": name, "source": spec, "modules": inventory,
        "packages": sorted(packages), "test_command": test_command,
        "verified_unit_dependency_packages": sorted(unit_dependencies),
        "build_commands": commands, "target_architecture": architecture,
        "binaries": {str(Path(c[c.index("-o") + 1]).relative_to(output)):
                     digest(Path(c[c.index("-o") + 1])) for c in commands},
        "go_version": run(["go", "version"], source, capture=True).stdout.strip(),
        "qualification": "Source build and upstream unit tests; image and sandbox qualification are separate."
    }, indent=2) + "\n")


def build(name, manifest, build_root=BUILD_ROOT, output=OUTPUT):
    spec = manifest["components"][name]
    validate_spec(name, spec)
    require(subprocess.check_output(["go", "version"], env=go_environment(), text=True).split()[2]
            == manifest["go_version"],
            "Unexpected Go toolchain")
    architecture = os.environ.get("TARGETARCH", "")
    require(architecture in ("amd64", "arm64"), "TARGETARCH must be explicit")
    build_root.mkdir(parents=True, exist_ok=True)
    source = build_root / name
    url = f"https://codeload.github.com/{spec['repository']}/tar.gz/{spec['commit']}"
    with tempfile.NamedTemporaryFile(dir=build_root, suffix=".tar.gz") as temporary:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=60) as response:
            size = 0
            while block := response.read(1024 * 1024):
                size += len(block)
                require(size <= MAX_ARCHIVE, "Source download exceeds limit")
                temporary.write(block)
        temporary.flush()
        require(size == spec["compressed_bytes"], "Source archive size mismatch")
        unpack(Path(temporary.name), source, spec)
    install_locks(source, LOCKS / name, spec)
    retain_namesgenerator(source, spec)
    fix_runtime_test(source, spec)
    tags = {"containerd": "urfave_cli_no_docs,osusergo,netgo,static_build", "compose": "e2e"}.get(name)
    tag_arguments = ["-tags", tags] if tags else []
    packages = test_packages(name, run(["go", "list", *tag_arguments, "./..."], source, capture=True).stdout.splitlines())
    unit_dependencies = set()
    if spec.get("retained_namesgenerator"):
        result = run(["go", "list", "-deps", "-test", "-json", *packages], source, capture=True)
        unit_dependencies = {p["ImportPath"] for p in json_stream(result.stdout)}
        check_retained_graph(unit_dependencies)
    # Preserve actual test/skip outcomes and do not substitute Go's result cache
    # for a fresh execution when this build step runs.
    test_command = ["go", "test", "-count=1", "-v", "-p", "2", "-timeout", "10m",
                    *tag_arguments, *packages]
    require(packages, "No upstream unit packages selected")
    # Buildx's own unit-test target deliberately excludes external daemon integration.
    prior = os.environ.get("SKIP_INTEGRATION_TESTS")
    if name == "buildx":
        os.environ["SKIP_INTEGRATION_TESTS"] = "1"
    try:
        run(test_command, source)
    finally:
        if prior is None:
            os.environ.pop("SKIP_INTEGRATION_TESTS", None)
        else:
            os.environ["SKIP_INTEGRATION_TESTS"] = prior
    for subdir in ("bin", "cli-plugins"):
        (output / subdir).mkdir(parents=True, exist_ok=True)
    commands = build_commands(name, spec, output)
    for command in commands:
        require(not Path(command[command.index("-o") + 1]).exists(), "Output already exists")
        run(command, source, architecture=architecture)
    for lock in ("go.mod", "go.sum"):
        require(digest(source / lock) == spec["fixed_locks"][lock], "Build changed reviewed locks")
    verify_runtime_test(source, spec)
    preserve_notices(source, output / "notices" / name, name, spec, commands,
                     architecture, test_command, output, unit_dependencies)
    for lock in ("go.mod", "go.sum"):
        require(digest(source / lock) == spec["fixed_locks"][lock], "Notice collection changed locks")
    shutil.rmtree(source)


def ci_directories(name):
    require(os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_OS") == "Linux", "CI mode requires a Linux Actions worker")
    temporary = Path(os.environ.get("RUNNER_TEMP", ""))
    require(temporary.is_absolute() and temporary.is_dir() and not temporary.is_symlink(),
            "CI mode requires the worker temporary directory")
    stage = temporary / ("victron-go-tools-" + name)
    stage.mkdir(mode=0o700, exist_ok=False)
    return stage / "source", stage / "output"


def changed_inputs(paths):
    exact = {"scripts/build_runner_go_tools.py", "tests/test_runner_go_tools.py",
             "tests/test_public_runner_image.py", ".github/workflows/contract-tests.yml",
             "deploy/runner-reserve/Dockerfile", "deploy/runner-reserve/Dockerfile.dockerignore"}
    return any(path in exact or path.startswith(".github/containers/runner-go-tools") for path in paths)


def ci_needs_build():
    if os.environ.get("GITHUB_EVENT_NAME") != "pull_request":
        return True
    revision = os.environ.get("BASE_REVISION", "")
    require(re.fullmatch(r"[0-9a-f]{40}", revision), "Full PR base revision required")
    changed = subprocess.check_output(["git", "diff", "--name-only", revision + "...HEAD"],
                                      cwd=ROOT, text=True).splitlines()
    return changed_inputs(changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=REPOSITORIES, nargs="?")
    parser.add_argument("--check-changes", action="store_true", help="Check PR inputs; no source download")
    parser.add_argument("--ci", action="store_true",
                        help="Use an exclusive Linux Actions temporary directory")
    args = parser.parse_args()
    if args.check_changes:
        require(not args.component and not args.ci, "Change detection cannot build")
        print(str(ci_needs_build()).lower())
        return
    require(args.component, "A component is required")
    manifest = json.loads(MANIFEST.read_text())
    require(manifest["schema"] == 1 and set(manifest["components"]) == set(REPOSITORIES),
            "Unexpected tool manifest")
    directories = ci_directories(args.component) if args.ci else (BUILD_ROOT, OUTPUT)
    build(args.component, manifest, *directories)


if __name__ == "__main__":
    main()
