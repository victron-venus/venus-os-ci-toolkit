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

# Preserve the later default-branch guard without installing it in inventory-only mode.
CREATE_IDENTITY = {
    "hunks": [
        {
            "before": "        rc.require(readback.json(source_ref[0]) == source_ref[1],\n",
            "after": "        info = readback.json(\"\")\n        rc.require(isinstance(info, dict) and isinstance(info.get(\"default_branch\"), str)\n                   and f\"git/ref/heads/{quote(info['default_branch'], safe='')}\" == source_ref[0],\n                   \"Create readback default branch changed\")\n        rc.require(readback.json(source_ref[0]) == source_ref[1],\n"
        }
    ],
    "original_to_current": {
        "2d2e1b65544c817107a4d7a5008fcc11335a9408faec4cb159c87d454d58c7dd": "978783bc3465c174f45669fe38568337d8c886161749f8d1c050ab67d8999e9d"
    },
    "current": [
        "978783bc3465c174f45669fe38568337d8c886161749f8d1c050ab67d8999e9d"
    ]
}


# Preserve the automatic-source fix only when already present.
AUTOMATIC_SOURCE = {
    "hunks": [
        {
            "before": "        source_ref = (source_path, gh.api(source_path))\n        consumed = begin_publication(gh, plan, cast(int, run[\"id\"]), parent)\n",
            "after": "        source_ref = (source_path, gh.api(source_path))\n        automatic = (run[\"event\"], channel) in {(\"push\", \"beta\"), (\"schedule\", \"nightly\")}\n        if automatic:\n            captured = source_ref[1]\n            rc.require(\n                isinstance(captured, dict)\n                and captured.get(\"ref\") == f\"refs/heads/{info['default_branch']}\"\n                and isinstance(captured.get(\"object\"), dict)\n                and cast(dict[str, object], captured[\"object\"]).get(\"type\") == \"commit\"\n                and cast(dict[str, object], captured[\"object\"]).get(\"sha\") == run[\"head_sha\"],\n                \"Default branch moved before source snapshot\",\n            )\n\n        def confirm_automatic_source() -> None:\n            rc.require(rc.repository_info(gh)[\"default_branch\"] == info[\"default_branch\"],\n                       \"Automatic publication default branch changed; draft left unpublished\")\n            rc.require(gh.api(source_path) == source_ref[1],\n                       \"Automatic publication default source changed; draft left unpublished\")\n\n        consumed = begin_publication(gh, plan, cast(int, run[\"id\"]), parent)\n"
        },
        {
            "before": "            confirm_create=confirm_create,\n        )\n",
            "after": "            confirm_create=confirm_create,\n            confirm_publish=confirm_automatic_source if automatic else None,\n        )\n"
        }
    ],
    "original_to_current": {
        "978783bc3465c174f45669fe38568337d8c886161749f8d1c050ab67d8999e9d": "19cb27817c0aaf75867cdfba01a72dacb1026f71773bf42acb6527fc8553cb42"
    },
    "current": [
        "19cb27817c0aaf75867cdfba01a72dacb1026f71773bf42acb6527fc8553cb42"
    ]
}


# Preserve live execution proof only when the exact later layer is already present.
EXECUTION_JOB = {
    "hunks": [
        {
            "before": "\n\ndef context(\n    gh: rc.GitHub, channel: str, gate: bool = False\n) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:\n    \"\"\"Reuse the original publisher's execution and source-policy checks.\"\"\"\n    run_id = rc.positive(os.environ.get(\"GITHUB_RUN_ID\"), \"run ID\")\n    attempt = rc.positive(os.environ.get(\"GITHUB_RUN_ATTEMPT\"), \"run attempt\")\n    info = rc.repository_info(gh)\n    run = rc.wait_for_executing_run(\n        gh, run_id, channel, info, rc.checked_out_sha(), attempt, gate=gate\n    )\n    snapshot = rc.source_policy_snapshot(gh, cast(str, run[\"head_sha\"]))\n    rc.require_release_policy(\n",
            "after": "\n\ndef context(\n    gh: rc.GitHub, channel: str, gate: bool = False,\n    execution_proof: list[rc.ExecutionJobProof] | None = None,\n) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:\n    \"\"\"Reuse the original publisher's execution and source-policy checks.\"\"\"\n    run_id = rc.positive(os.environ.get(\"GITHUB_RUN_ID\"), \"run ID\")\n    attempt = rc.positive(os.environ.get(\"GITHUB_RUN_ATTEMPT\"), \"run attempt\")\n    info = rc.repository_info(gh)\n    run = rc.wait_for_executing_run(\n        gh, run_id, channel, info, rc.checked_out_sha(), attempt, gate=gate,\n        execution_proof=execution_proof,\n    )\n    snapshot = rc.source_policy_snapshot(gh, cast(str, run[\"head_sha\"]))\n    rc.require_release_policy(\n"
        },
        {
            "before": "\ndef create_confirmation(\n    run: dict[str, object], source_ref: tuple[str, object], consumed: bytes,\n) -> Callable[[rc.CreateReadback], None]:\n    \"\"\"Bind one release-create call, without adding any cross-run recovery state.\"\"\"\n\n    def confirm(readback: rc.CreateReadback) -> None:\n        current = readback.json(f\"actions/runs/{run['id']}/attempts/{run['run_attempt']}\")\n        rc.require(isinstance(current, dict) and current.get(\"status\") == \"in_progress\"\n                   and current.get(\"conclusion\") is None\n                   and all(type(current.get(key)) is type(run[key]) and current[key] == run[key] for key in\n                           (\"id\", \"run_attempt\", \"head_sha\", \"path\", \"event\")),\n                   \"Create readback run identity changed\")\n        info = readback.json(\"\")\n        rc.require(isinstance(info, dict) and isinstance(info.get(\"default_branch\"), str)\n                   and f\"git/ref/heads/{quote(info['default_branch'], safe='')}\" == source_ref[0],\n",
            "after": "\ndef create_confirmation(\n    run: dict[str, object], source_ref: tuple[str, object], consumed: bytes,\n    execution_proof: rc.ExecutionJobProof | None = None,\n) -> Callable[[rc.CreateReadback], None]:\n    \"\"\"Bind one release-create call, without adding any cross-run recovery state.\"\"\"\n\n    def confirm(readback: rc.CreateReadback) -> None:\n        current = readback.json(f\"actions/runs/{run['id']}/attempts/{run['run_attempt']}\")\n        rc.require(isinstance(current, dict)\n                   and current.get(\"conclusion\") is None\n                   and all(type(current.get(key)) is type(run[key]) and current[key] == run[key] for key in\n                           (\"id\", \"run_attempt\", \"head_sha\", \"path\", \"event\")),\n                   \"Create readback run identity changed\")\n        current = rc.require_active_attempt(current, run)\n        if current.get(\"status\") != \"in_progress\":\n            rc.require(type(execution_proof) is rc.ExecutionJobProof,\n                       \"Create readback lacks a local executing-job proof\")\n            proof = cast(rc.ExecutionJobProof, execution_proof)\n            rc.require(type(proof.job_id) is int and proof.job_id > 0\n                       and type(proof.check_run_id) is int and proof.check_run_id > 0,\n                       \"Invalid local executing-job proof\")\n            rc.validate_run_provenance(\n                readback, current, {\"default_branch\": run[\"head_branch\"]},\n                cast(str, run[\"head_sha\"]), cast(int, run[\"run_attempt\"]),\n            )\n            rc.require(current.get(\"run_started_at\") == run.get(\"run_started_at\"),\n                       \"Create readback attempt start changed\")\n            job = readback.json(f\"actions/jobs/{proof.job_id}\")\n            observed = rc.require_executing_job(job, current, readback.repo, proof.check_run_id)\n            rc.require(observed == proof, \"Create readback executing-job identity changed\")\n        info = readback.json(\"\")\n        rc.require(isinstance(info, dict) and isinstance(info.get(\"default_branch\"), str)\n                   and f\"git/ref/heads/{quote(info['default_branch'], safe='')}\" == source_ref[0],\n"
        },
        {
            "before": "    plan = version_plan.validate_plan(json.loads(PLAN.read_text(encoding=\"utf-8\")))\n    channel = cast(str, plan[\"channel\"])\n    gh = StateGitHub(args.repo)\n    info, run, snapshot = context(gh, channel, gate=True)\n    policy = cast(dict[str, object], snapshot[\"data\"])\n    version_plan.validate_plan(plan, policy, cast(str, run[\"head_sha\"]))\n    parent = None\n",
            "after": "    plan = version_plan.validate_plan(json.loads(PLAN.read_text(encoding=\"utf-8\")))\n    channel = cast(str, plan[\"channel\"])\n    gh = StateGitHub(args.repo)\n    execution_proof: list[rc.ExecutionJobProof] = []\n    info, run, snapshot = context(gh, channel, gate=True, execution_proof=execution_proof)\n    policy = cast(dict[str, object], snapshot[\"data\"])\n    version_plan.validate_plan(plan, policy, cast(str, run[\"head_sha\"]))\n    parent = None\n"
        },
        {
            "before": "                       \"Automatic publication default source changed; draft left unpublished\")\n\n        consumed = begin_publication(gh, plan, cast(int, run[\"id\"]), parent)\n        confirm_create = create_confirmation(run, source_ref, consumed)\n        result = rc._publish_prepared(  # pylint: disable=protected-access\n            gh,\n            cast(str, plan[\"tag\"]),\n",
            "after": "                       \"Automatic publication default source changed; draft left unpublished\")\n\n        consumed = begin_publication(gh, plan, cast(int, run[\"id\"]), parent)\n        confirm_create = create_confirmation(\n            run, source_ref, consumed, execution_proof[0] if execution_proof else None\n        )\n        result = rc._publish_prepared(  # pylint: disable=protected-access\n            gh,\n            cast(str, plan[\"tag\"]),\n"
        }
    ],
    "original_to_current": {
        "19cb27817c0aaf75867cdfba01a72dacb1026f71773bf42acb6527fc8553cb42": "673f336995440c7b1c616322be470945e8d645a5144b102f686e037d6526564d"
    },
    "current": [
        "673f336995440c7b1c616322be470945e8d645a5144b102f686e037d6526564d"
    ]
}


# Preserve exact automatic test-push admission without installing it in inventory-only mode.
AUTOMATIC_TEST_PUSH = {
    "hunks": [
        {
            "before": "    run: dict[str, object],\n    snapshot: dict[str, object],\n    base: str,\n) -> str:\n    \"\"\"Find proved same-input publication after fresh scheduled checks and builds.\"\"\"\n    head_path = \"git/ref/heads/\" + quote(cast(str, info[\"default_branch\"]), safe=\"\")\n    if cast(\n        dict[str, object], cast(dict[str, object], gh.api(head_path)).get(\"object\", {})\n",
            "after": "    run: dict[str, object],\n    snapshot: dict[str, object],\n    base: str,\n    *, source_sha: str | None = None,\n) -> str:\n    \"\"\"Prove a publication while guarding the current run against main movement.\"\"\"\n    source_sha = source_sha or cast(str, run[\"head_sha\"])\n    head_path = \"git/ref/heads/\" + quote(cast(str, info[\"default_branch\"]), safe=\"\")\n    if cast(\n        dict[str, object], cast(dict[str, object], gh.api(head_path)).get(\"object\", {})\n"
        },
        {
            "before": "    records = [\n        (rc.positive(key, \"reserved run\"), record[\"plan\"])\n        for key, record in ledger[\"plans\"].items()\n        if record[\"plan\"][\"source_sha\"] == cast(str, run[\"head_sha\"])\n        and record[\"plan\"][\"base_version\"] == base\n        and record[\"plan\"][\"policy_sha256\"]\n        == version_plan.policy_digest(snapshot[\"data\"])\n",
            "after": "    records = [\n        (rc.positive(key, \"reserved run\"), record[\"plan\"])\n        for key, record in ledger[\"plans\"].items()\n        if record[\"plan\"][\"source_sha\"] == source_sha\n        and record[\"plan\"][\"base_version\"] == base\n        and record[\"plan\"][\"policy_sha256\"]\n        == version_plan.policy_digest(snapshot[\"data\"])\n"
        },
        {
            "before": "        f\"Validation: https://github.com/{repository}/actions/runs/{run_id}\\n\\n\"\n        f\"See `{rc.MANIFEST}` for package hashes and version input evidence.\"\n    )\n\n\ndef prepare(args: argparse.Namespace) -> dict[str, object]:\n",
            "after": "        f\"Validation: https://github.com/{repository}/actions/runs/{run_id}\\n\\n\"\n        f\"See `{rc.MANIFEST}` for package hashes and version input evidence.\"\n    )\n\n\ndef automatic_test_push(\n    gh: rc.GitHub, info: dict[str, object], run: dict[str, object],\n    snapshot: dict[str, object], base: str,\n) -> dict[str, object] | None:\n    \"\"\"Skip only after proving the exact predecessor was already fully delivered.\"\"\"\n    policy = cast(dict[str, object], snapshot[\"data\"])\n    selected = cast(dict[str, object], policy.get(\"change_scope\", {})).get(\"automatic_release_test_paths\")\n    if os.environ.get(\"GITHUB_EVENT_NAME\") != \"push\" or not selected:\n        return None\n    if __package__:\n        from . import release_push_scope  # pylint: disable=import-outside-toplevel\n    else:\n        import release_push_scope  # pylint: disable=import-outside-toplevel\n    admission = release_push_scope.current_push(Path.cwd(), selected, info[\"default_branch\"])\n    if not admission[\"test_only\"] or admission[\"after\"] != run[\"head_sha\"]:\n        return None\n    try:\n        tag = scheduled_reuse(gh, info, run, snapshot, base, source_sha=admission[\"before\"])\n    except (rc.ReleaseError, ValueError, KeyError, TypeError, OSError):\n        # An unavailable/expired/pending proof must not suppress delivery.\n        return None\n    if not tag:\n        return None\n    admission.update(status=\"test-only-already-published\", published_tag=tag)\n    Path(\".release-push-admission.json\").write_bytes(rc.json_bytes(admission))\n    return {\"channel\": \"beta\", \"version\": base, \"build\": \"false\",\n            \"plan_artifact\": \"\", \"status\": admission[\"status\"]}\n\n\ndef prepare(args: argparse.Namespace) -> dict[str, object]:\n"
        },
        {
            "before": "    else:\n        base = client.resolve_version(policy, cast(str, inputs.get(\"version\", \"\")))\n    version_plan.check_base_versions(Path.cwd(), policy, base)\n    closed = rc.closed_push_cycle(gh, base, cast(str, kind))\n    if closed:\n        return closed\n",
            "after": "    else:\n        base = client.resolve_version(policy, cast(str, inputs.get(\"version\", \"\")))\n    version_plan.check_base_versions(Path.cwd(), policy, base)\n    if admitted := automatic_test_push(gh, info, run, snapshot, base):\n        return admitted\n    closed = rc.closed_push_cycle(gh, base, cast(str, kind))\n    if closed:\n        return closed\n"
        }
    ],
    "original_to_current": {
        "673f336995440c7b1c616322be470945e8d645a5144b102f686e037d6526564d": "b8eea2626f3cb7b850d0cfa2914bc75c069b3552958cc4eead84abc8c177de10"
    },
    "current": [
        "b8eea2626f3cb7b850d0cfa2914bc75c069b3552958cc4eead84abc8c177de10"
    ]
}


# Preserve compact readers only when this exact later layer is already installed.
COMPACT_DISTRIBUTION = {
    "hunks": [
        {
            "before": "import sys\nimport tempfile\nfrom collections.abc import Callable, Iterator\nfrom datetime import datetime, timezone\nfrom pathlib import Path\nfrom typing import cast\n",
            "after": "import sys\nimport tempfile\nfrom collections.abc import Callable, Iterator\nfrom contextlib import nullcontext\nfrom datetime import datetime, timezone\nfrom pathlib import Path\nfrom typing import cast\n"
        },
        {
            "before": "\n# Keep source, workflow and byte verification together at the trust boundary.\n# pylint: disable-next=too-many-locals\ndef verified_rc(\n    gh: rc.GitHub, tag: object, info: dict[str, object], current_run: dict[str, object]\n) -> tuple[dict[str, object], dict[str, object]]:\n    \"\"\"Verify the exact accepted RC source, immutable evidence and payload bytes.\"\"\"\n    rc.require(\n        isinstance(tag, str)\n",
            "after": "\n# Keep source, workflow and byte verification together at the trust boundary.\n# pylint: disable-next=too-many-locals\ndef _read_verified_rc(\n    gh: rc.GitHub, tag: object, info: dict[str, object],\n    current_run: dict[str, object] | None, stage: Path | None,\n) -> tuple[dict[str, object], bytes, dict[str, object]]:\n    \"\"\"Verify the exact accepted RC source, immutable evidence and payload bytes.\"\"\"\n    rc.require(\n        isinstance(tag, str)\n"
        },
        {
            "before": "    initial = rc.release_snapshot(gh, tag)\n    ref, _, assets = initial\n    manifests = [asset for asset in assets if asset[\"name\"] == rc.MANIFEST]\n    rc.require(len(manifests) == 1, \"RC manifest is missing\")\n    raw = gh.binary(\n        f\"releases/assets/{rc.positive(manifests[0]['id'], 'manifest asset ID')}\"\n    )\n    manifest = rc.validate_manifest(raw, gh.repo, tag)\n    source_policy = rc.source_policy_snapshot(gh, cast(str, manifest[\"source_sha\"]))\n    rc.require(source_policy == manifest[\"source_policy\"], \"RC source policy changed\")\n    rc.require_current_fuzzing(gh, cast(dict[str, object], source_policy[\"data\"]), cast(str, current_run[\"head_sha\"]))\n    rc.require(\n        cast(dict[str, object], ref[\"object\"]).get(\"sha\")\n        == cast(str, manifest[\"source_sha\"]),\n        \"RC source tag mismatch\",\n    )\n    rc.require(\n        manifest[\"run_id\"] != current_run[\"id\"], \"RC must come from a separate run\"\n    )\n    source_run = cast(dict[str, object], gh.api(f\"actions/runs/{manifest['run_id']}\"))\n    rc.validate_validation_run(gh, source_run, cast(dict[str, object], source_policy[\"data\"]), \"rc\")\n    rc.require(\n",
            "after": "    initial = rc.release_snapshot(gh, tag)\n    ref, _, assets = initial\n    manifests = [asset for asset in assets if asset[\"name\"] == rc.MANIFEST]\n    rc.require(len(manifests) == 1 and 0 < cast(int, manifests[0].get(\"size\", 2_000_001)) <= 2_000_000,\n               \"RC manifest is missing or oversized\")\n    raw = gh.binary(\n        f\"releases/assets/{rc.positive(manifests[0]['id'], 'manifest asset ID')}\"\n    )\n    manifest = rc.validate_manifest(raw, gh.repo, tag)\n    source_policy = rc.source_policy_snapshot(gh, cast(str, manifest[\"source_sha\"]))\n    rc.require(source_policy == manifest[\"source_policy\"], \"RC source policy changed\")\n    rc.require_release_policy(cast(dict[str, object], source_policy[\"data\"]), gh.repo, qualified=True)\n    if current_run is not None:\n        rc.require_current_fuzzing(gh, cast(dict[str, object], source_policy[\"data\"]),\n                                   cast(str, current_run[\"head_sha\"]))\n    rc.require(\n        cast(dict[str, object], ref[\"object\"]).get(\"sha\")\n        == cast(str, manifest[\"source_sha\"]),\n        \"RC source tag mismatch\",\n    )\n    if current_run is not None:\n        rc.require(manifest[\"run_id\"] != current_run[\"id\"], \"RC must come from a separate run\")\n    source_run = cast(dict[str, object], gh.api(f\"actions/runs/{manifest['run_id']}\"))\n    rc.validate_validation_run(gh, source_run, cast(dict[str, object], source_policy[\"data\"]), \"rc\")\n    rc.require(\n"
        },
        {
            "before": "        \"RC asset inventory differs from immutable evidence\",\n    )\n    rc.require(manifests[0][\"size\"] == len(raw), \"RC asset size mismatch\")\n    with tempfile.TemporaryDirectory(prefix=\"verified-rc-\") as temp:\n        payload = Path(temp) / \"payload\"\n        budget = rc.DownloadBudget(sum(cast(int, item[\"size\"]) for item in expected.values()))\n        for item in assets:\n            if item[\"name\"] == rc.MANIFEST:\n                continue\n            identity = rc.download_asset(\n                gh, item[\"id\"], payload,\n                expected=cast(rc.AssetIdentity, {\n                    key: expected[item[\"name\"]][key] for key in (\"size\", \"sha256\")\n                }), budget=budget,\n            )\n            payload.unlink()\n            rc.require(item[\"size\"] == identity[\"size\"], \"RC asset size mismatch\")\n            declaration = expected[item[\"name\"]]\n            rc.require(\n",
            "after": "        \"RC asset inventory differs from immutable evidence\",\n    )\n    rc.require(manifests[0][\"size\"] == len(raw), \"RC asset size mismatch\")\n    if stage is not None:\n        rc.require(stage.is_dir() and not stage.is_symlink() and not any(stage.iterdir()),\n                   \"RC staging must be an empty owned directory\")\n    retained = stage is not None or bool(manifest.get(\"version_plan\"))\n    with (nullcontext(str(stage)) if stage is not None else\n          tempfile.TemporaryDirectory(prefix=\"verified-rc-\")) as temp:\n        directory = Path(temp)\n        budget = rc.DownloadBudget(sum(cast(int, item[\"size\"]) for item in expected.values()))\n        for item in assets:\n            if item[\"name\"] == rc.MANIFEST:\n                continue\n            payload = directory / (cast(str, item[\"name\"]) if retained else \"payload\")\n            identity = rc.download_asset(\n                gh, item[\"id\"], payload,\n                expected=cast(rc.AssetIdentity, {\n                    key: expected[item[\"name\"]][key] for key in (\"size\", \"sha256\")\n                }), budget=budget,\n            )\n            if not retained:\n                payload.unlink()\n            rc.require(item[\"size\"] == identity[\"size\"], \"RC asset size mismatch\")\n            declaration = expected[item[\"name\"]]\n            rc.require(\n"
        },
        {
            "before": "                and identity[\"sha256\"] == declaration[\"sha256\"],\n                f\"RC payload checksum mismatch: {item['name']}\",\n            )\n    rc.require(\n        rc.snapshot_identity(rc.release_snapshot(gh, tag))\n        == rc.snapshot_identity(initial),\n        \"RC changed during verification\",\n    )\n    return manifest, {\n        \"tag\": tag,\n        \"manifest_sha256\": rc.digest(raw),\n        \"source_sha\": cast(str, manifest[\"source_sha\"]),\n        \"run_id\": manifest[\"run_id\"],\n    }\n\n\ndef event_inputs() -> dict[str, object]:\n",
            "after": "                and identity[\"sha256\"] == declaration[\"sha256\"],\n                f\"RC payload checksum mismatch: {item['name']}\",\n            )\n        if manifest.get(\"version_plan\"):\n            policy = cast(dict[str, object], source_policy[\"data\"])\n            plan = cast(dict[str, object], manifest[\"version_plan\"])\n            receipts = verify_receipts(\n                directory, plan, cast(list[dict[str, object]], manifest[\"assets\"]), policy,\n                producer_identity(gh.repo, cast(int, manifest[\"run_id\"]),\n                                  cast(int, manifest[\"run_attempt\"]), cast(str, manifest[\"source_sha\"])))\n            build, validation = candidate_fuzz_receipts(policy, \"rc\", receipts)\n            verify_required_build_receipts(build, policy)\n            rc.require(manifest.get(\"build_receipts\") == [\n                {\"name\": value[\"name\"], \"sha256\": value[\"sha256\"]} for value in build\n            ], \"RC build receipt evidence differs\")\n            if \"fuzzing\" in policy:\n                rc.require(manifest.get(\"validation_receipts\") == [\n                    {\"name\": value[\"name\"], \"sha256\": value[\"sha256\"]} for value in validation\n                ], \"RC validation receipt evidence differs\")\n            rc.require(verify_declared_artifacts(directory, policy, plan) == manifest.get(\"package_versions\"),\n                       \"RC package metadata differs\")\n            if policy.get(\"sign_releases\"):\n                # Authenticate with the retained source policy, not a newer driver.\n                if __package__:\n                    from .release_signing import verify\n                else:\n                    from release_signing import verify\n                verify(directory, policy, cast(str, manifest[\"source_sha\"]))\n            else:\n                rc.verify_required_signatures(directory, gh.repo, cast(str, manifest[\"source_sha\"]))\n    latest = cast(dict[str, object], gh.api(f\"actions/runs/{manifest['run_id']}\"))\n    rc.require(latest.get(\"id\") == manifest[\"run_id\"], \"RC source run identity changed\")\n    rc.validate_validation_run(gh, latest, cast(dict[str, object], source_policy[\"data\"]), \"rc\")\n    rc.validate_run(gh, latest, info, cast(str, manifest[\"source_sha\"]),\n                    cast(int, manifest[\"run_attempt\"]), completed=True)\n    rc.require(\n        rc.snapshot_identity(rc.release_snapshot(gh, tag))\n        == rc.snapshot_identity(initial),\n        \"RC changed during verification\",\n    )\n    return manifest, raw, {\n        \"tag\": tag,\n        \"manifest_sha256\": rc.digest(raw),\n        \"source_sha\": cast(str, manifest[\"source_sha\"]),\n        \"run_id\": manifest[\"run_id\"],\n        \"run_attempt\": manifest[\"run_attempt\"],\n    }\n\n\ndef read_verified_rc(\n    gh: rc.GitHub, tag: object, info: dict[str, object] | None = None,\n    *, stage: Path | None = None,\n) -> tuple[dict[str, object], bytes, dict[str, object]]:\n    \"\"\"Authenticate a full retained RC without inventing a dispatch/current run.\n\n    An optional empty caller-owned stage retains every RC payload, including\n    receipts omitted by compact distributions. Without it temporary bytes expire.\n    \"\"\"\n    return _read_verified_rc(gh, tag, info or rc.repository_info(gh), None, stage)\n\n\ndef verified_rc(\n    gh: rc.GitHub, tag: object, info: dict[str, object], current_run: dict[str, object]\n) -> tuple[dict[str, object], dict[str, object]]:\n    \"\"\"Keep dispatch-only separation/fuzz checks and the legacy parent schema.\"\"\"\n    manifest, _, proof = _read_verified_rc(gh, tag, info, current_run, None)\n    return manifest, {key: value for key, value in proof.items() if key != \"run_attempt\"}\n\n\n# Keep the outer stable release and authenticated retained RC in one read scope.\n# pylint: disable-next=too-many-locals\ndef read_verified_distribution(\n    gh: rc.GitHub, tag: str, raw: bytes, info: dict[str, object] | None = None,\n    *, stage: Path | None = None,\n) -> tuple[dict[str, object], bytes, dict[str, object]]:\n    \"\"\"Resolve a compact stable envelope and verify its exact shipped bytes.\"\"\"\n    # Legacy generated consumers need not import this optional module.\n    # pylint: disable-next=import-outside-toplevel\n    if __package__:\n        from .release_distribution import parse_distribution, verify_distribution\n    else:\n        from release_distribution import parse_distribution, verify_distribution\n\n    compact = parse_distribution(raw, gh.repo, tag)\n    rc.require(not rc.RELEASE_SIGNATURES_REQUIRED,\n               \"Compact stable cannot omit full-inventory signature evidence\")\n    info = info or rc.repository_info(gh)\n    pointer = compact[\"rc_evidence\"]\n    original = rc.release_snapshot(gh, pointer[\"tag\"])\n    manifest, candidate_raw, proof = read_verified_rc(gh, pointer[\"tag\"], info)\n    rc.require(not cast(dict[str, object], cast(dict[str, object], manifest[\"source_policy\"])[\"data\"]).get(\"sign_releases\"),\n               \"Compact stable cannot omit full-inventory signature evidence\")\n    verify_distribution(compact, manifest, candidate_raw)\n    plan = cast(dict[str, object], manifest[\"version_plan\"])\n    policy = cast(dict[str, object], manifest[\"source_policy\"])\n    source, run_id = cast(str, manifest[\"source_sha\"]), cast(int, manifest[\"run_id\"])\n    candidate_tag = cast(str, manifest[\"tag\"])\n    verify_scheduled_reuse(gh, info, policy, plan, run_id, candidate_tag)\n    initial = published_plan_snapshot(gh, tag, source, False)\n    expected = {item[\"name\"]: item for item in compact[\"assets\"]}\n    declarations = {**expected, rc.MANIFEST: {\"size\": len(raw), \"sha256\": rc.digest(raw)}}\n    inventory = {asset[\"name\"]: asset for asset in initial[2]}\n    rc.require(set(inventory) == set(declarations), \"Compact stable asset inventory mismatch\")\n    rc.require(all(asset.get(\"size\") == declarations[name][\"size\"]\n                   and asset.get(\"digest\") == \"sha256:\" + declarations[name][\"sha256\"]\n                   for name, asset in inventory.items()), \"Compact stable asset metadata mismatch\")\n    if stage is not None:\n        rc.require(stage.is_dir() and not stage.is_symlink() and not any(stage.iterdir()),\n                   \"Stable staging must be an empty owned directory\")\n    with (nullcontext(str(stage)) if stage is not None else\n          tempfile.TemporaryDirectory(prefix=\"verified-distribution-\")) as temporary:\n        directory = Path(temporary)\n        budget = rc.DownloadBudget(sum(item[\"size\"] for item in expected.values()))\n        for name, item in expected.items():\n            identity = rc.download_asset(\n                gh, inventory[name][\"id\"], directory / name,\n                expected=cast(rc.AssetIdentity, {key: item[key] for key in (\"size\", \"sha256\")}), budget=budget,\n            )\n            rc.require(identity == {key: item[key] for key in (\"size\", \"sha256\")},\n                       \"Compact stable payload changed\")\n        verify_scheduled_reuse(gh, info, policy, plan, run_id, candidate_tag)\n        rc.require(rc.snapshot_identity(original) == rc.snapshot_identity(\n            rc.release_snapshot(gh, pointer[\"tag\"])), \"Retained RC changed during stable transfer\")\n        rc.require(rc.snapshot_identity(initial) == rc.snapshot_identity(\n            published_plan_snapshot(gh, tag, source, False)),\n            \"Compact stable release changed during transfer\")\n    return manifest, candidate_raw, proof\n\n\ndef event_inputs() -> dict[str, object]:\n"
        },
        {
            "before": "    rc.require(cast(int, asset[\"size\"]) <= 2_000_000, \"Qualified manifest is too large\")\n    raw = gh.binary(f\"releases/assets/{rc.positive(asset['id'], 'manifest ID')}\")\n    manifest = rc.parse_json(raw, \"qualified manifest\")\n    rc.require(\n        isinstance(manifest, dict)\n        and isinstance(manifest.get(\"schema\"), int)\n",
            "after": "    rc.require(cast(int, asset[\"size\"]) <= 2_000_000, \"Qualified manifest is too large\")\n    raw = gh.binary(f\"releases/assets/{rc.positive(asset['id'], 'manifest ID')}\")\n    manifest = rc.parse_json(raw, \"qualified manifest\")\n    if isinstance(manifest, dict) and type(manifest.get(\"schema\")) is int and manifest[\"schema\"] == 2:\n        candidate, _, _ = read_verified_distribution(gh, tag, raw, info)\n        rc.require(candidate[\"version_plan\"] == plan and candidate[\"source_policy\"] == snapshot\n                   and candidate[\"run_id\"] == source_run_id,\n                   \"Compact stable differs from its qualified RC plan\")\n        return\n    rc.require(\n        isinstance(manifest, dict)\n        and isinstance(manifest.get(\"schema\"), int)\n"
        }
    ],
    "original_to_current": {
        "b8eea2626f3cb7b850d0cfa2914bc75c069b3552958cc4eead84abc8c177de10": "999072e1cec48cc463ea34045b2983113a1e7731b459912e6d37b1f600f6224c"
    },
    "current": [
        "999072e1cec48cc463ea34045b2983113a1e7731b459912e6d37b1f600f6224c"
    ]
}


# Preserve the later dispatch-wrapper compatibility fix only when already installed.
LEGACY_RC_COMPATIBILITY = {
    "hunks": [
        {
            "before": "    if stage is not None:\n        rc.require(stage.is_dir() and not stage.is_symlink() and not any(stage.iterdir()),\n                   \"RC staging must be an empty owned directory\")\n    retained = stage is not None or bool(manifest.get(\"version_plan\"))\n    with (nullcontext(str(stage)) if stage is not None else\n          tempfile.TemporaryDirectory(prefix=\"verified-rc-\")) as temp:\n        directory = Path(temp)\n",
            "after": "    if stage is not None:\n        rc.require(stage.is_dir() and not stage.is_symlink() and not any(stage.iterdir()),\n                   \"RC staging must be an empty owned directory\")\n    # The dispatch wrapper must keep accepting RC receipts from before producer\n    # identities were introduced. Only the new read-only resolver revalidates\n    # their complete semantic contract; the wrapper streams authenticated bytes.\n    retained = stage is not None or (current_run is None and bool(manifest.get(\"version_plan\")))\n    with (nullcontext(str(stage)) if stage is not None else\n          tempfile.TemporaryDirectory(prefix=\"verified-rc-\")) as temp:\n        directory = Path(temp)\n"
        },
        {
            "before": "                and identity[\"sha256\"] == declaration[\"sha256\"],\n                f\"RC payload checksum mismatch: {item['name']}\",\n            )\n        if manifest.get(\"version_plan\"):\n            policy = cast(dict[str, object], source_policy[\"data\"])\n            plan = cast(dict[str, object], manifest[\"version_plan\"])\n            receipts = verify_receipts(\n",
            "after": "                and identity[\"sha256\"] == declaration[\"sha256\"],\n                f\"RC payload checksum mismatch: {item['name']}\",\n            )\n        if current_run is None and manifest.get(\"version_plan\"):\n            policy = cast(dict[str, object], source_policy[\"data\"])\n            plan = cast(dict[str, object], manifest[\"version_plan\"])\n            receipts = verify_receipts(\n"
        }
    ],
    "original_to_current": {
        "999072e1cec48cc463ea34045b2983113a1e7731b459912e6d37b1f600f6224c": "9d8957d25de18740c016da30774a47b7e2dc934a4d2d7bde96c4cdf8017908d8"
    },
    "current": [
        "9d8957d25de18740c016da30774a47b7e2dc934a4d2d7bde96c4cdf8017908d8"
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


def _before_legacy_rc_compatibility(source: str) -> str:
    """Reverse only the exact dispatch-wrapper compatibility correction."""
    return _before_layer(source, LEGACY_RC_COMPATIBILITY, "Legacy RC compatibility")


def _before_compact_distribution(source: str) -> str:
    """Reverse only the exact compact reader layer; never accept partial shapes."""
    return _before_layer(_before_legacy_rc_compatibility(source), COMPACT_DISTRIBUTION,
                         "Compact distribution")


def _before_automatic_test_push(source: str) -> str:
    """Unwrap compact readers before the accepted automatic test-push layer."""
    return _before_layer(_before_compact_distribution(source), AUTOMATIC_TEST_PUSH,
                         "Automatic test push")


def _before_execution_job(source: str) -> str:
    """Reverse automatic test-push admission before the live execution-proof layer."""
    return _before_layer(_before_automatic_test_push(source), EXECUTION_JOB, "Execution job")


def _before_automatic_source(source: str) -> str:
    """Reverse live execution proof before the automatic-source layer."""
    return _before_layer(_before_execution_job(source), AUTOMATIC_SOURCE, "Automatic source")


def _before_create_identity(source: str) -> str:
    """Reverse only the exact later default-branch identity guard."""
    return _before_layer(_before_automatic_source(source), CREATE_IDENTITY, "Create identity")


def _before_create_readback(source: str) -> str:
    """Return the exact notes-only publisher when the new layers are recognized."""
    return _before_layer(_before_create_identity(source), CREATE_READBACK, "Create readback")


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
                if _before_create_identity(source) != source:
                    _, result = receipts.transform(result, name, CREATE_IDENTITY)
                    if _before_automatic_source(source) != source:
                        _, result = receipts.transform(result, name, AUTOMATIC_SOURCE)
                        if _before_execution_job(source) != source:
                            _, result = receipts.transform(result, name, EXECUTION_JOB)
            if _before_automatic_test_push(source) != source:
                _, result = receipts.transform(result, name, AUTOMATIC_TEST_PUSH)
            if _before_compact_distribution(source) != source:
                _, result = receipts.transform(result, name, COMPACT_DISTRIBUTION)
            if _before_legacy_rc_compatibility(source) != source:
                _, result = receipts.transform(result, name, LEGACY_RC_COMPATIBILITY)
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
