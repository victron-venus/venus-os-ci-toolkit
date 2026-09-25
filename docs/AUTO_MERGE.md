# Native auto-merge for protected CI gates

The reusable `auto-merge.yml` keeps its existing check-waiting behavior by default.
Set `native-auto-merge: true` only after every required PR validator is included in
`quality-gate.yml` and the repository requires `CI gate` with strict branch freshness.
This mode verifies the effective GitHub branch rules, re-reads PR eligibility and the
head commit, requests native auto-merge, and exits without occupying a waiting runner.
GitHub still enforces reviews, required checks and other branch rules. External
checks (such as SonarCloud and code scanning results) must also be required branch
checks and listed in `required-status-checks` before opting in.

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

## Reviewed merge for repositories without protected branch rules

A private repository may have no branch protection on its current GitHub plan.
For these repositories, `reviewed-merge: true` provides an opt-in bot gate while
leaving existing native and legacy consumers unchanged. It is mutually exclusive
with `native-auto-merge: true` and requires an explicit, nonempty
`required-status-checks` list of exact check names.

The shared workflow reads metadata only. It requires a ready same-repository PR
targeting the default branch, a trusted author and the **`automerge`** label.
Every named check must finish with `SUCCESS`; missing, pending, skipped or neutral
required checks block the merge. All other observed checks must also finish
without failure. An independent approval from `4alvit` or `californiantiramisu`
must reference the current head commit, and no reviewer's latest decisive review
may request changes. All review pages are read; a later comment alone does not
retract an approval or changes request.

Checks, review decisions and the head must remain stable across two observations
and a final refresh. The merge uses `--match-head-commit` and never `--admin`.
This mode merges directly after verification; it does not leave a queued native
auto-merge request on an unprotected branch. The caller should listen to the same
PR metadata events as native mode, preserve the no-forks boundary, pin this
workflow to a reviewed full SHA, and forward only `BOT_PAT`. Adding `automerge`
opts a PR in; removing it or converting the PR to a draft withdraws eligibility.
The workflow waits at most 120 minutes. If validation or review takes longer,
remove and re-add the label after the checks finish to retry.

This is a bot policy, **not server-enforced branch protection**. It cannot stop a
maintainer's separate manual merge or make check/review/label reads atomic with
GitHub's merge operation. A last-moment review or check change can occur after the
final refresh; the head guard prevents merging a different commit, not all such
metadata races. Enable proper branch rules and use protected native mode when
available. Never remove existing branch protections to use this mode.


## Metadata runner selection

Both `auto-merge.yml` and `auto-approve-reusable.yml` accept `runner-labels`, a
JSON array that defaults to `["ubuntu-latest"]`. A private consumer can select an
existing dedicated trusted runner, for example
`["self-hosted","Linux","X64","mp","private-ci","robinhood"]`. The runner must
already provide `python3` and `gh`; neither workflow checks out source or installs
packages. Set labels in the trusted base workflow, never from PR title, body,
branch name or another contributor-controlled value. Keep the token confined to
the metadata job, and preserve the repository's own exact workflow policy.
Both Python entrypoints run in isolated mode (`-I`) so a leftover checkout,
`PYTHONPATH`, or user site directory cannot replace their standard-library
imports. The runner host and its preinstalled Python/`gh` executables must still
be trusted; this does not isolate metadata jobs from a compromised persistent host.
This option does not provision a runner, change GitHub billing, or grant runner
access across repositories. A waiting merge job occupies a runner slot, so label
a PR for merge after its CI has completed when using a single dedicated runner.


## Recover an unavailable approval runner

`auto-approve-reusable.yml` accepts optional `pull-request-number` and
`expected-head` inputs only for an explicit `workflow_dispatch` caller. The head
must be the full 40-character lowercase commit SHA. Recovery retains the normal
trusted-author, default-base, open/non-draft and independent-reviewer checks and
also requires a same-repository head on both metadata reads. A different or
changed head is never approved. This interface adds approval only, not merge.

Use a reviewed dispatchable caller with the exact pinned toolkit revision and a
trusted runner; never execute PR source in the metadata job. A recovery caller
should validate its dispatched commit through its ordinary CI gate before the
review job and require `github.sha == inputs.expected-head`. For example, an
existing private repository CI workflow can expose optional recovery inputs and
run the metadata-only approval job after its gate succeeds. Existing automatic
PR-event callers keep their behavior when these inputs are omitted.

This is intended to repair a broken base approval runner without extracting its
secret, changing billing, or claiming a failed old run succeeded. Verify the
actual bot review references the requested head, then merge only after the normal
checks pass. After migration, run the ordinary approval workflow on another ready
PR to verify the new runner path. A dispatch from an unreviewed branch is not an
acceptable bootstrap procedure.
