# CI dependency updates

GitHub Actions and reusable workflows are maintained by the centrally scheduled
`Renovate CI dependencies` workflow in this repository. It uses the existing
organization `BOT_PAT`, an explicit repository allowlist, and `requireConfig:
required`. Repositories opt in with `renovate.json` extending
`local>victron-venus/venus-os-ci-toolkit:renovate-ci`.

Each repository receives one `renovate/ci-workflows` PR containing all available
CI updates. Major, minor, patch and digest updates are grouped together. CodeQL
`init`, `autobuild`, `analyze` and `upload-sarif` must use the same full commit SHA
across all workflows; the existing required CI configuration check rejects a
partial update before invoking the scanners. Renovate does not enable automatic
merging or apply an `automerge` label. The normal review and required CI gate apply.

The generator reads `.github/action-pins.json`. Renovate updates this manifest in
the same group as workflow references, and the contracts reject generated
workflows that disagree with it. Regeneration preserves version comments, so
Renovate can continue resolving pinned actions. The SHA remains the executable
reference; the comment identifies the upstream release or branch to track.

Runner images and action tool-version inputs are excluded from this migration.
Existing Dependabot policies for application packages and containers, including
version exclusions, remain in place. Dependabot's Actions entry has a zero version
PR limit and an explicit ignore rule, which also suppresses separate security
update PRs. Merely deleting the entry would not suppress security PRs. GitHub
vulnerability alerts remain enabled. Renovate's separate vulnerability PR path
is disabled for this CI-only preset: patched action releases arrive through the
same daily grouped update and required checks.

## Operations

The sweep runs daily at 09:23 UTC. For a read-only check, dispatch the workflow
with `dry-run=true` (the manual default). Dispatch with `dry-run=false` for an
immediate update sweep. Only the default branch may run the privileged job.
Failures are visible in the workflow run; a missing token fails the job instead
of silently disabling updates. The runner does not execute dependency lifecycle
scripts or repository-defined post-upgrade commands.

`BOT_PAT` must permit repository contents and workflow writes (`repo` and
`workflow` for a classic PAT). The preflight rejects a classic token missing
`workflow`: Renovate otherwise logs a rejected workflow push while exiting
successfully. A fine-grained token must grant Contents and Workflows read/write,
as well as the other permissions required by Renovate's GitHub platform.
Tokens without an `X-OAuth-Scopes` response header produce an explicit warning:
authentication succeeded, but workflow write permissions remain unverified.
The preflight does not check fine-grained permissions for each target repository;
confirm those permissions separately before enabling write sweeps.

Commits use the dedicated SSH signing key in the repository Actions secret
`RENOVATE_SIGNING_KEY`, registered as a signing-only public key on `4alvit`.
`gitAuthor` matches that account's verified email. This key was generated for
this runner; it is not a personal authentication key. To rotate it, register the
replacement public signing key, update the secret, verify a signed Renovate PR,
then remove the old public key. Keep required-signature branch rules enabled.

To migrate another repository:

1. Add its full name to `renovate-repositories.json`.
2. Run `python3 scripts/migrate_ci_updates.py /path/to/repository`.
3. Add verified version/branch comments to bare SHA references. A bare SHA has
   no version selector and Renovate otherwise skips it.
4. Ship the current `scripts/workflow_contracts.py` through the required CI gate.
5. Validate the config with the pinned Renovate config validator, run workflow
   contracts and actionlint, then merge the migration through the normal gate.
6. Run a dry sweep followed by a live sweep and inspect the grouped PR.

Do not merge old standalone Dependabot action PRs after migration. Close them
when their changes are superseded by the grouped Renovate PR. Application
dependency PRs are unaffected.

References: [GitHub Actions manager](https://docs.renovatebot.com/modules/manager/github-actions/)
and [grouping options](https://docs.renovatebot.com/configuration-options/#groupname).
