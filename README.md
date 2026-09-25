# Venus OS CI Toolkit

## Runtime scope

This toolkit runs in GitHub Actions; it is not a service installed on Venus OS.
A green workflow on an x86 Linux runner does not establish compatibility with
Cerbo GX ARMv7 or Raspberry Pi firmware. Consumers shipping native GX services
should separately exercise BusyBox/POSIX installers, offline dependency failure,
repeated installation, `/data/rc.local` boot persistence, bounded logs, and the
actual firmware Python ABI. Venus OS uses daemontools (`svc`, `svstat`), not
systemd; container jobs are companion-host build/test environments.


Reusable GitHub Actions workflows and composite actions for Victron Venus OS projects.

## Automatic approval and merge

Copy the [approval caller](docs/examples/auto-approve.yml) and [merge caller](docs/examples/auto-merge.yml) into `.github/workflows/` in each consumer, replacing `TOOLKIT_COMMIT_SHA` with a reviewed immutable commit from this repository. The metadata-only callers use `pull_request` for same-repository branches and `pull_request_target` for forks. They invoke pinned reusable code without checking out PR source. This preserves the existing trust in repository branch writers and lets an installation PR receive its first independent bot approval. For repositories that do not trust branch writers with workflow definitions, use only `pull_request_target` and arrange the initial human review.

Ready PRs from trusted authors targeting the default branch receive automatic approval regardless of labels. The default trusted authors are `4alvit`, `californiantiramisu`, `dependabot[bot]`, and `renovate[bot]`; the event actor does not decide eligibility. Drafts and other authors are left for manual review. Add the `automerge` label only when automatic merging is wanted; approval alone does not enable it.

Dependabot PRs are approved as `github-actions[bot]` using the caller's automatic `GITHUB_TOKEN` with `pull-requests: write`. Enable **Allow GitHub Actions to create and approve pull requests** in repository Actions settings. No `BOT_PAT` or `APPROVAL_PAT` Dependabot secret is needed for approval. Credential selection uses the PR author, not the actor who labels or reruns it.

For other trusted authors, including `4alvit`, forward `BOT_PAT` explicitly. Approval still prefers the independent reviewer behind `BOT_PAT` and applies to the current commit. To automate a PR authored by the account behind `BOT_PAT`, forward a separate reviewer's `APPROVAL_PAT`; the workflow never approves its own PR. Missing credentials fail with a configuration error instead of silently skipping an expected approval. Merging continues to use `BOT_PAT` for all authors, preserving PAT-triggered downstream workflows; its account needs repository write access and native auto-merge must be enabled.

For a classic `BOT_PAT` that merges workflow changes, include the [`workflow` scope](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps#available-scopes) alongside the appropriate repository scope. Repository write access alone does not grant this token permission. GitHub can reject a merge that combines workflow changes even when the bot successfully approved the PR.

If GitHub requires reapproval after the base branch changes, rerun the approval workflow. An existing same-head approval is refreshed only when GitHub reports `REVIEW_REQUIRED`; each reviewer records both the default-branch tip and GitHub's observed PR base commit to avoid repeating an approval for the same head and base context. The workflow rechecks both branch tips and PR eligibility before submitting a review, and does not repeat an approval when GitHub reports `CHANGES_REQUESTED`.

Before requesting native auto-merge, the workflow waits for the latest attempt of each reported check to pass, including optional checks, for up to two hours. It then requests GitHub native auto-merge for the verified head. Existing review requirements, CODEOWNERS, branch protection, and merge rules remain in force. If a check fails or the wait expires, fix or rerun that check and rerun the merge workflow. A new commit, label change, or ready transition starts a fresh evaluation.

GitHub can retain an enabled native auto-merge request when a contributor with write access pushes another commit. That existing request follows repository requirements and can merge before optional checks finish. Before updating such a PR, disable its old request with `gh pr merge PR_NUMBER --repo OWNER/REPO --disable-auto`, then push the new commit. The fresh workflow run will request auto-merge after checking the new head.

Concurrent GitHub runs can start in a different order from their numeric run IDs. Pending attempts always block merging; completed attempts are ordered by when their jobs started, with job IDs as a fallback. An earlier canceled attempt cannot replace a later successful result, and an earlier success cannot hide a later failure.

## CI and release documentation

The [private OTTPlay k3s runner configuration](docs/ottplay-k3s-runners.md) adds
an opt-in GitHub/ARC switch, ephemeral worker images and a verified manual
activation command. It does not alter the fleet's default execution policy.

The [change-scope guide](docs/CHANGE_SCOPE.md) describes documentation-only checks,
full release qualification and per-repository exceptions. Generated pipelines
skip heavy builds and automatic releases for verified documentation-only changes.

See the [fleet rollout guide](docs/FLEET_ROLLOUT.md), [local nightly runner](docs/LOCAL_NIGHTLY.md), and [toolkit operations](docs/release-workflow.md). Application repositories keep their strategy in `RELEASING.md` and commands in `docs/release-workflow.md`; README files only link to them. The canonical English strategy is [templates/release-strategy.md](templates/release-strategy.md). The [application release runbook](docs/APPLICATION_RELEASES.md) covers request ordering, workflow-change recovery, approval and stable promotion across public applications.

Private repositories run ordinary OSS checks locally. Unavailable GitHub-hosted workflows are archived outside workflow discovery; only an existing manual deployment on a working self-hosted runner is retained. No paid GitHub security or governance feature is required. The reviewed inventory and visibility are recorded in [fleet.json](fleet.json).

## Validation

```bash
python3 -m pip install PyYAML==6.0.3
bash scripts/ci.sh
```

The contract suite includes rejected missing/skipped gates, wrong source revisions, checksum changes, forged/expired evidence, release collisions and stable byte identity. Consumer release workflows run the vendored engine contract tests as a required gate.

## Updating consumers

1. Change and test the toolkit source.
2. Render each consumer from its reviewed policy with `scripts/install_release.py`.
3. Verify generated drift with `--check`, inspect diffs, and submit PRs.
4. Merge workflows before enabling their required status checks. Keep existing security and review requirements active.

## License

MIT License — see [LICENSE](LICENSE).
