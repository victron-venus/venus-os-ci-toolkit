# Native auto-merge for protected CI gates

The reusable `auto-merge.yml` keeps its existing check-waiting behavior by default.
Set `native-auto-merge: true` only after every required PR validator is included in
`quality-gate.yml` and the repository requires `CI gate` with strict branch freshness.
This mode verifies the effective GitHub branch rules, re-reads PR eligibility and the
head commit, requests native auto-merge, and exits without occupying a waiting runner.
GitHub still enforces reviews, required checks and other branch rules.

Call it from `pull_request_target` with a pinned toolkit commit, explicit `BOT_PAT`,
and no PR checkout. Listen to `unlabeled`, `edited`, and `converted_to_draft` as well
as the normal PR events. Do not filter out label removal or draft transitions in the
caller: the reusable workflow disables an existing auto-merge request when eligibility
is withdrawn. Approval remains a separate workflow: Dependabot uses `GITHUB_TOKEN`;
human PRs use the independent bot's `BOT_PAT`.

`single_entry_ci: true` in `.release-policy.json` installs required CI configuration
contracts. Callable validators keep `workflow_call` (and optional manual dispatch),
while Quality gate or Release pipeline owns automatic triggers. Dependency Review
belongs in the gate; non-PR events report it as not applicable without skipping its job.
Keep specialized independent PR validation in the legacy merge mode until it is also
covered by a required gate. This migration does not change release approval policy.
