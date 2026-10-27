# CI and release operations — victron-venus/venus-os-ci-toolkit

The source of truth is `.release-policy.json`. `quality-gate.yml` runs the callable
validation workflows and produces the required **CI gate** status on every PR
and merge-queue commit. Superseded PR runs are cancelled. Without an explicit
`change_scope` policy, every change keeps full CI and normal release preparation.
Documentation-only skipping requires that explicit opt-in. When enabled, the
Change scope job checks the complete Git diff first; the gate accepts only proven
documentation skips. Missing, failed or unexpectedly skipped workflows fail the
gate. Unknown files, incomplete history, code, workflows and lockfile changes run
full validation.

The optional `change_scope` policy provides exact `documentation_paths`, exact
`required_paths` for documentation used as a build input, and
`always_validate_workflows` for independently required checks. Documentation paths
cannot exempt source, tests, fixtures, build configuration or dependencies.
Manual dispatch, scheduled runs and release qualification remain full. Only with
explicit `change_scope` opt-in does a documentation-only push stop before release
preparation, version allocation, artifact builds or publication. This does not
change the configured nightly policy.

## Local checks

Use Python 3.11+ for the CLI and the project toolchains documented in `scripts/ci.sh`.
The scripts fail on missing dependencies and do not publish anything during checks.

```bash
python3 scripts/release.py check
python3 scripts/release.py status
```

Callable validation workflows:
- `.github/workflows/ci.yml`
- `.github/workflows/security-required.yml`
- `.github/workflows/contract-tests.yml`

The [release strategy](../RELEASING.md) defines versioning, channels, acceptance,
ownership, hotfixes and rollback. This document is the operational runbook.

## Nightly, beta and RC

During rollout, checks and builds run but public candidate publication is disabled
until repository variable `RELEASE_CHANNELS_ENABLED=true`. Enable it only after
required release reviewers are configured and legacy production webhooks have
been migrated. This prevents the first nightly/beta from reaching an old auto-deploy
handler. Manual beta/RC/stable requests fail with an explicit configuration error
until enabled; build-only nightlies remain available in Actions artifacts.

Nightly runs daily at the repository's staggered UTC schedule. Default-branch
pushes request beta builds through the same validation and build gates. Publication
also requires the opt-in variable and an eligible unreleased base version. GitHub can delay
scheduled runs; schedule timing is not an SLA. A committed base version (`X.Y.Z`)
is required. Version changes go through PR review, including any native companion
version files. The frozen release plan supplies full candidate versions to declared
format adapters before compilation; the manifest binds the plan and build receipts.

From a clean checkout matching GitHub's default-branch HEAD:

```bash
python3 scripts/release.py package --version 1.2.3 --channel rc
python3 scripts/release.py nightly --dry-run
python3 scripts/release.py beta --version 1.2.3
python3 scripts/release.py rc --version 1.2.3
python3 scripts/release.py status
```

Replace the example version with the committed project version. Native multi-OS
packages require the hosted build matrix; local packaging covers only supported
local targets. These commands never stage unrelated changes, push `main`, or create
tags directly. Publication commands dispatch `release-pipeline.yml` on the default
branch; `package` builds locally, `status` reads run history, and `--dry-run` only
displays the request.

If the base version already has a stable release, bump the committed version through
a PR before beta/RC publication. Nightly builds may still use that existing base. After every fresh check and platform build passes, an automatic nightly skips duplicate GitHub publication if retained immutable evidence qualifies an existing beta/RC/stable at the same source, policy and base. It reports `reused` with that tag, retains its Actions build artifacts, and does not advance the publication floor or create new promotion evidence. Reserved counters may have gaps. Mutable build inputs and current security databases are still exercised. Manual nightly requests, new sources and missing/expired/unverifiable evidence retain full publication.
Automatic push betas skip packaging/publication with `version-required` when their
committed base already has a stable tag. Quality and security checks still run.
Prepare the next version through a PR; manual beta/RC requests still fail strictly.

Candidate tags are unique and immutable: `vX.Y.Z-beta.N`, `vX.Y.Z-rc.N`, or
`vX.Y.Z-nightly.<UTC timestamp>.<run>.<attempt>`. Candidates are prereleases and
never update stable/latest. All required platforms must build before publication.
`release-manifest.json` records source SHA, workflow/run attempt and every payload
SHA-256. The manifest is also saved in immutable Actions evidence for 90 days.

Automatic push betas and scheduled nightlies publish the current default HEAD.
An older source is explicitly marked `superseded` only after ancestry and one
newer automatic replacement run at current HEAD are verified. This does not claim
that the replacement passed or published. No tag, release or promotion evidence
is created for that skipped publication, and its reserved number is not reused.
Missing replacement evidence or API failures remain errors. Manual releases keep
their existing checks. A race after the final check can still fail publication;
inspect the error and dispatch a fresh run at current HEAD, never reset the ledger.

## Stable promotion

After testing the RC on the intended target/environment:

```bash
python3 scripts/release.py doctor
python3 scripts/release.py stable --rc v1.2.3-rc.1 --dry-run
python3 scripts/release.py stable --rc v1.2.3-rc.1
```

Approve the pending `release` environment in GitHub Actions. The publisher checks
that reviewers are configured, verifies the RC's successful run/attempt, default
branch ancestry, Release gate, immutable evidence and every payload checksum.
Stable `vX.Y.Z` copies the tested RC bytes without rebuilding. No override or
force-tag option exists. Expired/missing evidence requires a new RC. A partial
upload remains an unpublished draft; inspect it before any manual recovery.

Assets are uploaded to the draft's known release ID, using one bounded request
per file. Publication does not repeat a tag lookup for each upload: a draft may
not appear in the published-release-by-tag API. Each response must confirm a
created asset with the expected name and size; the final verification still
downloads and checks every payload before making the release public. A timeout,
incomplete response or upload conflict stops publication without retrying or
overwriting the asset. Preserve the draft and its evidence for inspection;
do not reset the version allocation ledger to reuse the failed build number.

During publication, the current Actions run and its prerequisite gate can take
time to appear consistently in the API. The publisher shares one five-minute
wait across both updates and rechecks execution identity on every read. It
publishes only after the exact Release gate reports completed success. Failed,
skipped, duplicate or mismatched gates stop immediately; validating a previously
completed release never uses this propagation wait.

GitHub releases do not deploy production. Existing push/tag/CI deployment hooks
must be migrated or disabled before enabling automatic prereleases. Container
and PyPI publication use verified stable assets as a separate explicit operation.

## Qualified GitHub CLI

Release jobs install and check the exact native GitHub CLI before GitHub API operations. For local use, follow [the supported CLI installation and TLS profile](release-github-cli.md).

## Project limits and rollout requirements

- Tooling contracts cover candidate/promotion rejection cases. Retired legacy publisher/dispatcher entry points now fail with a migration message; consumers pinned to older toolkit SHAs must migrate explicitly.
- ci.yml already runs CodeQL; security-required.yml adds strict full-source Trivy without duplicate language analysis.
- Source candidates carry an explicit inventory of toolkit source and are accepted by installing locked dependencies and running contracts against the extracted archive.
- The source distribution version is independent of immutable workflow references. Publishing a candidate does not update consumers or establish device or deployment acceptance.

For public repositories, merge and verify the workflows before enabling the
additive Terraform **CI gate** ruleset. Where release/deployment workflows use
environments, configure reviewers and default-branch-only policies. The governance
repositories contain `release-standards.tf` and opt-in examples for public
repositories only. Do not extend these requirements to private repositories by
buying a plan or to workflows that have not landed.

Existing review/security rules remain in force. Physical hardware, real
credentials/streams and production access are not implied by unit tests or builds.

The release engine/client are vendored from `victron-venus/venus-os-ci-toolkit`.
They are excluded from consumer-specific formatting/type policy. Application
release workflows run the mandatory Release tooling contracts job; validation-only
projects receive the local client, whose contracts run in the toolkit. Update the toolkit source, then run
`python3 scripts/install_release.py /path/to/consumer` from the toolkit checkout;
add `--check` to detect drift without writing files. The installer is not vendored
into consumer repositories.

References: [GitHub schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[protected environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments),
[artifact provenance](https://docs.github.com/en/rest/actions/artifacts).

## Automatic version preparation

Run `python3 scripts/release.py prepare-version --pr` from the clean default-branch HEAD. The command refreshes tags and opens a PR with synchronized owned version fields. An existing unreleased base is retained; use `--bump minor`, `--bump major`, or `--version X.Y.Z` for explicit intent. See [version plans](VERSIONING.md) for build overlays, the dedicated allocation ledger and recovery.

A local candidate package also needs the saved `.release-plan.json` at its exact source commit. Restore the `version_plan` object from the published `release-manifest.json` as `.release-plan.json` in a disposable checkout. Apply the recorded version fields with `python3 scripts/version_plan.py sync --root . --plan .release-plan.json` before `release.py package`; do not invent a tag or native counter locally. Ordinary development builds can use the project's native build command and explicitly local version identity.
