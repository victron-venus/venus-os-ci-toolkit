# CodeRabbit review requests

CodeRabbit reviews are advisory: this integration adds no required check, approval,
or merge rule. The repository configuration selects `quiet` reviews,
enables automatic reviews where the service permits them, and includes drafts.

Public repositories with fewer than 10 stars require a manual review request under
[CodeRabbit's OSS plan](https://docs.coderabbit.ai/management/plans). The separate
review-request workflow posts `@coderabbitai review` as `californiantiramisu` once
when a PR opens, including draft PRs and contributions from forks. It skips
repositories with 10 or more stars, which use CodeRabbit's automatic reviews.
It does not request another review on each push or ready transition.

## Install in a public repository

1. Install the CodeRabbit GitHub App for the intended repository and enable reviews
   in its service settings.
2. Copy [the caller](examples/coderabbit-review.yml) to
   `.github/workflows/coderabbit-review.yml`. The example is pinned to the reviewed,
   merged toolkit commit `b81ed0aec645591f0c82a778eb8e3115db86f9ed`. Keep a full
   40-character SHA when upgrading after review; do not use a mutable branch or tag.
3. Copy the toolkit's `.coderabbit.yaml`, or merge its `reviews` settings into the
   repository's existing configuration without discarding other settings.
4. Provide the Actions secret `BOT_PAT`, owned by `californiantiramisu`, and forward
   only that named secret to the reusable workflow. For a fine-grained PAT, grant
   access to the selected public repositories with **Pull requests: write**;
   GitHub includes metadata read access. The account also needs permission to
   comment in those repositories. An existing compatible classic PAT can be used;
   this workflow does not require additional token scopes for checkout or merge.
5. Merge the caller onto the default branch before expecting new PR events to run
   it. The installation PR itself predates the trusted event workflow; validate
   it with the manual dispatch below after installation.

The toolkit uses its own local reusable workflow. Consumers must use the reviewed
SHA pin shown in the example. Neither caller needs `GITHUB_TOKEN` permissions.
This workflow is separate from automatic approval and merge; their author and
draft policies remain independent.

## Credential and execution boundaries

The [`pull_request_target` event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request_target)
runs trusted base-repository workflow code for fork PRs. The reusable workflow
runs inline Python on an ephemeral GitHub-hosted runner, with no checkout, cache,
downloaded artifact, package installation, or execution of PR content. PR titles,
bodies and branches are never interpolated into commands. Only the PR number is
accepted, validated as a positive integer, and used with the caller's repository.
Do not add steps that fetch or execute PR code to this credential-bearing job.

Before commenting, the workflow verifies live API metadata: the repository must
be public, unarchived and below 10 stars; the PR must be open with the expected
number and base repository. It verifies the token's account via `GET /user` and
requires exactly `californiantiramisu`. Missing credentials, another identity,
malformed eligibility metadata and API errors cannot cause a comment. Private
repositories also have a caller-level guard.

The workflow paginates all PR comments. It skips its exact marked request only
when both the comment author's account ID and login match the verified bot.
Another contributor copying the marker cannot suppress a review. A concurrency
group shared by automatic and manual runs serializes requests per repository/PR,
without canceling an active request. It rechecks repository and PR eligibility
immediately before the single comment write. GitHub API reads and writes are not
atomic; an external change in that last interval cannot be locked by this job.

The comment body is fixed:

```text
@coderabbitai review

<!-- toolkit-coderabbit-review:v1 -->
```

## Verify or recover

After merging the caller, use a newly opened PR or dispatch against the default
branch for an existing open PR:

```bash
gh workflow run coderabbit-review.yml --repo OWNER/REPO --ref DEFAULT_BRANCH \
  --field pull-request-number=123
```

Check that the workflow succeeds, that the comment is authored by
`californiantiramisu`, and that CodeRabbit responds. Run the same dispatch again
to verify it reports an existing request without posting another comment.
Posting the command proves delivery, not successful completion of CodeRabbit's
review; inspect the bot's response and checks separately.

PR-producing automation should use a suitable PAT or GitHub App token when it
needs to trigger this event workflow. GitHub
[restricts events created with `GITHUB_TOKEN`](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow#triggering-a-workflow-from-a-workflow).
If no review-request run starts, use the explicit dispatch above for that PR.

If a write fails ambiguously, inspect the PR and rerun the workflow: it checks all
comments before any new write and does not blindly retry a failed POST. If the
request already exists but CodeRabbit did not review, fix the app configuration
or service issue and request a new review manually. Do not delete the marker or
add a per-push workflow to bypass review-request deduplication.

## Request Autofix after a completed review

Copy [the Autofix caller](examples/coderabbit-autofix.yml) to
`.github/workflows/coderabbit-autofix.yml` and replace `TOOLKIT_COMMIT_SHA` with
the reviewed, merged 40-character toolkit commit. Forward the existing `BOT_PAT`.
The token must belong to `californiantiramisu`; a fine-grained token needs
**Pull requests: write** and **Commit statuses: read** on each selected repository.
The caller must be merged onto the default branch before status events reach it.

The [status event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#status)
runs the default-branch workflow when CodeRabbit reports success. It works for
fork PRs without checking out their code or exposing secrets to a PR workflow.
The reusable job verifies an actual submitted CodeRabbit review on the current
head; a successful status alone, including a skipped review, is insufficient.
It then finds unresolved, non-outdated CodeRabbit review threads containing
`Prompt for AI Agents` instructions and posts this command as the verified bot:

```text
@coderabbitai autofix
```

A hidden marker records the head and a hash of the finding IDs. The job allows
one automatic request per head, does not retry the same finding set after another
push, and caps automatic requests at three per PR. This bounds the feedback loop
when Autofix pushes a commit that produces another review. Requests beyond this
limit need a maintainer's explicit manual command. Do not delete the markers.
Closed, draft, private, archived, deleted-head and superseded PRs are skipped.
Open fork PRs are eligible; CodeRabbit's ability to write the fork still depends
on its permissions. PR authors are not used as an identity signal for CodeRabbit.

The job paginates reviews, threads and comments, verifies the bot's login and
account ID for deduplication, and rechecks the head and findings before writing.
Concurrent events for the same head are serialized. An API failure stops the job;
it never blindly retries a comment POST. As with review requests, external changes
between the last API read and the comment write cannot be locked by Actions.

For an existing reviewed PR, validate eligibility without a comment:

```bash
gh workflow run coderabbit-autofix.yml --repo OWNER/REPO --ref DEFAULT_BRANCH \
  --field pull-request-number=123 --field expected-head=FULL_HEAD_SHA \
  --field dry-run=true
```

Use `dry-run=false` to deliver an eligible request. Manual dispatch retains all
deduplication, review and three-round checks. It cannot force an unreviewed head.
Inspect the resulting CodeRabbit commit and normal CI before merging.
This workflow requests changes; it does not approve or merge the resulting code.
[Autofix availability and limits](https://docs.coderabbit.ai/finishing-touches/autofix)
are controlled by CodeRabbit. A delivered command is not proof of a completed fix.
