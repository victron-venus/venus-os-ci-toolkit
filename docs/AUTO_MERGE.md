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

The example caller lets label removal, draft conversion and same-repository base
retargeting reach the shared handler. Both the default check-waiting path and the
protected native path cancel an existing request only for the observed trusted
PR, after a second metadata read confirms the same head, base, author and request
and that eligibility is still withdrawn. Cancellation also needs an explicit
current `automerge` label, an earlier eligible observation of the same request,
or the matching webhook that removed that label. For a removal webhook, the
request must predate the event's PR update time; missing timestamps and equal
second ambiguity do not authorize cancellation. This preserves manual native
requests after unrelated or stale events. A restored label or changed identity
is left for its new event; unrelated PRs are not scanned or modified. As with
merge requests, GitHub provides no atomic operation combining this read with
cancellation, so a final concurrent metadata change remains possible.

Keep the example's default-base guard for fork `pull_request_target` events.
Retargeting a fork PR away from the default branch does not reach this handler;
do not remove that guard to expose credentials in another workflow context.
Until the separate trusted default-context cleanup in
[issue #330](https://github.com/victron-venus/venus-os-ci-toolkit/issues/330) is
implemented, a maintainer must explicitly check and disable any remaining native
auto-merge request on such a retarget. This caller does not promise universal
revocation across all fork/base transitions.

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


## Release the runner between reviewed evaluations

For a consumer with scarce metadata runners, set both `reviewed-merge: true`
and `reviewed-single-pass: true`. This opt-in evaluates every eligible open PR
once, using GitHub's paginated PR list. Pending, missing or failed validation and
missing reviews defer that PR without sleeping; other candidates are still
examined. The job has a five-minute timeout. API, authentication and malformed
identity errors fail visibly instead of being reported as a successful check.
An unsuccessful merge command is never retried within the same evaluation;
other candidates still receive fresh checks, and the run fails after the scan
with the affected PR numbers. Metadata/read failures, including authentication
failures during those reads, stop immediately.
Existing consumers keep their polling behavior until explicitly migrated.

The caller must provide future wakeups; otherwise a deferred PR will remain open:

- Keep `pull_request_target` state/label events, with a same-repository head and
  trusted-author guard. For a privileged self-hosted caller, do not add
  `pull_request` or `pull_request_review`: their workflow context can include PR
  changes. Manual reviews can be picked up by the recovery schedule.
- Use `workflow_run: {types: [completed], workflows: [...]}` with the exact names
  of both the CI and approval workflows. A review made with `GITHUB_TOKEN` may not
  create another workflow run through `pull_request_review`.
- Add `status` for external commit statuses. `check_run: {types: [completed]}` can
  help with external checks, but GitHub suppresses some check events associated
  with Actions commits; it is not a replacement for `workflow_run`.
- Add `workflow_dispatch` and a recovery schedule, such as every fifteen minutes,
  in the opted-in caller to recover missed external events. Do not install a
  fleet-wide scheduled poll as part of an unrelated consumer update.

Use one repository-wide concurrency group for this caller with
`cancel-in-progress: false`. Each invocation evaluates all eligible PRs, so
coalescing queued events does not discard an individual PR's evaluation. Allow
non-PR wakeups through an explicit event allowlist and a
`github.ref == format('refs/heads/{0}', github.event.repository.default_branch)`
guard. Keep the caller's same-repository/trusted-author check on PR-target
events. The reusable workflow resolves current PR identities itself and enforces
its author, label, default-base,
non-draft and same-repository rules. Event head SHAs and event PR lists are only
wakeups, never authorization. Keep this workflow on the default branch, pin the
reusable code, and never check out PR code or execute downloaded artifacts in it.
Do not include this merge workflow itself in the `workflow_run` list.

This mode also requires the **current default-branch commit to be an ancestor of
the PR head**. If another PR has merged, update the branch and rerun CI before it
can merge. The base, head, checks and review decisions must remain stable across
three fresh observations; the ancestry check is repeated before the merge. An
old cancelled auto-merge check is excluded only after its repository, workflow,
Actions job and GitHub Actions CheckRun identity are verified through GitHub's
API. A similarly named check from another workflow or app remains mandatory.
The current invocation need not have a check on the PR, which is normal for
`workflow_run`, `status` and scheduled events.

The existing limitation still applies: GitHub offers no atomic transaction for
these metadata reads and the subsequent merge. `--match-head-commit` guards the
head; a last-moment base, review or check change after the final read requires
server-enforced branch rules to eliminate completely.

### Official GitHub Actions reviewer

Reviewed mode keeps the existing `4alvit` and `californiantiramisu` independent
reviewer policy by default. Two separate options align it with repositories that
already permit the shared approval workflow's official `GITHUB_TOKEN` paths:

- `allow-dependabot-actions-review: true` accepts `github-actions[bot]` only for a
  genuine GitHub bot identity `dependabot[bot]` in a same-repository PR.
- `allow-owned-bot-actions-review: true` separately accepts that reviewer for the
  existing `californiantiramisu` fallback. Enable it only where that fallback is
  part of the repository's intended approval policy.

Both require reviewed mode. The REST reviewer identity must be exactly
`github-actions[bot]` with type `Bot`, and its latest decisive review must approve
the current head. Dismissals and any outstanding changes requests still block.
Neither option admits Actions approval of a `4alvit`-authored PR or another bot's
PR. Human-authored PRs continue to require the independent bot/PAT review.

This trusts workflows allowed to issue Actions reviews in that repository,
matching GitHub's **Allow GitHub Actions to create and approve pull requests**
setting. The review does not prove which specific workflow produced it. Keep
workflow modification permissions and branch-writer trust consistent with that
policy; the merge workflow still uses `BOT_PAT`.

See GitHub's [workflow event reference](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
and [GITHUB_TOKEN event behavior](https://docs.github.com/en/actions/concepts/security/github_token)
when choosing the consumer's wakeups. Completion workflows must exist on the
default branch and remain within GitHub's workflow chaining limits.


## Metadata runner selection

Both `auto-merge.yml` and `auto-approve-reusable.yml` accept `runner-labels`, a
JSON array that defaults to `["ubuntu-latest"]`. A private consumer can select an
existing dedicated trusted runner, for example
`["self-hosted","Linux","X64","trusted-metadata"]`. The runner must
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
a PR for merge after its CI has completed when using a single dedicated runner,
or opt into the nonblocking reviewed evaluation above. Public repositories keep
the existing runner trust boundary: completion/status wakeups use hosted runners;
this change does not extend public self-hosted routing to those events.


## Recover an unavailable approval runner

`auto-approve-reusable.yml` accepts optional `pull-request-number` and
`expected-head` inputs only for an explicit `workflow_dispatch` caller. The head
must be the full 40-character lowercase commit SHA. Recovery retains the normal
trusted-author, default-base, open/non-draft and independent-reviewer checks and
also requires a same-repository head on both metadata reads. A different or
changed head is never approved. This interface adds approval only, not merge.

Manual dispatch has no PR author in its event payload. Recovery first reads the
PR metadata, then uses `GITHUB_TOKEN` for Dependabot or the configured independent
PAT for other trusted authors. A missing installation token never falls back to
a PAT for a Dependabot review. The final metadata read also verifies the author
has not changed before submitting the exact-head approval.

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

## Dependabot event context

Dependabot `pull_request` runs cannot read repository Actions secrets. The shared
caller examples route its metadata jobs through `pull_request_target` only when
the base is the repository default branch; they never check out or execute PR
code. Approval still uses the automatic `GITHUB_TOKEN`, while merge automation
receives the explicitly forwarded `BOT_PAT`. Other trusted same-repository
writers retain their existing `pull_request` path.

Update the caller condition as well as the pinned reusable revision. A pin-only
update cannot change event selection. See [GitHub's Dependabot Actions restrictions](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-on-actions).
