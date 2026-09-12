# Fleet rollout: gated CI and release channels

Inventory: 2026-09-12, GitHub owners `victron-venus`, `open-ott-play`, `4alvit`.
`fleet.json` records every discovered repository, its default branch, isolated
worktree and exclusions. There are 52 active owned repositories: 25 application
release policies and 27 validation-only policies. Five repositories are excluded
because they are archived, empty runner experiments, or an upstream fork.
Read-only visibility verification found 43 public and nine private repositories.
All current application release policies are public. The private repositories use
validation/deployment policies and require no paid GitHub security/governance features.

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
   artifacts, but candidate publication remains disabled by default. The nine private
   repositories remove unavailable hosted workflows and use local validation; they
   have no hosted nightly or CI gate. Use the [local nightly runner](LOCAL_NIGHTLY.md).
   Default-branch schedules and `pull_request_target` callers disappear after merge;
   a draft migration PR does not itself disable definitions on the default branch.
4. Deploy the reviewed webhook changes before enabling prereleases. Both webhook
   implementations default `AUTO_DEPLOY_STABLE_RELEASES=false`; push/tag/CI hooks
   cannot deploy, and only explicitly enabled published stable releases qualify.
   Keep this opt-in off until registry publication and deployment coordination are
   configured, since a GitHub release can be published before its registry import.
   The shared webhook image also moves to UID/GID 10001. Coordinate that image with
   its Compose update and prepare the narrow SSH/secret/config permissions described
   in its README. Do not recursively change monitoring data ownership. Portainer's
   committed redacted Compose is a template; materialize the ignored live Compose
   with the existing secrets before applying its deployment. No host changes are
   performed by this migration.
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
Private repositories expose only `check`, `status` and `doctor` through the local
client; they have no GitHub dispatch or publication commands. The retained manual
Portainer deployment checks the reviewed source and live Compose bytes on the
existing LAN runner before plan/apply.
`check` runs every declared `local_checks` command, including security/integration
commands. It stops on failure and does not qualify an RC for publication.

## Private repository cost boundary

The nine private repositories use `ci_execution: local` and no hosted validators.
Unavailable validation, security, automatic approval and automatic merge workflow
files are removed from `.github/workflows/`; their reviewed definitions remain as
reference text in `docs/github-hosted-workflows/`. There are no hosted scheduled,
PR or manual validation callers to leave failing because of account billing.

The sole retained private workflow is Portainer's manual redeployment on its
existing LAN runner. Selection, source approval, local validation, security checks,
and deployment all use that runner. No new runner, paid protection, Code Scanning
upload, spending-limit change or production apply is introduced.

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

- Local verification: 146 toolkit contract tests passed; generated-file,
  identity, whitespace and Actions schema checks passed for all 52 repositories.
  All 25 English strategy documents and 52 runbooks passed link/substitution checks.
  This evidence does not mean the changes are merged or that every hosted build
  and external acceptance check has passed on the submitted revision. Five local-only
  contracts additionally verify ordered checks, failure propagation, configuration-only
  status, absence of publishing commands and rejection of hosted dependencies.
- The nine private repositories were scanned with ordinary OSS tools. Five passed;
  four retain blocking infrastructure misconfigurations: `terraform-portainer-synology`,
  `home-assistant-k3s`, `terraform-oracle-oci`, and `k3s-self-healing`. Their
  `docs/security-checks.md` records the findings. Root/container capabilities and
  cluster RBAC need service-specific review; this migration does not suppress those
  findings or change production runtime permissions to make local checks pass.
- Toolkit: offline publication/promotion rejection tests, local-client tests,
  renderer contracts, registry byte identity and deployment digest tests. Existing
  PR-automation and Python typecheck contracts are retained. The typecheck install
  test needs PyPI access in its disposable environment.
- Service family: eight source/native archive adapters checked for reproducibility,
  file completeness, executable modes, version preservation, checksums and rejection
  of symlinks, missing files, untracked secrets and stale output. Four Python
  distributions built with strict Twine/checksum checks and temporary source staging.
- Applications: Vue 43 tests and SPA/library bundles; Python dashboard 179 tests,
  69.73% application-source coverage against the existing 68% baseline, frozen binary
  HTTP/SPA smoke; Go host binary/version smoke; Ottplay frontend regression tests,
  emitted classic-bundle smoke, ES5 grammar for 291 scripts and standard/Mode A bundles;
  Swop Worker dry-run bundle. Full native matrices remain hosted checks.
- Deployment hooks: 23 shared-webhook and 65 monitoring-webhook tests, with deployment
  commands mocked. Beta, RC, draft, push/tag/CI events cannot trigger deployment.
- Additional services: controller 573 tests, monitoring 79, FastAPI gateway 76,
  solar forecasting 91, MQTT interceptor 25 and exporter 20 tests pass. External
  database/container integration remains a required hosted gate.
- Infrastructure: source syntax and Actions schema checks; eight Terraform repos
  validate with backend disabled, including all 18 Portainer roots and the new GitHub
  governance resources. K3s seven tests and vitrine seven tests pass. Amazon voice:
  15 pass, three optional signature tests need the full webhook extras/container job.
- Physical Venus OS/GX, TVs, native decoders, DRM, real streams, external credentials
  and production cluster behavior are separate acceptance work. Docker/ESPHome/native
  matrices were not run on this workstation where their runtimes are unavailable.

Swop and IOT profile policies explicitly block RC/stable until the
missing application unit suites are on the candidate's source revision. Source
policy is part of immutable evidence; later policy edits cannot qualify an older RC.
Python dashboard gradual typing remains a documented baseline gap; the previous
silent mypy skip has been removed and no passing typecheck is claimed.

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
