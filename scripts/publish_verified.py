#!/usr/bin/env python3
"""Publish verified stable assets to a registry without rebuilding.

The GitHub stable release must already exist. Payload bytes are checked against
the original RC manifest AND its immutable Actions evidence before any write.
Registry credentials come from the operator's existing docker or skopeo login;
docker credential helpers (credsStore/credHelpers) are resolved here because
skopeo does not read that part of the docker client configuration.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
# Publication uses fixed tool commands and verified inputs without a shell.
import subprocess  # nosec B404
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

if __package__:
    from .release_control import (
        MANIFEST,
        DownloadBudget,
        GitHub,
        ReleaseError,
        download_asset,
        positive,
        repository_info,
        require,
        require_release_policy,
        source_policy_snapshot,
        validate_manifest,
        validate_run,
        verify_evidence,
    )
else:
    from release_control import (
        MANIFEST,
        DownloadBudget,
        GitHub,
        ReleaseError,
        download_asset,
        positive,
        repository_info,
        require,
        require_release_policy,
        source_policy_snapshot,
        validate_manifest,
        validate_run,
        verify_evidence,
    )

ROOT = Path(__file__).resolve().parents[1]
DOCKER_TRANSPORT = "docker://"
DOCKER_HUB_HOSTS = {"docker.io", "index.docker.io", "registry-1.docker.io"}
DOCKER_HUB_SERVER = "https://index.docker.io/v1/"


def _read_stable_release(gh, tag):
    """Read the published release, tag and bounded manifest before trusting contents."""
    require(
        re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag),
        "An exact stable vX.Y.Z tag is required",
    )
    release = gh.api(f"releases/tags/{quote(tag, safe='')}")
    require(
        release.get("draft") is False and release.get("prerelease") is False,
        "Not a published stable release",
    )
    ref = gh.api(f"git/ref/tags/{quote(tag, safe='')}")
    assets = gh.pages(f"releases/{positive(release['id'], 'release ID')}/assets")
    manifests = [asset for asset in assets if asset["name"] == MANIFEST]
    require(
        len(manifests) == 1 and 0 < manifests[0].get("size", 2_000_001) <= 2_000_000,
        "Stable release has no bounded unambiguous manifest",
    )
    raw = gh.binary(f"releases/assets/{manifests[0]['id']}")
    candidate = json.loads(raw)
    require(isinstance(candidate, dict), "Stable manifest must be an object")
    return release, ref, assets, raw, candidate


def _read_versioned_stable_release(gh, tag, directory, raw, candidate):
    """Resolve the strict schema-2 compact distribution through its RC evidence."""
    if type(candidate.get("schema")) is not int or candidate["schema"] != 2:
        return None
    if __package__:
        from .release_versioned import read_verified_distribution
    else:
        from release_versioned import read_verified_distribution
    manifest, _, _ = read_verified_distribution(gh, tag, raw, stage=directory)
    return manifest


def _validate_legacy_stable_provenance(gh, tag, ref, raw, candidate):
    """Validate original policy, tag source and immutable run evidence."""
    manifest = validate_manifest(raw, gh.repo, candidate.get("tag", ""), allow_final=True)
    original_policy = source_policy_snapshot(gh, manifest["source_sha"])
    require_release_policy(original_policy["data"], gh.repo, qualified=True)
    require(
        original_policy == manifest["source_policy"],
        "Manifest policy snapshot differs from the policy at the candidate source commit",
    )
    require("v" + manifest["version"] == tag, "Stable version differs from the RC")
    require(
        ref.get("object", {}).get("type") == "commit"
        and ref["object"]["sha"] == manifest["source_sha"],
        "Stable tag points to different source",
    )
    info = repository_info(gh)
    run = gh.api(f"actions/runs/{manifest['run_id']}")
    require(run.get("id") == manifest["run_id"], "Source run identity mismatch")
    require(run.get("event") == "workflow_dispatch",
            "Release candidate must come from a manual workflow dispatch")
    validate_run(gh, run, info, manifest["source_sha"], manifest["run_attempt"], completed=True)
    verify_evidence(gh, manifest, raw)
    if manifest["channel"] == "stable":
        if __package__:
            from .release_versioned import verified_rc
        else:
            from release_versioned import verified_rc
        _, parent = verified_rc(
            gh, manifest["derived_from_rc"]["tag"], info, run,
        )
        require(
            parent == manifest["derived_from_rc"],
            "Final package RC provenance mismatch",
        )
    return manifest, original_policy


def _download_stable_inventory(gh, assets, manifest, directory):
    """Download each exact stable payload using one inventory-wide byte budget."""
    expected = {item["name"]: item for item in manifest["assets"]}
    require(
        len(assets) == len(expected) + 1
        and {asset["name"] for asset in assets} == set(expected) | {MANIFEST},
        "Stable inventory differs from RC evidence",
    )
    budget = DownloadBudget(sum(item["size"] for item in expected.values()))
    for asset in assets:
        if asset["name"] == MANIFEST:
            continue
        item = expected[asset["name"]]
        identity = download_asset(
            gh, asset["id"], directory / asset["name"],
            expected={key: item[key] for key in ("size", "sha256")}, budget=budget,
        )
        require(
            asset.get("state") == "uploaded"
            and identity["size"] == item["size"]
            and identity["sha256"] == item["sha256"],
            f"Stable payload changed: {asset['name']}",
        )


def _verify_stable_receipts(directory, manifest, original_policy):
    """Check any package and declared-artifact receipts after byte verification."""
    if not manifest.get("version_plan"):
        return
    if __package__:
        from .version_receipt import verify_declared_artifacts, verify_receipts
    else:
        from version_receipt import verify_declared_artifacts, verify_receipts
    verify_receipts(directory, manifest["version_plan"], manifest["assets"], original_policy["data"])
    verify_declared_artifacts(directory, original_policy["data"], manifest["version_plan"])


def verified_assets(gh, tag, directory):
    """Verify stable provenance and source policy before downloading approved bytes."""
    _release, ref, assets, raw, candidate = _read_stable_release(gh, tag)
    manifest = _read_versioned_stable_release(gh, tag, directory, raw, candidate)
    if manifest is not None:
        return manifest
    manifest, original_policy = _validate_legacy_stable_provenance(
        gh, tag, ref, raw, candidate,
    )
    _download_stable_inventory(gh, assets, manifest, directory)
    _verify_stable_receipts(directory, manifest, original_policy)
    return manifest

def verified_publication_config(policy, manifest, field):
    """Reject destination mappings that differ from the immutable RC policy."""
    require(field in {"container_assets", "pypi_assets"}, "Unknown publication mapping")
    default = {} if field == "container_assets" else []
    original = manifest["source_policy"]["data"].get(field, default)
    require(
        policy.get(field, default) == original,
        f"Current {field} differs from the candidate source policy; "
        "use the original mapping or create a new RC",
    )
    return original


def docker_config():
    """Return the operator's docker client configuration when it is readable."""
    configured = os.environ.get("DOCKER_CONFIG")
    path = (
        Path(configured) / "config.json"
        if configured
        else Path.home() / ".docker" / "config.json"
    )
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def docker_helper_credentials(helper, server):
    """Resolve one registry login through a docker credential helper."""
    require(
        isinstance(helper, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", helper),
        "Invalid docker credential helper name",
    )
    try:
        result = subprocess.run(  # nosec B603, B607
            ["docker-credential-" + helper, "get"],
            input=server,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        credential = json.loads(result.stdout)
    except ValueError:
        return None
    username = credential.get("Username")
    secret = credential.get("Secret")
    if not isinstance(username, str) or not isinstance(secret, str) or not secret:
        return None
    return username, secret


def registry_auth_entries(images):
    """Build skopeo-readable auths for destinations logged in via docker helpers."""
    config = docker_config()
    auths = config.get("auths")
    auths = auths if isinstance(auths, dict) else {}
    helpers = config.get("credHelpers")
    helpers = helpers if isinstance(helpers, dict) else {}
    store = config.get("credsStore")
    store = store if isinstance(store, str) else None
    resolved = {}
    missing = []
    for host in sorted({image.split("/", 1)[0] for image in images}):
        server = DOCKER_HUB_SERVER if host in DOCKER_HUB_HOSTS else host
        if any(
            isinstance(auths.get(key), dict)
            and (auths[key].get("auth") or auths[key].get("identitytoken"))
            for key in (server, host)
        ):
            # The docker client configuration already carries this login.
            continue
        helper = helpers.get(host) or store
        credential = docker_helper_credentials(helper, server) if helper else None
        if credential is None and helper:
            credential = docker_helper_credentials(helper, host)
        if credential is None:
            missing.append(host)
            continue
        username, secret = credential
        resolved[server] = {
            "auth": base64.b64encode(f"{username}:{secret}".encode()).decode()
        }
    require(
        not missing,
        "No registry credentials for " + ", ".join(missing)
        + "; run docker login for that registry and retry",
    )
    return auths, resolved


@contextmanager
def registry_credentials(images):
    """Expose docker login credentials to skopeo before any payload download."""
    # Operator-provided skopeo auth file already covers every registry.
    if os.environ.get("REGISTRY_AUTH_FILE"):
        yield
        return
    auths, resolved = registry_auth_entries(images)
    if not resolved:
        yield
        return
    with tempfile.TemporaryDirectory(prefix="registry-auth-") as temporary:
        path = Path(temporary) / "config.json"
        path.write_text(json.dumps({"auths": {**auths, **resolved}}))
        path.chmod(0o600)
        previous = os.environ.get("DOCKER_CONFIG")
        os.environ["DOCKER_CONFIG"] = temporary
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("DOCKER_CONFIG", None)
            else:
                os.environ["DOCKER_CONFIG"] = previous


def require_unpublished_version(image, version):
    """Fail closed unless registry inventory proves the version is unpublished."""
    # The caller validates the registry destination; skopeo receives one transport argument.
    check = subprocess.run(  # nosec B603, B607
        ["skopeo", "list-tags", DOCKER_TRANSPORT + image],
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode == 0:
        require(
            version not in json.loads(check.stdout).get("Tags", []),
            f"Container version already exists: {image}:{version}",
        )
    # Skopeo can render the registry's NAME_UNKNOWN code as "name unknown".
    # Require an unambiguous terminal diagnostic: a missing-repository phrase
    # followed by a login or permission warning is not proof of absence.
    elif not re.search(
        r"(?:\bNAME_UNKNOWN\b(?:: repository name not known to registry)?"
        r"|\bname unknown\b|\brepository does not exist\b)[\"']?\s*$",
        check.stderr,
        re.IGNORECASE,
    ):
        raise ReleaseError(
            "Cannot establish registry tag inventory; authenticate and retry: " + image
        )


def image_copy_command(archive, image, tag):
    """Copy every approved OCI platform without rebuilding or changing digests."""
    return [
        "skopeo",
        "copy",
        "--all",
        "--preserve-digests",
        "oci-archive:" + str(archive),
        DOCKER_TRANSPORT + image + ":" + tag,
    ]


def container_commands(policy, manifest, directory, latest):
    """Preflight all original destinations, then order version and latest copies."""
    mapping = verified_publication_config(policy, manifest, "container_assets")
    require(mapping, "No container archive mapping in .release-policy.json")
    commands = []
    for name, image in mapping.items():
        require(
            (directory / name).is_file() and Path(name).name == name,
            f"Missing approved image archive: {name}",
        )
        require(
            re.fullmatch(r"(?:ghcr\.io|docker\.io)/[a-z0-9][a-z0-9._/-]*", image),
            f"Invalid configured image destination: {image}",
        )
        # Never overwrite an existing version tag, even on retries.
        require_unpublished_version(image, manifest["version"])
        commands.append(
            image_copy_command(directory / name, image, manifest["version"])
        )
    if latest:
        commands.extend(
            image_copy_command(directory / name, image, "latest")
            for name, image in mapping.items()
        )
    return commands


def pypi_commands(policy, manifest, directory):
    """Check and upload only distributions named by the original source policy."""
    patterns = verified_publication_config(policy, manifest, "pypi_assets")
    files = sorted(
        {
            str(path)
            for pattern in patterns
            for path in directory.iterdir()
            if fnmatch.fnmatch(path.name, pattern)
        }
    )
    require(
        files and any(name.endswith(".whl") for name in files),
        "No verified wheel assets configured for PyPI",
    )
    return [
        ["python3", "-m", "twine", "check", *files],
        ["python3", "-m", "twine", "upload", *files],
    ]


def main():
    """Display or execute registry publication from verified stable payloads."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=["containers", "pypi"])
    parser.add_argument("--tag", required=True)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Perform the displayed registry publication (default verifies only)",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Also move container latest after every versioned image is published",
    )
    args = parser.parse_args()
    try:
        policy = json.loads((ROOT / ".release-policy.json").read_text())
        gh = GitHub(policy["repository"])
        mapping = policy.get("container_assets")
        images = (
            [image for image in mapping.values() if isinstance(image, str)]
            if args.target == "containers" and isinstance(mapping, dict)
            else []
        )
        # Fail on missing logins before the multi-hundred-megabyte verification.
        with registry_credentials(images):
            with tempfile.TemporaryDirectory(prefix="verified-publication-") as temp:
                directory = Path(temp)
                manifest = verified_assets(gh, args.tag, directory)
                if args.target == "containers":
                    commands = container_commands(
                        policy, manifest, directory, args.latest
                    )
                else:
                    commands = pypi_commands(policy, manifest, directory)
                print(
                    json.dumps(
                        {
                            "repository": gh.repo,
                            "stable_tag": args.tag,
                            "candidate": manifest["tag"],
                            "commands": commands,
                            "execute": args.execute,
                        },
                        indent=2,
                    )
                )
                if args.execute:
                    for command in commands:
                        # Commands come only from the verified builders above.
                        subprocess.run(command, check=True)  # nosec B603
        return 0
    except (
        ReleaseError,
        OSError,
        ValueError,
        KeyError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"publish-verified: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
