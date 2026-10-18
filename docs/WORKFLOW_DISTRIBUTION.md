# Caller-local reusable workflow distribution

Runner selection remains explicit: `CI_RUNNER_MODE=github` uses GitHub-hosted
workers; `self-hosted` (or its compatibility alias `k3s`) selects the configured
ephemeral pools. There is no billing poller, automatic retry or `auto` mode.
The caller's `CI_RUNNER_LABELS`, `CI_RUNNER_AUTOMATION_LABELS` and
`CI_RUNNER_RELEASE_LABELS` keep the existing routing contract. Platform-specific
and private-network jobs need their own qualified profile.

GitHub documents self-hosted reusable workflow access within one owner. Some
cross-owner calls work, but a caller-local distribution avoids depending on that
behavior or broadening runner access. Keep one authoritative toolkit source;
generate its job definitions into the consumer. Composite actions remain pinned
upstream because they execute inside an already assigned job.

## Generate and verify

With a local checkout containing the exact source commits and the generator's
locked dependencies installed:

```sh
python scripts/vendor_workflows.py /path/to/consumer --toolkit-repository /path/to/toolkit
python scripts/vendor_workflows.py /path/to/consumer --toolkit-repository /path/to/toolkit --check
```

The generator follows every toolkit reusable workflow dependency, including
relative calls and explicit references to other immutable toolkit commits.
It writes `toolkit-<workflow>-<full-source-SHA>.yml` files directly under the
consumer's `.github/workflows/`, rewrites job calls to local paths, and removes
independent triggers from distributed definitions. Job names, permissions,
secrets, checks and pinned action steps are preserved. Other external nested
reusable workflows are refused for explicit review.

`.github/ci-toolkit-workflows.lock.json` records each original source reference
and generated content hash. Commit this lock and the generated files together.
Do not edit the generated files: regeneration and `--check` reject content drift,
untracked collisions and symlinks. Old generated files are removed only when
their hash still matches the lock and no caller uses them.

For an upgrade, change the **caller job's** local `uses` back to the reviewed
upstream reference with its new full SHA, then regenerate. The generator updates
the lock, follows the new complete dependency graph and retires unused copies.
Review that diff and run the consumer's own tests before merging. Existing local
references regenerate from their recorded original sources without a network
request. A changed distribution has its own consumer commit; its SHA is not the
upstream source SHA.

## Switch either way

For a qualified private repository, set its three label variables to JSON arrays
containing the actual ARC scale-set name for each role. Do not append generic
`self-hosted/Linux/X64` labels to an ARC selector unless that exact configuration
has been explicitly qualified. Then change only the mode:

```sh
gh variable set CI_RUNNER_MODE --repo OWNER/REPOSITORY --body self-hosted
gh variable set CI_RUNNER_MODE --repo OWNER/REPOSITORY --body github
```

The switch affects new scheduling; it does not move running or queued jobs.
Finish or cancel obsolete runs deliberately and start a fresh run. Preserve
labels while using GitHub-hosted workers, so switching back needs no guessing.
Personal-account repositories need repository variables; organizations may use
selected-repository organization variables, unless repository overrides exist.

For self-hosted mode, prove a real job starts, uploads an artifact and disappears
with its runner pod. Idle worker count must return to zero. Controller/listener
pods remain running. A successful local generator check is not runtime admission.
Use separate CI, metadata and release credentials and networks; never replace a
required platform or silently move a private deployment to a hosted machine.

Retired persistent runner Deployments, registration bootstraps and self-healing
manifests must stay retired. Switching to `github` does not restore them, and
switching to `self-hosted` selects only the new ephemeral pools. See
[runner routing](RUNNERS.md) and the consumer's private operational runbook.

Reference: [GitHub reusable workflow runner scope](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).
