# Review policy with a CodeRabbit quota exception

This optional policy prevents a verified CodeRabbit review quota from indefinitely
blocking a PR. It does not submit an approval, merge a PR, waive required CI, or
interpret comments as approval evidence. It is separate from the toolkit's
advisory review request and automated approval workflows.

## Decision

The `Review policy` commit status passes when an independent reviewer formally
approved the current head. Accepted reviewers are the authentic CodeRabbit bot
(login `coderabbitai[bot]`, numeric ID `136622811`, type `Bot`) or a user whose live
repository permission is `write`, `maintain`, or `admin`. The PR author cannot
supply the approval. Review independence is from the PR author; this policy does
**not** reproduce GitHub's last-pusher independence rule. Commit authors and
committers are not reliable evidence of who pushed a branch.

The latest decisive review for each reviewer is used. A later comment does not
erase an approval or change request. An effective trusted `CHANGES_REQUESTED`
review blocks even if it refers to an older head, until it is dismissed or
superseded by that reviewer. Approvals must reference the current head.

With `allow-coderabbit-rate-limit: true`, the latest authentic CodeRabbit commit
status on the exact current head may instead qualify when its context is exactly
`CodeRabbit`, state exactly `success`, and description exactly
`Review rate limited`. This is an observed quota signal; it does not distinguish
monthly quotas from other rate limits. Historical quota signals, a newer pending
review, unrelated bots, plan-upgrade messages, and unrecognized errors do not
qualify. This option defaults to false. A quota exception also requires the
review-event listener file to have the same Git blob on the PR head and base;
changing or disabling the listener requires a real independent approval.

The evaluator marks the head pending before reading evidence, fetches evidence
again and rechecks the PR head, base, author and open/draft state before publishing
a result. Authenticated event SHAs and listed open heads are marked pending before
further metadata reads. API failures after that remain blocking; an outage that
prevents writing any status cannot revoke a previously published success.
GitHub does not provide an atomic read-and-set operation. Review and policy
updates are asynchronous; a later event, scheduled run, or manual reevaluation
is needed when evidence changes after the final read. Native rules must continue to
require resolved review threads and the strict `CI gate` check.

## Install deliberately

1. Copy [review-policy.yml](examples/review-policy.yml) into the consumer's
   `.github/workflows/` and pin its reusable workflow to the reviewed toolkit
   commit that contains it. The example pin must be updated when adopting the
   newly added workflow.
2. Copy [review-policy-events.yml](examples/review-policy-events.yml) unchanged
   to that same directory. Its name and path form the review-event relay contract.
3. Merge the two callers onto the default branch. The privileged evaluator refuses
   to run from any other branch. It never checks out PR code, reads artifacts,
   evaluates PR text, or executes caller-provided commands.
4. Run `Review policy` from the default branch with an open PR number. Verify the
   `Review policy` status is produced by GitHub Actions.
5. Add `Review policy` as a required status bound to GitHub Actions (app ID
   `15368`), retaining strict required `CI gate`, conversation resolution,
   no-bypass and branch protections. **Install the new required check before
   removing the unconditional approval requirement.**
6. Only for explicitly opted-in repositories, set the native required approval
   count to zero and turn off native last-push approval. Native unconditional
   approvals cannot express the quota exception. Other repositories keep their
   existing review policy.

The initial consumer is `ha-homelab/ha-desloc`; this does not opt the card or other
repositories into a new review requirement. The policy status is emitted by the
GitHub token with `statuses: write`; it does not use the shared bot's personal token.

Review events execute their small, permission-free listener from a PR merge ref.
A `workflow_run` event then runs the actual evaluator from the protected default
branch and fetches live PRs/reviews itself. The listener's outputs and conclusion
are never accepted as evidence. CodeRabbit status events also run from the default
branch; both the caller and evaluator verify its bot identity and exact context,
so the evaluator's own status cannot create a loop.

Every accepted event reconciles all open default-branch PRs. GitHub can replace
queued concurrency runs, so this prevents a later event for another PR from
stranding an earlier PR's stale success. Failure on one PR does not stop the rest.
Two open PRs sharing one head SHA are blocked because a commit status cannot
represent separate PR review decisions safely. Use separate branches/commits.
A staggered 15-minute schedule reconciles missed events and listener changes;
GitHub may delay scheduled runs, so this is a recovery backstop, not a zero-latency
guarantee. Native collaborator change requests and unresolved threads remain
blocking under the retained pull-request rule.

A `workflow_dispatch` from the default branch with an explicit PR number is the
recovery path for missed events, approval permission changes, or a transient API
failure. Native auto-merge should consume required GitHub rules. The older toolkit
merge mode that waits for every optional check can still be held by an unrelated
optional vendor check; it should not be used as the quota exception's authority.
