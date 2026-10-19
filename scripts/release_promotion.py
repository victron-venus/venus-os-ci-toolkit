"""Bind unchanged RC bytes to the separate successful stable promotion run."""

from __future__ import annotations

import io
from pathlib import Path
import re
import zipfile

if __package__:
    from . import release_control as rc, release_versioned as lifecycle, version_plan
else:
    import release_control as rc
    import release_versioned as lifecycle
    import version_plan

ARTIFACT = "release-promotion-evidence"
FILE = "promotion.json"
MAX_BYTES = 32_768


def locator(release: dict, repository: str) -> int:
    """Treat the public release-body link only as an untrusted run locator."""
    body = release.get("body")
    rc.require(isinstance(body, str), "Stable release has no promotion locator")
    matches = re.findall(
        r"^Promotion: https://github\.com/" + re.escape(repository)
        + r"/actions/runs/([1-9]\d*)[ \t]*$", body, re.MULTILINE,
    )
    rc.require(len(matches) == 1, "Stable release has no unique promotion locator")
    return rc.positive(matches[0], "promotion run ID")


def receipt(release: dict, manifest: dict, raw: bytes, run: dict) -> dict:
    """All identities come from verified public metadata, never worker output."""
    return {
        "schema": 1, "repository": manifest["repository"], "workflow": rc.WORKFLOW,
        "release_id": str(rc.positive(release.get("id"), "stable release ID")),
        "release_tag": release["tag_name"], "rc_tag": manifest["tag"],
        "source_sha": manifest["source_sha"], "rc_manifest_sha256": rc.digest(raw),
        "source_run_id": str(rc.positive(manifest["run_id"], "source run ID")),
        "source_run_attempt": str(rc.positive(manifest["run_attempt"], "source run attempt")),
        "promotion_run_id": str(rc.positive(run.get("id"), "promotion run ID")),
        "promotion_run_attempt": str(rc.positive(run.get("run_attempt"), "promotion run attempt")),
        "workflow_source_sha": run["head_sha"],
    }


def record(gh: rc.GitHub, info: dict, run: dict, rc_tag: str, output: Path) -> dict:
    """After publication, record the exact stable release without changing it."""
    rc.require(not output.exists() and not output.is_symlink(), "Promotion receipt already exists")
    original = rc.release_snapshot(gh, rc_tag)
    declared = [item for item in original[2] if item.get("name") == rc.MANIFEST]
    rc.require(len(declared) == 1 and declared[0].get("size", 2_000_001) <= 2_000_000,
               "RC manifest is missing or oversized")
    raw = gh.binary(f"releases/assets/{rc.positive(declared[0].get('id'), 'manifest ID')}")
    rc.require(len(raw) <= 2_000_000, "RC manifest is oversized")
    manifest = rc.validate_manifest(raw, gh.repo, rc_tag)
    source = manifest["source_sha"]
    policy = rc.source_policy_snapshot(gh, source)
    rc.require(policy == manifest["source_policy"], "RC source policy changed")
    plan = version_plan.validate_plan(manifest["version_plan"], policy["data"], source)
    rc.require(plan["promotion"] == "promote-bytes", "Promotion receipt requires unchanged RC bytes")
    tag = "v" + manifest["version"]
    stable = lifecycle.published_plan_snapshot(gh, tag, source, False)
    rc.require(locator(stable[1], gh.repo) == run["id"], "Stable locator differs from this run")
    stable_manifest = [item for item in stable[2] if item["name"] == rc.MANIFEST]
    rc.require(len(stable_manifest) == 1 and stable_manifest[0].get("size", 2_000_001) <= 2_000_000,
               "Stable manifest is missing or oversized")
    stable_raw = gh.binary(f"releases/assets/{rc.positive(stable_manifest[0]['id'], 'manifest ID')}")
    rc.require(stable_raw == raw, "Stable manifest differs from the accepted RC")
    lifecycle.verify_scheduled_reuse(gh, info, policy, plan, manifest["run_id"], tag)
    rc.validate_run(gh, run, info, run["head_sha"], run["run_attempt"], False, gate=False)
    rc.require(rc.snapshot_identity(stable) == rc.snapshot_identity(
        lifecycle.published_plan_snapshot(gh, tag, source, False)), "Stable release changed during recording")
    result = receipt(stable[1], manifest, raw, run)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as stream:
        stream.write(rc.json_bytes(result))
    return result


def verify_jobs(gh: rc.GitHub, run_id: int, attempt: int, source: str) -> None:
    """Check the source-controlled closed obligation and actual publisher job."""
    policy = rc.source_policy_snapshot(gh, source)["data"]
    rc.require_release_policy(policy, gh.repo, qualified=True)
    config = policy.get("closed_build")
    rc.require(isinstance(config, dict) and type(config.get("enabled")) is bool,
               "Promotion source has no reviewed receipt contract")
    jobs = gh.pages(f"actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    expected = {"stable": "success", "closed_stable_validate": "success" if config["enabled"] else "skipped"}
    for name, conclusion in expected.items():
        matching = [job for job in jobs if job.get("name") == name or (
            name == "closed_stable_validate" and job.get("name", "").startswith(name + " / "))]
        rc.require(len(matching) == 1 and matching[0].get("status") == "completed"
                   and matching[0].get("conclusion") == conclusion and matching[0].get("head_sha") == source,
                   "Promotion has no successful exact stable validation path")


def read_receipt(gh: rc.GitHub, run_id: int, source: str) -> bytes:
    """Read a bounded immutable artifact after authenticating its run metadata."""
    artifacts = gh.pages(f"actions/runs/{run_id}/artifacts", "artifacts")
    matching = [item for item in artifacts if item.get("name") == ARTIFACT and item.get("expired") is False]
    rc.require(len(matching) == 1, "Promotion requires one retained immutable receipt")
    artifact = matching[0]
    identity = artifact.get("workflow_run", {})
    rc.require(identity.get("id") == run_id and identity.get("head_sha") == source,
               "Promotion receipt provenance mismatch")
    rc.require(0 < artifact.get("size_in_bytes", MAX_BYTES + 1) <= MAX_BYTES, "Promotion receipt archive is oversized")
    archive = gh.binary(f"actions/artifacts/{rc.positive(artifact.get('id'), 'artifact ID')}/zip")
    rc.require(len(archive) <= MAX_BYTES and artifact.get("digest") == "sha256:" + rc.digest(archive),
               "Promotion receipt digest mismatch")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            rc.require(len(entries) == 1 and entries[0].filename == FILE and entries[0].file_size <= MAX_BYTES,
                       "Promotion receipt archive has unexpected contents")
            return zipped.read(entries[0])
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise rc.ReleaseError("Invalid promotion receipt archive") from error


def verify(gh: rc.GitHub, info: dict, stable: tuple, manifest: dict, raw: bytes) -> dict:
    """Require retained exact-attempt evidence and final promotion-run success."""
    run_id = locator(stable[1], gh.repo)
    run = gh.api(f"actions/runs/{run_id}")
    rc.require(run.get("id") == run_id and run_id != manifest["run_id"], "Invalid promotion run")
    attempt = rc.positive(run.get("run_attempt"), "promotion run attempt")
    source = run.get("head_sha")
    rc.require(isinstance(source, str) and rc.SHA_RE.fullmatch(source), "Invalid promotion source")
    rc.validate_run(gh, run, info, source, attempt, True, gate=False)
    rc.require(run.get("event") == "workflow_dispatch", "Promotion must be explicitly requested")
    verify_jobs(gh, run_id, attempt, source)
    raw_receipt = read_receipt(gh, run_id, source)
    result = receipt(stable[1], manifest, raw, run)
    rc.require(raw_receipt == rc.json_bytes(result), "Promotion receipt binding mismatch")
    # A rerun started during verification invalidates the earlier success too.
    current = gh.api(f"actions/runs/{run_id}")
    rc.require(current.get("id") == run_id, "Promotion run changed")
    rc.validate_run(gh, current, info, source, attempt, True, gate=False)
    return result
