# Application release operations

This runbook applies to public application repositories whose `.release-policy.json`
selects release mode. Validation-only projects, including this toolkit, do not
publish application candidates. Private repositories remain outside this rollout.
Use the consumer's `RELEASING.md` and `docs/release-workflow.md` for its actual
version files, build matrix, blockers and deployment adapters.

## Prepare the next version

Consumers can require human-readable notes by setting `"release_notes":
"CHANGELOG.md"` in `.release-policy.json`, then regenerating their installation.
The release engine reads this file from the exact source commit used for the
build. Its `## [X.Y.Z]` section must summarize the changes for that base version
and contain nonempty `### Upgrade` and `### Security` subsections. State upgrade
steps, compatibility changes, fixed vulnerabilities and advisory identifiers
where applicable. When there are no security changes, say so based on the actual
release contents. Do not substitute commit logs or CI provenance for these notes.

Beta, RC, nightly and stable releases select the same base-version section. The
engine appends the original build provenance. Missing or ambiguous sections,
invalid content or a mismatched Git blob fail before tag or release creation.
Changes to a later default-branch changelog cannot relabel an earlier build.
Existing consumers without this option retain their prior behavior; migrate
their current version's notes together with the engine and policy. This option
does not rewrite historical release descriptions.

Update the committed base version and its package/native companions through a
reviewed PR. Beta and RC share that base and receive separate server-selected
counters. Once `vX.Y.Z` exists, beta/RC require a new base; nightly may still use the
existing version. Run the project's local checks, merge with the required CI and
review, then update to a clean checkout at GitHub's default-branch HEAD.

Candidate publication requires `RELEASE_CHANNELS_ENABLED=true`. Its durable owner
configuration is the tracked `release-activation.auto.tfvars.json` in the relevant
Terraform repository. Activate only reviewed public repositories with working
checks/builds, release reviewers and migrated legacy release deployment hooks.
Validation success alone does not enable publication or apply infrastructure.

## Request and follow a candidate

Run `python3 scripts/release.py status` before requesting a beta, RC or stable.
Wait for the existing Release pipeline runs to finish. The configured concurrency
policy is not FIFO: a new pending run can replace the old pending run even with
`cancel-in-progress: false`. See [GitHub concurrency](https://docs.github.com/en/actions/concepts/workflows-and-actions/concurrency).

Default-branch pushes build beta; the daily schedule builds nightly. For a manual
candidate, run `python3 scripts/release.py beta` or `python3 scripts/release.py rc`
from the clean current checkout. Both use the checked-in version. Follow the exact
requested run in Actions and check its final result; dispatch acceptance is not
publication success. All declared checks, platforms and Release gate must pass.

## Automatic nightly publication reuse

Versioned repositories still allocate a fresh plan and run all required checks,
current security scans and the complete nightly build matrix. This preserves
coverage of mutable base images, downloaded packages and hosted toolchains even
when the source commit is unchanged. After the Release gate and new build receipts
pass, a scheduled nightly can report `reused` instead of publishing duplicate
GitHub release assets. It requires a published beta, RC or stable with the exact
same source, policy and base version, successful originating CI/Release gates,
retained immutable Actions evidence, and matching asset sizes and digests.

Only publication is skipped: the new build artifacts remain in Actions under
normal adapter retention. No new release/tag or promotion evidence is created,
and the publication floor stays unchanged so this nightly does not invalidate the
accepted RC. The allocated build number remains reserved; gaps are intentional.
Manual nightly requests, new sources, and missing, expired or unverifiable prior
evidence follow normal publication. Failed current checks/builds still fail the
run. This does not reset an existing floor or restore an already obsolete RC;
those still require a new RC. Legacy repositories retain their existing behavior.

## Handle a workflow change during a build

GitHub's release API requires workflow-write authorization when the target commit's
workflow files differ from the current default branch. The workflow `GITHUB_TOKEN`
cannot receive that authorization. A long build can therefore pass every check
and still fail publication with `403 Resource not accessible by integration` or
`404` after a workflow change lands. Ordinary source changes with an unchanged
workflow tree do not create this restriction. See the [release API permission note](https://docs.github.com/en/rest/releases/releases#create-a-release).

The publisher checks the exact workflow tree before advancing the publication
floor and again before creating the tag. A known mismatch fails without those
writes; ordinary source changes with the same workflow tree remain eligible.
An explicitly configured publication token retains its existing exception only
after its workflow authorization probe succeeds. GitHub does not make these reads
and publication atomic: a later branch change can still cause a partial failure.

Inspect existing tags and draft/published releases before recovery. Preserve any
existing candidate and its evidence. Use the candidate build for the current
workflow tree, or request a new one after the existing pipeline finishes. Retrying
the old SHA does not fix a workflow-tree mismatch. Never relabel old payloads as
coming from another commit or replace a published tag.

## Accept and promote the exact RC

Test the selected RC assets on their intended platforms and integrations; record
the tag, source SHA, results and limitations in the PR or linked issue. Hardware,
real streams and production acceptance are separate from hosted build success.
Stable copies the accepted RC bytes without rebuilding. Its copied manifest keeps
the original RC tag/channel because those fields describe payload provenance.

Before stable, compare the selected RC workflow tree with current `main` (the
current public application default branch), using the actual RC tag:

```bash
rc_tag=v1.2.3-rc.2
git fetch origin main --tags
git diff --exit-code "$rc_tag" origin/main -- .github/workflows/
python3 scripts/release.py doctor
python3 scripts/release.py stable --rc "$rc_tag" --dry-run
```

A workflow difference requires a new RC and renewed acceptance. `doctor` checks
that a release reviewer exists. `--dry-run` displays the proposed dispatch; it
does not verify the RC payloads, promise server acceptance or publish anything.
After acceptance and preflight, request stable without `--dry-run` and follow its
exact run. The publisher verifies the original run/attempt, source policy, ancestry,
Release gate, immutable Actions evidence and every asset checksum. Evidence is
retained for 90 days by configuration; deleted or expired evidence requires a new RC.

## Approval, administrative access and deployment

Normal stable operation waits for reviewer `4alvit` on the `release` environment.
The single-maintainer policy permits self-review and restricts deployment to `main`.
Release approval keeps its independent `can_admins_bypass=true` owner policy.
The required CI ruleset has no bypass actors: administrators must also wait for
a successful `CI gate`. Immutable version-tag rules likewise have no bypass actors.
Administrative access does not add a release CLI option to skip provenance checks,
overwrite a tag or force publication. Use the normal approval and acceptance flow.

GitHub publication, registry/store import, device delivery and production deployment
are separate steps. Prerelease, tag and generic CI events must not trigger legacy
production deployments. Follow each application's adapter runbook for delivery and
rollback using already accepted immutable artifacts.

## Write verified container deployment inputs

Projects with `container_assets` can resolve an accepted stable release to verified
registry digests with `scripts/verified_images.py`. The output must remain below
an existing trusted directory: the current working directory by default, or an
explicit `--output-root` chosen by the operator. An absolute output outside the
current directory now requires that explicit root, for example in GitHub Actions:

```bash
python3 scripts/verified_images.py --tag v1.2.3 \
  --output-root "$RUNNER_TEMP" --output "$RUNNER_TEMP/verified-images.json"
```

Relative outputs are interpreted beneath the selected root. Parent traversal and
symlink destination parents are rejected before downloading release payloads or
reading registry manifests. The output is replaced atomically without writing
through existing hard links; final symlinks and non-file destinations are rejected.
Directory descriptors keep a replaced parent pathname from redirecting the write.
Platforms without no-follow directory descriptor support fail explicitly. The
root is trusted operator configuration, not a sandbox against its owner.
