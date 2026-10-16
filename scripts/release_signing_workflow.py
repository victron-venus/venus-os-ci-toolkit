"""Opt-in signing for versioned release pipelines, isolated from build credentials."""

from __future__ import annotations

import fnmatch

from release_signing import trust

COSIGN_SOURCE = "11926fa5bbbbde47e88fc006b625a17769b743b2"
ARTIFACT = "release-assets-signature"


def enabled(policy: dict) -> bool:
    """Reject ambiguous opt-ins or unsupported publication contracts."""
    value = policy.get("sign_releases", False)
    if not isinstance(value, bool):
        raise ValueError("sign_releases must be a boolean")
    if value and (
        not policy.get("versioning")
        or policy.get("mode", "release") != "release"
        or policy.get("visibility", "public") != "public"
        or not fnmatch.fnmatchcase(ARTIFACT, policy.get("artifact_pattern", "release-assets*"))
    ):
        raise ValueError("Signing requires public versioned releases and matching artifact_pattern")
    if value:
        trust(policy, "0" * 40)
    return value


def installer(pins: dict) -> list[dict]:
    """All signer/verifier jobs use identical source, dependency and TLS policy."""
    return [
        {"uses": pins["actions/checkout"], "with": {
            "repository": "sigstore/cosign", "ref": COSIGN_SOURCE,
            "path": ".release-cosign-source", "persist-credentials": False}},
        {"uses": pins["actions/setup-go"], "with": {
            "go-version": "1.27.2", "cache-dependency-path": ".github/cosign/go.sum"}},
        {"name": "Build the reviewed patched release signer", "run": (
            'python3 scripts/build_release_signer.py --source .release-cosign-source '
            '--output "$RUNNER_TEMP/release-signer/cosign"\n'
            'echo "$RUNNER_TEMP/release-signer" >> "$GITHUB_PATH"\n'
            'echo "GODEBUG=fips140=on" >> "$GITHUB_ENV"\n')},
    ]


def apply(workflow: dict, policy: dict, pins: dict) -> None:
    """Add a hosted signer after validation, and fail closed before publication."""
    if not enabled(policy):
        return
    jobs = workflow["jobs"]
    repository = policy["repository"]
    branch = policy.get("default_branch", "main")
    jobs["sign"] = {
        "name": "Sign complete release inventory",
        "needs": ["prepare", "checks", "build"],
        "if": ("${{ needs.prepare.outputs.build == 'true' && "
               f"github.repository == '{repository}' && github.ref == 'refs/heads/{branch}'" + " }}"),
        "runs-on": "ubuntu-latest",
        "timeout-minutes": 20,
        "permissions": {"contents": "read", "actions": "read", "id-token": "write"},
        "steps": [
            {"uses": pins["actions/checkout"], "with": {"persist-credentials": False}},
            {"uses": pins["actions/setup-python"], "with": {"python-version": "3.12"}},
            *installer(pins),
            {"uses": pins["actions/download-artifact"], "with": {
                "name": "${{ needs.prepare.outputs.plan_artifact }}", "path": "."}},
            {"name": "Apply the reviewed frozen version plan", "run":
             "python3 scripts/version_plan.py sync --root . --plan .release-plan.json"},
            {"uses": pins["actions/download-artifact"], "with": {
                "pattern": policy.get("artifact_pattern", "release-assets*"),
                "path": ".release-download", "merge-multiple": False}},
            {"name": "Collect original build assets", "run": (
                "# A retried signing job must not ingest its own previous output.\n"
                "rm -rf .release-download/release-assets-signature\n"
                "python3 scripts/release.py collect .release-download .release-assets\n")},
            {"name": "Verify build receipts and inventory reviewed source", "run":
             'python3 scripts/release_signing.py prepare .release-assets --source-sha "$GITHUB_SHA"'},
            {"name": "Sign the complete source and artifact inventory", "run": (
                "cosign sign-blob --yes --bundle .release-assets/release-signatures.sigstore.json "
                ".release-assets/release-signatures.json")},
            {"name": "Verify publisher identity and every payload", "run":
             'python3 scripts/release_signing.py verify .release-assets --source-sha "$GITHUB_SHA"'},
            {"name": "Reject an incorrect source identity", "run": (
                "if python3 scripts/release_signing.py verify .release-assets "
                "--source-sha 0000000000000000000000000000000000000000; then\n"
                "  echo 'Signature accepted an incorrect source SHA' >&2\n  exit 1\nfi\n")},
            {"name": "Bind the signature and source archive to the version plan", "run": (
                "python3 scripts/stage_release_assets.py --target signature "
                "--pattern .release-assets/release-signatures.json "
                "--pattern .release-assets/release-signatures.sigstore.json "
                '--pattern ".release-assets/source-$GITHUB_SHA.tar.gz"')},
            {"uses": pins["actions/upload-artifact"], "with": {
                "name": ARTIFACT, "path": "release-assets/signature/*",
                "if-no-files-found": "error", "retention-days": 30, "overwrite": True}},
        ],
    }
    gate = jobs["gate"]
    gate["needs"].append("sign")
    source = gate["steps"][0]["run"]
    original = "('prepare', 'checks', 'build')"
    skipped = "    expected['build'] = 'skipped'"
    if source.count(original) != 1 or source.count(skipped) != 1:
        raise ValueError("Release gate structure changed; review the signing gate")
    gate["steps"][0]["run"] = source.replace(
        original, "('prepare', 'checks', 'build', 'sign')"
    ).replace(skipped, skipped + "\n    expected['sign'] = 'skipped'")
    # Verification, including old-RC promotion, needs no signing identity/token.
    for name in ("candidate", "stable", "final"):
        if name in jobs:
            jobs[name]["steps"][1:1] = installer(pins)
