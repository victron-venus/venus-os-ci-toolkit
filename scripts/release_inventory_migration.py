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


# Keep the accepted casefold delta immutable; length validation is a separate layer.
NAME_LIMIT = {
    "hunks": [
        {
            "before": "or any(not isinstance(name, str)\n",
            "after": "or any(not isinstance(name, str) or len(name) > 200\n"
        },
        {
            "before": "raise VersionError(\"required_build_receipts must be nonempty ASCII package receipt filenames\")",
            "after": "raise VersionError(\n            \"required_build_receipts must be nonempty ASCII package receipt filenames \"\n            \"of at most 200 characters\"\n        )"
        }
    ],
    "original_to_current": {
        "212ad31b8fa380b0ef556b82fd4ed6f2d49cd59ee4516956e98bb8a9120483e9": "039cd5827ab5c56c6e9453b46a86ce10a3b2e2552f8e9a653bdfb915a0a2139a"
    },
    "current": [
        "039cd5827ab5c56c6e9453b46a86ce10a3b2e2552f8e9a653bdfb915a0a2139a"
    ]
}


# Recognize the independently reviewed notes fix without changing inventory-only scope.
NOTES_PROVENANCE = {
    "hunks": [
        {
            "before": "    return \"\"\n\n\ndef prepare(args: argparse.Namespace) -> dict[str, object]:\n    \"\"\"Freeze one durable plan before any platform build consumes version files.\"\"\"\n    inputs = event_inputs()\n",
            "after": "    return \"\"\n\n\ndef candidate_provenance(\n    repository: str, channel: str, source_sha: str, run_id: int,\n    parent_tag: str | None,\n) -> str:\n    \"\"\"Render the exact known publication text before any version reservation.\"\"\"\n    description = (\n        f\"Final build from accepted {parent_tag}; new verified package bytes.\"\n        if parent_tag\n        else f\"{channel} candidate with a version fixed before compilation.\"\n    )\n    return (\n        description + f\"\\n\\nSource: `{source_sha}`\\n\\n\"\n        f\"Validation: https://github.com/{repository}/actions/runs/{run_id}\\n\\n\"\n        f\"See `{rc.MANIFEST}` for package hashes and version input evidence.\"\n    )\n\n\ndef prepare(args: argparse.Namespace) -> dict[str, object]:\n    \"\"\"Freeze one durable plan before any platform build consumes version files.\"\"\"\n    inputs = event_inputs()\n"
        },
        {
            "before": "        # Every channel selects the base-version section; do not allocate a\n        # sequence or build number merely to discover unpublishable notes.\n        # The publisher independently validates the same source again.\n        rc.release_notes(gh, f\"v{base}\", cast(str, run[\"head_sha\"]), \"\")\n    plan = reserve_plan(\n        gh,\n        policy,\n",
            "after": "        # Every channel selects the base-version section; do not allocate a\n        # sequence or build number merely to discover unpublishable notes.\n        # The publisher independently validates the same source again.\n        rc.release_notes(\n            gh, f\"v{base}\", cast(str, run[\"head_sha\"]),\n            candidate_provenance(\n                gh.repo, channel, cast(str, run[\"head_sha\"]), cast(int, run[\"id\"]),\n                cast(str, parent[\"tag\"]) if parent else None,\n            ),\n        )\n    plan = reserve_plan(\n        gh,\n        policy,\n"
        },
        {
            "before": "        verify_reservation(gh, plan, cast(int, run[\"id\"]), parent)\n        if channel == \"stable\":\n            rc.require_reviewers(gh)\n        description = (\n            f\"Final build from accepted {parent['tag']}; new verified package bytes.\"\n            if parent\n            else f\"{channel} candidate with a version fixed before compilation.\"\n        )\n        superseded = rc.superseded_candidate(gh, info, run, channel)\n        if superseded:\n            return superseded\n",
            "after": "        verify_reservation(gh, plan, cast(int, run[\"id\"]), parent)\n        if channel == \"stable\":\n            rc.require_reviewers(gh)\n        superseded = rc.superseded_candidate(gh, info, run, channel)\n        if superseded:\n            return superseded\n"
        },
        {
            "before": "            gh,\n            cast(str, plan[\"tag\"]),\n            cast(str, plan[\"source_sha\"]),\n            description + f\"\\n\\nSource: `{plan['source_sha']}`\\n\\n\"\n            f\"Validation: https://github.com/{gh.repo}/actions/runs/{run['id']}\\n\\n\"\n            f\"See `{rc.MANIFEST}` for package hashes and version input evidence.\",\n        )\n        rc.EVIDENCE.parent.mkdir(parents=True, exist_ok=True)\n        rc.EVIDENCE.write_bytes(content)\n",
            "after": "            gh,\n            cast(str, plan[\"tag\"]),\n            cast(str, plan[\"source_sha\"]),\n            candidate_provenance(\n                gh.repo, channel, cast(str, plan[\"source_sha\"]), cast(int, run[\"id\"]),\n                cast(str, parent[\"tag\"]) if parent else None,\n            ),\n        )\n        rc.EVIDENCE.parent.mkdir(parents=True, exist_ok=True)\n        rc.EVIDENCE.write_bytes(content)\n"
        }
    ],
    "original_to_current": {
        "179a6fe589801c59bccfea0c9a0c2e1b5d0be1915e87e1b4ec9313f32380aa7a": "579c4409b768f90311a58eb6e9afef2eb423fa4ccdd75f9056e31d0e3497ee99"
    },
    "current": [
        "579c4409b768f90311a58eb6e9afef2eb423fa4ccdd75f9056e31d0e3497ee99"
    ]
}


# Recognize response reconciliation without adding it through inventory-only updates.
CREATE_READBACK = {
    "hunks": [
        {
            "before": "from __future__ import annotations\n\nimport argparse\nimport json\nimport os\nimport re\nimport sys\nimport tempfile\nfrom collections.abc import Iterator\nfrom datetime import datetime, timezone\nfrom pathlib import Path\nfrom typing import cast\n",
            "after": "from __future__ import annotations\n\nimport argparse\nimport base64\nimport json\nimport os\nimport re\nimport secrets\nimport sys\nimport tempfile\nfrom collections.abc import Callable, Iterator\nfrom datetime import datetime, timezone\nfrom pathlib import Path\nfrom typing import cast\n"
        },
        {
            "before": "    if gate:\n        rc.validate_validation_run(gh, run, cast(dict[str, object], snapshot[\"data\"]), channel)\n    return info, run, snapshot\n\n\n# Keep source, workflow and byte verification together at the trust boundary.\n",
            "after": "    if gate:\n        rc.validate_validation_run(gh, run, cast(dict[str, object], snapshot[\"data\"]), channel)\n    return info, run, snapshot\n\n\ndef create_marker(repository: str, run: dict[str, object], plan_digest: str, nonce: str) -> str:\n    \"\"\"Reserve the same UTF-8 body space at preparation and actual publication.\"\"\"\n    identity = {\"repository\": repository, \"source\": run[\"head_sha\"],\n                \"run\": run[\"id\"], \"attempt\": run[\"run_attempt\"],\n                \"plan\": plan_digest, \"nonce\": nonce}\n    marker = json.dumps(identity, sort_keys=True, separators=(\",\", \":\"))\n    return \"\\n\\n<!-- release-create \" + marker + \" -->\"\n\n\ndef create_confirmation(\n    run: dict[str, object], source_ref: tuple[str, object], consumed: bytes,\n) -> Callable[[rc.CreateReadback], None]:\n    \"\"\"Bind one release-create call, without adding any cross-run recovery state.\"\"\"\n\n    def confirm(readback: rc.CreateReadback) -> None:\n        current = readback.json(f\"actions/runs/{run['id']}/attempts/{run['run_attempt']}\")\n        rc.require(isinstance(current, dict) and current.get(\"status\") == \"in_progress\"\n                   and current.get(\"conclusion\") is None\n                   and all(type(current.get(key)) is type(run[key]) and current[key] == run[key] for key in\n                           (\"id\", \"run_attempt\", \"head_sha\", \"path\", \"event\")),\n                   \"Create readback run identity changed\")\n        rc.require(readback.json(source_ref[0]) == source_ref[1],\n                   \"Create readback default source changed\")\n        content = readback.json(\"contents/release-version-state.json?ref=release-version-state\")\n        rc.require(isinstance(content, dict) and content.get(\"type\") == \"file\"\n                   and content.get(\"encoding\") == \"base64\" and isinstance(content.get(\"content\"), str),\n                   \"Invalid create readback ledger\")\n        encoded = cast(str, cast(dict[str, object], content)[\"content\"])\n        raw = base64.b64decode(\"\".join(encoded.split()), validate=True)\n        rc.require(raw == consumed, \"Create readback publication ledger changed\")\n\n    return confirm\n\n\n# Keep source, workflow and byte verification together at the trust boundary.\n"
        },
        {
            "before": "            candidate_provenance(\n                gh.repo, channel, cast(str, run[\"head_sha\"]), cast(int, run[\"id\"]),\n                cast(str, parent[\"tag\"]) if parent else None,\n            ),\n        )\n    plan = reserve_plan(\n        gh,\n",
            "after": "            candidate_provenance(\n                gh.repo, channel, cast(str, run[\"head_sha\"]), cast(int, run[\"id\"]),\n                cast(str, parent[\"tag\"]) if parent else None,\n            ) + create_marker(gh.repo, run, \"0\" * 64, \"0\" * 64),\n        )\n    plan = reserve_plan(\n        gh,\n"
        },
        {
            "before": "            candidate_provenance(\n                gh.repo, channel, cast(str, plan[\"source_sha\"]), cast(int, run[\"id\"]),\n                cast(str, parent[\"tag\"]) if parent else None,\n            ),\n        )\n        rc.EVIDENCE.parent.mkdir(parents=True, exist_ok=True)\n        rc.EVIDENCE.write_bytes(content)\n        begin_publication(gh, plan, cast(int, run[\"id\"]), parent)\n        result = rc._publish_prepared(  # pylint: disable=protected-access\n            gh,\n            cast(str, plan[\"tag\"]),\n",
            "after": "            candidate_provenance(\n                gh.repo, channel, cast(str, plan[\"source_sha\"]), cast(int, run[\"id\"]),\n                cast(str, parent[\"tag\"]) if parent else None,\n            ) + create_marker(gh.repo, run, version_plan.plan_digest(plan), secrets.token_hex(32)),\n        )\n        rc.EVIDENCE.parent.mkdir(parents=True, exist_ok=True)\n        rc.EVIDENCE.write_bytes(content)\n        source_path = f\"git/ref/heads/{quote(cast(str, info['default_branch']), safe='')}\"\n        source_ref = (source_path, gh.api(source_path))\n        consumed = begin_publication(gh, plan, cast(int, run[\"id\"]), parent)\n        confirm_create = create_confirmation(run, source_ref, consumed)\n        result = rc._publish_prepared(  # pylint: disable=protected-access\n            gh,\n            cast(str, plan[\"tag\"]),\n"
        },
        {
            "before": "            stage,\n            channel != \"stable\",\n            body,\n        )\n    return {\n        \"status\": \"published\",\n",
            "after": "            stage,\n            channel != \"stable\",\n            body,\n            confirm_create=confirm_create,\n        )\n    return {\n        \"status\": \"published\",\n"
        }
    ],
    "original_to_current": {
        "579c4409b768f90311a58eb6e9afef2eb423fa4ccdd75f9056e31d0e3497ee99": "2d2e1b65544c817107a4d7a5008fcc11335a9408faec4cb159c87d454d58c7dd"
    },
    "current": [
        "2d2e1b65544c817107a4d7a5008fcc11335a9408faec4cb159c87d454d58c7dd"
    ]
}

def _before_layer(source: str, layer: dict, label: str) -> str:
    """Reverse one exact accepted layer, preserving a vendor header."""
    prefix = "#!/usr/bin/env python3\n" + receipts.HEADER
    vendored = source.startswith(prefix)
    body = source.replace(receipts.HEADER, "", 1) if vendored else source
    current = receipts.digest(body)
    if current not in layer["current"]:
        return source
    for hunk in reversed(layer["hunks"]):
        if body.count(hunk["after"]) != 1:
            raise ValueError(f"{label} reverse context changed")
        body = body.replace(hunk["after"], hunk["before"], 1)
    if layer["original_to_current"].get(receipts.digest(body)) != current:
        raise ValueError(f"{label} reverse result changed")
    return body.replace("#!/usr/bin/env python3\n", prefix, 1) if vendored else body


def _before_create_readback(source: str) -> str:
    """Return the exact notes-only publisher when the new layer is recognized."""
    return _before_layer(source, CREATE_READBACK, "Create readback")


def _before_notes_provenance(source: str) -> str:
    """Unwrap exact known publisher layers for the unchanged historical oracle."""
    return _before_layer(_before_create_readback(source), NOTES_PROVENANCE, "Notes provenance")


def _before_name_limit(source: str) -> str:
    """Return the exact accepted pre-length-limit source when recognized."""
    return _before_layer(source, NAME_LIMIT, "Receipt name limit")


def _before_casefold(source: str) -> str:
    """Reverse accepted plan layers to the immutable inventory-v1 source."""
    return _before_layer(_before_name_limit(source), CASEFOLD, "Casefold")


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
        if name == receipts.PUBLISHER:
            prior = _before_notes_provenance(source)
        state, result = receipts.transform(prior, name, specification[name])
        if name == PLAN:
            _, result = receipts.transform(result, name, CASEFOLD)
            _, result = receipts.transform(result, name, NAME_LIMIT)
        elif name == receipts.PUBLISHER and prior != source:
            # Preserve a fix already present; this mode does not install it.
            _, result = receipts.transform(result, name, NOTES_PROVENANCE)
            if _before_create_readback(source) != source:
                _, result = receipts.transform(result, name, CREATE_READBACK)
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
    elif name == receipts.PUBLISHER:
        source = _before_notes_provenance(source)
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
Each complete filename must be at most 200 ASCII characters, matching the release
asset staging limit; longer names are rejected during policy validation.
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
