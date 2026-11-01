"""Add the reviewed final-publication inventory guard to known release runtimes."""
from __future__ import annotations

import json
from pathlib import Path

import release_receipt_migration as receipts
import version_plan

PLAN = "scripts/version_plan.py"
FILES = (PLAN, receipts.RECEIPT, receipts.PUBLISHER)
DELTA = Path(__file__).with_name("release_inventory_delta.json")

# Keep the accepted inventory-v1 delta and reversal oracle immutable.
CASEFOLD = {
    "hunks": [
        {
            "before": "    require(len(set(names)) == len(names), \"required_build_receipts contains duplicate filenames\")",
            "after": "    require(\n        len({name.casefold() for name in names}) == len(names),\n        \"required_build_receipts contains duplicate or case-colliding filenames\",\n    )"
        }
    ],
    "original_to_current": {
        "fbc56a9d494678f6b5d9180be02d587897c2b5276cf46e9102ac61089c91dde2": "212ad31b8fa380b0ef556b82fd4ed6f2d49cd59ee4516956e98bb8a9120483e9"
    },
    "current": [
        "212ad31b8fa380b0ef556b82fd4ed6f2d49cd59ee4516956e98bb8a9120483e9"
    ]
}


def _before_casefold(source: str) -> str:
    """Reverse only the exact accepted casefold layer, preserving a vendor header."""
    prefix = "#!/usr/bin/env python3\n" + receipts.HEADER
    vendored = source.startswith(prefix)
    body = source.replace(receipts.HEADER, "", 1) if vendored else source
    current = receipts.digest(body)
    if current not in CASEFOLD["current"]:
        return source
    for hunk in reversed(CASEFOLD["hunks"]):
        if body.count(hunk["after"]) != 1:
            raise ValueError("Casefold reverse context changed")
        body = body.replace(hunk["after"], hunk["before"], 1)
    if CASEFOLD["original_to_current"].get(receipts.digest(body)) != current:
        raise ValueError("Casefold reverse result changed")
    return body.replace("#!/usr/bin/env python3\n", prefix, 1) if vendored else body


def render(directory: Path) -> dict[str, str]:
    """Require an explicit inventory, then validate all files before returning edits."""
    policy = json.loads(receipts.read_local(directory, ".release-policy.json"))
    config = policy.get("versioning") if isinstance(policy, dict) else None
    if (
        not isinstance(config, dict)
        or type(config.get("schema")) is not int
        or config["schema"] != 1
        or policy.get("mode", "release") != "release"
        or policy.get("visibility", "public") != "public"
        or policy.get("ci_execution", "github") != "github"
    ):
        raise ValueError("Inventory migration requires a public hosted versioned release")
    if version_plan.required_build_receipts(config) is None:
        raise ValueError("Declare reviewed versioning.required_build_receipts first")
    specification = json.loads(DELTA.read_text(encoding="utf-8"))["files"]
    changes = {}
    states = set()
    for name in FILES:
        source = receipts.read_local(directory, name)
        prior = _before_casefold(source) if name == PLAN else source
        state, result = receipts.transform(prior, name, specification[name])
        if name == PLAN:
            _, result = receipts.transform(result, name, CASEFOLD)
        states.add(state)
        if result != source:
            changes[name] = result
    if len(states) != 1:
        raise ValueError("Partially migrated inventory guard requires explicit review")
    return changes


def previous_canonical_source(source: str, name: str) -> str:
    """Reverse only a known inventory delta for the immutable earlier migration oracle."""
    if name == receipts.SIGNER:
        return source
    if name == PLAN:
        source = _before_casefold(source)
    if name not in FILES:
        raise ValueError("Unknown inventory source")
    specification = json.loads(DELTA.read_text(encoding="utf-8"))["files"][name]
    current = receipts.digest(source)
    if current not in specification["current"]:
        raise ValueError("Unknown current inventory source")
    original = source
    for hunk in reversed(specification["hunks"]):
        if original.count(hunk["after"]) != 1:
            raise ValueError("Inventory reverse context changed")
        original = original.replace(hunk["after"], hunk["before"], 1)
    if specification["original_to_current"].get(receipts.digest(original)) != current:
        raise ValueError("Inventory reverse result changed")
    return original


def operator_guide() -> str:
    """Explain source-declared final receipt sets without promising package-format coverage."""
    return """### Require the complete set of package producers

Declare `versioning.required_build_receipts` as a nonempty list of exact receipt
filenames after reviewing every release-build matrix and conditional producer.
Include the final signature receipt when the publisher consumes one. For example,
an ordinary package and a later signing job require:

```json
"required_build_receipts": [
  "release-inputs-package.json",
  "release-inputs-signature.json"
]
```

Do not include `release-inputs-fuzz.json`: the authenticated RC validation receipt
is checked and separated from package receipts before this inventory check.
An intentionally smaller release scope needs its own reviewed policy; a skipped
producer cannot silently reduce the declared set.

For runtimes that already have package producer identity checks, apply only the
reviewed inventory delta with:

```sh
python3 scripts/install_release.py /path/to/consumer --release-inventory-only --check
python3 scripts/install_release.py /path/to/consumer --release-inventory-only
```

This mode changes only the known version-plan validator, receipt validator and
publisher. It does not infer or rewrite policy, workflow pins or signing code.
Unknown or partially migrated files fail before any changes are written.
The final fresh publisher authenticates each receipt's repository, source, run
and attempt, then rejects missing, duplicate or unexpected producer receipts.
Pre-signing validation remains separate because signature receipts do not yet
exist at that stage. Historical RC promotion retains its original source policy.

Update the declared set whenever producer jobs or matrices change. Receipt-set
completeness does not prove every package format inside a producer group; retain
declared artifact metadata checks and release asset verification.
"""
