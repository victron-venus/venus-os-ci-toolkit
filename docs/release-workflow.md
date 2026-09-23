# CI and release operations — victron-venus/venus-os-ci-toolkit

The source of truth is `.release-policy.json`. `quality-gate.yml` runs the callable
validation workflows and produces the required **CI gate** status on every PR
and merge-queue commit. Superseded PR runs are cancelled. A lightweight Change scope
job checks the complete Git diff first. Documentation-only changes skip build and
test workflows; the gate accepts only these explicitly justified skips. Missing,
failed or unexpectedly skipped workflows fail the gate. Unknown files, incomplete
history, code, workflow and lockfile changes run full validation.

The optional `change_scope` policy provides exact `documentation_paths`, exact
`required_paths` for documentation used as a build input, and
`always_validate_workflows` for independently required checks. Documentation paths
cannot exempt source, tests, fixtures, build configuration or dependencies.
Manual dispatch, scheduled runs and release qualification remain full. A push
containing only documentation stops before release preparation, version allocation,
artifact builds or publication. This does not change the configured nightly policy.

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

## Nightly validation and deployment

This repository uses validation-only policy: PR/merge queue checks and staggered
nightly validation. It does not publish synthetic application beta/RC releases.
Infrastructure deployments remain manual and use the checks and explicit source
approval declared by their deployment workflow. Terraform validation uses backend-disabled
copies; a green syntax/validate job is not a reviewed plan or a deployment.

## Project limits and rollout requirements

- Tooling contracts cover candidate/promotion rejection cases. Retired legacy publisher/dispatcher entry points now fail with a migration message; consumers pinned to older toolkit SHAs must migrate explicitly.
- ci.yml already runs CodeQL; security-required.yml adds strict full-source Trivy without duplicate language analysis.

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
projects receive the local client, whose contracts run in the toolkit. Update the toolkit source and rerun
`scripts/install_release.py`; `--check` detects drift.

References: [GitHub schedules](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[protected environments](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments),
[artifact provenance](https://docs.github.com/en/rest/actions/artifacts).
