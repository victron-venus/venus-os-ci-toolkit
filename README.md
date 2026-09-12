# Venus OS CI Toolkit

Reusable GitHub Actions workflows and composite actions for Victron Venus OS projects.

## Automatic approval and merge

Copy the [approval caller](docs/examples/auto-approve.yml) and [merge caller](docs/examples/auto-merge.yml) into `.github/workflows/` in each consumer, replacing `TOOLKIT_COMMIT_SHA` with a reviewed immutable commit from this repository. The metadata-only callers use `pull_request` for same-repository branches and `pull_request_target` for forks. They invoke pinned reusable code without checking out PR source. This preserves the existing trust in repository branch writers and lets an installation PR receive its first independent bot approval. For repositories that do not trust branch writers with workflow definitions, use only `pull_request_target` and arrange the initial human review.

Add the `automerge` label to a ready PR targeting the default branch. The default trusted authors are `4alvit`, `californiantiramisu`, `dependabot[bot]`, and `renovate[bot]`; the event actor does not decide eligibility. Drafts, other authors, and unlabeled PRs are left for manual review.

Forward `BOT_PAT` explicitly. Its account needs repository write access; automatic merging must be enabled in repository settings. Approval prefers the independent reviewer behind `BOT_PAT` and applies to the current commit. To automate a PR authored by the account behind `BOT_PAT`, forward a separate reviewer's `APPROVAL_PAT`; the workflow never approves its own PR. Missing credentials fail with a configuration error instead of silently skipping an expected approval.

For a classic `BOT_PAT` that merges workflow changes, include the [`workflow` scope](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps#available-scopes) alongside the appropriate repository scope. Repository write access alone does not grant this token permission. GitHub can reject a merge that combines workflow changes even when the bot successfully approved the PR.

If GitHub requires reapproval after the base branch changes, rerun the approval workflow. An existing same-head approval is refreshed only when GitHub reports `REVIEW_REQUIRED`; each reviewer records both the default-branch tip and GitHub's observed PR base commit to avoid repeating an approval for the same head and base context. The workflow rechecks both branch tips and PR eligibility before submitting a review, and does not repeat an approval when GitHub reports `CHANGES_REQUESTED`.

Before requesting native auto-merge, the workflow waits for the latest attempt of each reported check to pass, including optional checks, for up to two hours. It then requests GitHub native auto-merge for the verified head. Existing review requirements, CODEOWNERS, branch protection, and merge rules remain in force. If a check fails or the wait expires, fix or rerun that check and rerun the merge workflow. A new commit, label change, or ready transition starts a fresh evaluation.

GitHub can retain an enabled native auto-merge request when a contributor with write access pushes another commit. That existing request follows repository requirements and can merge before optional checks finish. Before updating such a PR, disable its old request with `gh pr merge PR_NUMBER --repo OWNER/REPO --disable-auto`, then push the new commit. The fresh workflow run will request auto-merge after checking the new head.

Concurrent GitHub runs can start in a different order from their numeric run IDs. Pending attempts always block merging; completed attempts are ordered by when their jobs started, with job IDs as a fallback. An earlier canceled attempt cannot replace a later successful result, and an earlier success cannot hide a later failure.

## Gated release standard

The current release process is defined by per-repository `.release-policy.json` files and the generator in `scripts/install_release.py`. The toolkit contains the complete fleet inventory in `fleet.json`.

- PR and merge queue: an aggregate **CI gate** requires every declared validation workflow.
- Nightly: staggered daily validation and build, with Actions artifacts.
- Beta/RC: immutable prereleases published only after the complete validation/build gate.
- Stable: a manual `release` environment approval promotes the exact RC bytes after source, run, evidence and checksum verification.
- Production deployment and container/PyPI publication are explicit separate operations using verified stable assets.

During rollout, candidate publication stays disabled until repository variable `RELEASE_CHANNELS_ENABLED=true`. Configure protected environments and migrate legacy auto-deploy webhooks before enabling it. Nightly checks/builds work while publication is disabled.

See [operator instructions](docs/release-workflow.md), [fleet rollout](docs/FLEET_ROLLOUT.md), and the project-specific guide generated in every consumer. Infrastructure, template, profile and unversioned operational repositories use validation-only policy.

```bash
python3 scripts/fleet.py status
python3 scripts/fleet.py render
python3 scripts/fleet.py check
# Inspect the isolated worktree diff and the project-specific validation evidence first.
python3 scripts/fleet.py submit --repo OWNER/REPO
python3 scripts/fleet.py submit --repo OWNER/REPO --execute
```

The submit command commits only the dedicated migration branch recorded in `fleet.json`, pushes that feature branch and opens a draft PR. It verifies both origin URLs and the repository policy before writing. It does not merge, apply Terraform, publish a release, or deploy production.

Old reusable `release.yml`, `docker-build.yml` and `nightly.yml` entry points now fail with a migration message. Their historical implementations are preserved in `docs/legacy-workflows/`. Consumers pinned to an older toolkit SHA must migrate explicitly; changing this repository cannot rewrite an existing SHA. New integrations use the gated policy and build-only adapter.

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
