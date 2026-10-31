"""Apply the reviewed package producer-identity delta to known vendored runtimes.

This renderer returns changes only after validating the complete selected set.
It never writes files, upgrades the release engine or modifies workflow pins.
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import stat

HEADER = (
    "# Vendored release toolkit; change the toolkit source, then render again.\n"
    "# ruff: noqa\n# mypy: ignore-errors\n# pylint: skip-file\n# fmt: off\n"
)
RECEIPT = "scripts/version_receipt.py"
PUBLISHER = "scripts/release_versioned.py"
SIGNER = "scripts/release_signing.py"
DELTA = Path(__file__).with_name("release_receipt_delta.json")


def read_local(directory: Path, relative: str) -> str:
    """Read bounded regular files in the selected checkout without following aliases."""
    root = directory.resolve(strict=True)
    path = root / relative
    if any(parent.is_symlink() for parent in (path, *path.parents) if parent != root and root in parent.parents):
        raise ValueError("Receipt migration requires local regular files: " + relative)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 512_000:
        raise ValueError("Receipt migration requires bounded regular files: " + relative)
    return path.read_bytes().decode("utf-8")


def digest(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def transform(source: str, name: str, specification: dict) -> tuple[str, str]:
    """Admit exact reviewed whole-file shapes and preserve consumer-only metadata."""
    prefix = "#!/usr/bin/env python3\n" + HEADER
    if not source.startswith(prefix):
        raise ValueError("Receipt migration requires the generated vendor header: " + name)
    body = source.replace(HEADER, "", 1)
    detached = None
    if name == RECEIPT:
        markers = re.findall(r"^DETACHED_PROVENANCE_REQUIRED = (True|False)$", body, re.MULTILINE)
        if len(markers) != 1:
            raise ValueError("Unknown receipt provenance flag")
        detached = markers[0]
        body = body.replace("DETACHED_PROVENANCE_REQUIRED = " + detached,
                            "DETACHED_PROVENANCE_REQUIRED = False", 1)
    identity = digest(body)
    if identity in specification["current"]:
        return "current", source
    target = specification["original_to_current"].get(identity)
    if target is None:
        raise ValueError("Unknown or partially migrated receipt runtime: " + name)
    for hunk in specification["hunks"]:
        if body.count(hunk["before"]) != 1:
            raise ValueError("Reviewed receipt delta context changed: " + name)
        body = body.replace(hunk["before"], hunk["after"], 1)
    if digest(body) != target:
        raise ValueError("Reviewed receipt delta output differs: " + name)
    if detached is not None:
        body = body.replace("DETACHED_PROVENANCE_REQUIRED = False",
                            "DETACHED_PROVENANCE_REQUIRED = " + detached, 1)
    result = body.replace("#!/usr/bin/env python3\n", prefix, 1)
    ast.parse(result, filename=name)
    return "original", result


def render(directory: Path) -> dict[str, str]:
    """Return only the receipt, publisher and optional signer changes; reject mixed sets."""
    policy = json.loads(read_local(directory, ".release-policy.json"))
    versioning = policy.get("versioning") if isinstance(policy, dict) else None
    if (not isinstance(versioning, dict) or type(versioning.get("schema")) is not int
            or versioning["schema"] != 1 or policy.get("mode", "release") != "release"
            or policy.get("visibility", "public") != "public"
            or policy.get("ci_execution", "github") != "github"
            or type(policy.get("sign_releases", False)) is not bool):
        raise ValueError("Receipt migration requires an existing public hosted versioned release")
    specification = json.loads(DELTA.read_text(encoding="utf-8"))["files"]
    names = [RECEIPT, PUBLISHER]
    if policy.get("sign_releases") or (directory / SIGNER).exists() or (directory / SIGNER).is_symlink():
        names.append(SIGNER)
    states = set()
    changes = {}
    for name in names:
        source = read_local(directory, name)
        state, result = transform(source, name, specification[name])
        states.add(state)
        if result != source:
            changes[name] = result
    if len(states) != 1:
        raise ValueError("Partially migrated receipt/publisher/signer set requires explicit review")
    return changes


def operator_guide() -> str:
    """Describe the narrow migration and the fresh-build boundary it creates."""
    return """### Migrate package receipt identity without replacing the release engine

For an existing public hosted versioned release, the toolkit can migrate reviewed
receipt, publisher and signing-preparation code without replacing action pins,
workflow adapters, package policy, or unrelated release APIs:

```sh
python3 scripts/install_release.py /path/to/consumer --release-receipts-only --check
python3 scripts/install_release.py /path/to/consumer --release-receipts-only
```

`--check` reports pending changes without writing. The migration recognizes
reviewed complete source shapes only. Unknown customizations, partly updated
functions or mixed old/new receipt, publisher and signer files require explicit
review; the renderer refuses the complete update before writing anything.
The generated header and detached-provenance requirement are preserved.

After migration, run a fresh, complete build of all required package producers in
one workflow attempt. The publisher verifies the independently supplied repository,
run ID, attempt and source SHA against every package receipt. Pre-migration receipts
without producer identity cannot qualify fresh package publication through this
migrated publisher. Existing historical promotion and retained-evidence validation
remain separate and unchanged. A failed-job-only retry cannot
fill missing current-attempt packages from an earlier attempt; regenerate the full
coherent package set instead. Existing release approval and acceptance policies
still apply.

Offline creation and inspection remain supported without an Actions identity, but
an offline receipt cannot qualify fresh publication through the migrated publisher.
Receipt coverage proves that
the staged files have matching receipts; package-set completeness is a separate
check for targets explicitly listed in `versioning.artifacts`. It does not infer
all intended platforms from workflow names. No release is dispatched by this mode.
"""
