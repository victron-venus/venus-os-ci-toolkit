# Fleet rollout: gated CI and release channels

This guide describes the shared rollout procedure; it is not a live deployment
inventory or proof that a repository has completed the migration. Use each
repository's current policy, default-branch workflows and check results.

Public inventories and documentation may identify only verified public
repositories. Keep non-public consumer inventories, rollout evidence, runner
registrations and deployment ownership in access-controlled operator storage.
Do not publish authenticated API results before filtering repository visibility.

## Documentation layout

Each application keeps its English release strategy in `RELEASING.md`: versioning,
branching, channel criteria, release ownership, acceptance, hotfixes and rollback.
The generated `docs/release-workflow.md` is the command runbook. Existing platform
setup instructions are preserved in `docs/release-packaging.md` where applicable.
README files contain short navigation links. Validation-only repositories document
their CI/deployment process without inventing application release channels.
The canonical strategy is `templates/release-strategy.md`; render and check the
entire fleet after changing the template or an individual policy.

## Activation order

1. Review the dedicated `ci/release-standard` changes and local validation evidence.
   `scripts/fleet.py check` checks generated drift, whitespace and workflow schema.
   `scripts/fleet.py submit --repo OWNER/REPO --execute` creates a draft PR.
2. For public repositories, require successful hosted CI on the exact PR head. Native OS matrices, Docker
   builds, ESPHome compilation and service integration need their actual runners;
   local syntax checks do not replace them. Callable release-build adapters run on
   the first unpublished nightly/default-branch build after merging; require that
   complete matrix to pass before enabling public release channels.
3. Merge workflow migrations. Public nightly/default-branch builds retain Actions
   artifacts, but candidate publication remains disabled by default. Consumers
   configured for local-only validation remove unavailable hosted validators and have no hosted nightly or
   CI gate. Use the [local nightly runner](LOCAL_NIGHTLY.md).
   Default-branch schedules and `pull_request_target` callers disappear after merge;
   a draft migration PR does not itself disable definitions on the default branch.
4. Deploy the reviewed webhook changes before enabling prereleases. Both webhook
   implementations default `AUTO_DEPLOY_STABLE_RELEASES=false`; push/tag/CI hooks
   cannot deploy, and only explicitly enabled published stable releases qualify.
   Keep this opt-in off until registry publication and deployment coordination are
   configured, since a GitHub release can be published before its registry import.
   The shared webhook image also moves to UID/GID 10001. Coordinate that image with
   its Compose update and prepare the narrow SSH/secret/config permissions described
   in its README. Do not recursively change monitoring data ownership. Treat
   committed redacted Compose files as templates; materialize live configuration
   through the operator’s secret store before a separately reviewed deployment.
   No host changes are performed by this migration.
5. In each GitHub Terraform governance repository, review the additive
   `release-standards.tf` resources. Add only migrated repositories to
   `release_gate_repositories`, and only configured applications to
   `release_channel_repositories`. Use `examples/release-standard.tfvars.json`
   as inventory input, not an instruction to apply every repository prematurely.
   Run `terraform plan -var-file=... -out=...`, inspect it, then apply that exact plan.
   Existing environments/rulesets must be imported if they already exist. These
   examples and governance resources target public repositories only; never upgrade
   a private plan or enable paid features to satisfy this migration.
6. For public release/deployment adapters, configure required reviewers and
   default-branch-only policies for `release` and `production` where used. The
   default single-maintainer example allows 4alvit to request and
   approve a promotion. For a team, add independent reviewers and prevent self-review.
   Private deployments use explicit manual source approval and ordinary pass/fail
   checks. They do not require an environment or an independent reviewer service.
7. After the deployment-hook and environment prerequisites are complete, enable
   `RELEASE_CHANNELS_ENABLED=true` for the chosen repositories through Terraform's
   `release_publication_enabled_repositories`. Nightly and automatic beta publication
   become active; manual RC/stable requests are then accepted.
8. Run a beta/RC, exercise the candidate on the intended hardware/environment, and
   request stable promotion. Stable payload bytes must be the tested RC bytes.
   Import verified stable OCI/PyPI artifacts separately, then deploy pinned digests.

No production deployment, Terraform apply, release creation or bypass of a failing
check is part of `fleet.py submit`. Drafts make this migration reviewable in batches.

## Local operation

Run from the repository checkout after its migration is merged:

```bash
python3 scripts/release.py check
python3 scripts/release.py package --version 1.2.3 --channel rc
python3 scripts/release.py rc --version 1.2.3
python3 scripts/release.py status
python3 scripts/release.py stable --rc v1.2.3-rc.1
```

Use the project's committed version; commands with publication effects dispatch the
protected default-branch workflow. The CLI pins the requested default-branch SHA and
refuses dirty/out-of-date checkouts. `--dry-run` displays a request without dispatch.
Public infrastructure and template repositories offer local checks and nightly validation.
Consumers with `ci_execution: local` expose only `check`, `status` and `doctor`
through the local client; they have no GitHub dispatch or publication commands.
A manual deployment must check the reviewed source and actual deployment
configuration before plan/apply.
`check` runs every declared `local_checks` command, including security/integration
commands. It stops on failure and does not qualify an RC for publication.

## Private repository cost boundary

Consumers opting into `ci_execution: local` use local validation instead of
hosted validators.
Unavailable validation, security, automatic approval and automatic merge workflow
files are removed from `.github/workflows/`; their reviewed definitions remain as
reference text in `docs/github-hosted-workflows/`. There are no hosted scheduled,
PR or manual validation callers to leave failing because of account billing.

A retained manual deployment workflow must keep source approval, local
validation, security checks and deployment on its approved trusted runner.
Review this per consumer; no runner provisioning, paid protection, Code Scanning
upload, spending-limit change or production apply is implied by the policy.

Local scans use OSS tools and return failure for findings or incomplete scans. The
local nightly runner uses normal toolchains on an existing machine, records the
checked SHA and marks remote freshness unknown. It does not install a scheduler,
fetch code, change branches or publish anything. Maintainers run and record local
checks on the proposed revision before merging. This is a review practice, not a
server-enforced GitHub gate; missing checks do not mean successful validation.

GitHub documents the availability boundaries for [Code Security](https://docs.github.com/en/code-security/reference/code-scanning/troubleshoot-analysis-errors/private-repository-enablement),
[environment protection](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/about-rulesets)
and [Actions usage](https://docs.github.com/en/actions/concepts/billing-and-usage).

## Verification and known boundaries

Record validation against an exact source revision. Historical fleet-wide test
counts cannot qualify a newer PR or release candidate. Verify, as applicable:

- Toolkit contracts, generated-file drift, identity checks and Actions schemas.
- Ordered local checks, failure propagation and rejection of unsupported
  publication commands for validation-only consumers.
- Source/native archive reproducibility, executable modes, versions, checksums
  and rejection of symlinks, missing files, untracked secrets and stale output.
- Application unit suites, integration services, native OS matrices and immutable
  release artifact identity on the actual candidate revision.
- Deployment-hook rejection of beta, RC, draft, push/tag and CI events. Mocked
  command tests do not establish production behavior.
- Infrastructure syntax and backend-disabled validation before a separately
  reviewed plan/apply. Do not suppress findings or broaden runtime permissions
  merely to make a check pass.

Physical Venus OS/GX, TVs, native decoders, real streams, external credentials
and production clusters require separate acceptance. A missing runtime or
skipped suite is an unresolved check, not a pass. Source policy is part of
immutable evidence; a later policy edit cannot qualify an older RC. Keep detailed
non-public rollout results in the affected project's access-controlled records.

## Recovery and rollback

Generated files are version-controlled; revert a reviewed migration commit to
restore the previous workflow definitions. Current worktree source branches and
uncommitted user changes were preserved during preparation. Do not disable required
security checks to get a release through. A failed gate requires a code/config fix
and a new successful run. A failed upload leaves a draft; inspect existing assets
and tags before recovery. The publisher never overwrites existing version tags.

Actions build artifacts retain 14 or 30 days according to the build adapter; promotion evidence retains
90 days. Missing/expired evidence requires a new RC. Published candidate releases
are immutable and are not automatically deleted by this initial rollout; review
nightly asset storage periodically and add an explicit retention policy before
long-term high-volume native publication.
