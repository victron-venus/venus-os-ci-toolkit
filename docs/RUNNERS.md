# Optional self-hosted routing

GitHub-hosted `ubuntu-latest` remains the default. No new runner, bootstrap job,
secret or Actions variable is created by the toolkit. The existing reusable
workflows evaluate routing directly when GitHub dispatches each job, including
the nested change-scope classifier and coverage uploader.

## Configuration

Configure Actions variables in the **calling repository or its organization**:

- `CI_RUNNER_MODE`: empty or `github` keeps hosted runners. Explicit
  `self-hosted` enables the configured pools; `k3s` is a compatibility alias.
  Other values do not enable self-hosted routing.
- `CI_RUNNER_LABELS`: JSON array selecting an isolated ephemeral Linux/x64 CI
  pool. For example `["self-hosted","linux","x64","your-ci-label"]`.
- `CI_RUNNER_AUTOMATION_LABELS`: JSON array selecting a separate trusted pool
  for approval, merging, CodeRabbit metadata requests and Renovate's bot tokens.
- `CI_RUNNER_RELEASE_LABELS`: JSON array selecting a separate trusted pool for
  release orchestration, publication and verified vendor delivery.

The names above are routing configuration, not runner registrations. Use labels
that actually exist and are available to the caller. ARC scale sets use their
single scale-set name, not a guessed array of generic labels. Missing labels in
an enabled profile produce an empty `runs-on` selection and fail admission;
malformed JSON also fails. Neither case guesses a private pool or silently sends
the job back to GitHub. Existing explicit `runner`/`runner-labels` inputs retain
their behavior for private callers and override shared routing when provided.

Set organization variables for the selected projects to switch centrally;
repository variables can override them. Organization variables do not configure
personal-account repositories such as `4alvit/*`. Runner visibility and group
permissions still apply in the caller's context. Sharing a workflow across owners
does not share the toolkit organization's machines. GitHub documents self-hosted
reuse for workflows owned by the same user/organization as the caller; cross-owner
callers such as `4alvit/*` calling this organization require separate live
qualification and must not be advertised as ready from configuration alone.

## Event and privilege boundaries

Shared CI routing accepts same-repository `pull_request` and default-branch
`push`, `schedule` and `workflow_dispatch`. Fork PRs, merge queues (which can
include fork changes), non-default branch dispatches and unknown events stay on
GitHub-hosted runners. Explicit inputs cannot bypass that boundary for public
callers. The metadata-only profile additionally permits same-repository
`pull_request_target` and `pull_request_review`; these workflows never check out
or execute PR code. Release routing accepts only default-branch events.

Use ephemeral jobs with fresh workspaces, no host/container runtime sockets, no
production network access and no shared signing/automation credentials in the CI
pool. Labels are selectors, not authorization: enforce selected-repository and,
where appropriate, pinned-workflow access through GitHub runner groups. Do not
reuse a runner attached to a private infrastructure repository for public code.
Promotion still enforces the existing source, toolchain and artifact checks; a
runner change cannot waive release evidence or replace RC bytes.

## One-time adoption

Consumers pin reusable workflows to reviewed commit SHAs. Merge the toolkit
change and adopt the new pins through the existing grouped Renovate updates.
For existing generated gates and repository-owned Linux/x64 validation/build
adapters, use the same installer without upgrading the release engine:

```bash
python3 -m pip install --require-hashes --only-binary=:all: -r .github/requirements-generator.txt
python3 scripts/install_release.py /path/to/consumer --runners-only
python3 scripts/install_release.py /path/to/consumer --runners-only --check
```

This mode follows local workflow calls from the policy's validators, Quality gate
and release workflows. It changes only literal `ubuntu-latest` and previously
managed toolkit runner expressions, preserving test commands, permissions,
thresholds, versions, locks and release scripts. It refuses recursive calls,
ambiguous aliases and helpers shared across conflicting CI/release profiles.
Its check covers runner adaptation only, not full release-toolkit freshness.

Other Ubuntu versions, custom/matrix selectors, native ARM64, Windows and macOS
retain their current routing. Review these explicitly before claiming that a
whole project can run during a hosted-runner outage. This switch does not move a
job already queued; after configuration changes start a new run and verify its
actual runner identity. Roll back with `CI_RUNNER_MODE=github`.

Before activating, verify runner/group access and a real trusted smoke run for
each required profile, its toolchains (Python/Node/Go/Java, Docker where used),
artifact access and network isolation. Linux alone is insufficient: for example
the Python adapter requires its existing apt/sudo and DBus build dependencies.
An offline or missing pool will leave jobs queued. Keep GitHub mode until this
admission is complete; do not change group visibility as an automatic fallback.

Self-hosted runners still depend on GitHub Actions orchestration, authentication,
checkout and artifact services. They cover hosted compute outages, not a complete
GitHub outage. No automatic failover is enabled.

## Toolkit checks

`scripts/runner_selection.py --check` verifies the shared declarations.
`tests/runner_selection.mjs` evaluates dispatch using the pinned official
`@actions/expressions` parser/evaluator, including defaults, profile separation,
fork exclusion, malformed configuration and private-input compatibility.
The existing CI contract job installs its locked test dependency and runs it.

References: [reusable workflow runners and variables](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations),
[runner group access](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access),
[self-hosted runner security](https://docs.github.com/en/actions/reference/security/secure-use).
