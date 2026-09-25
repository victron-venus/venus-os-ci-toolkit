# Verified vendor delivery

`.github/workflows/vendor-update.yml` delivers compiled libraries to independently
versioned consumers. Call it after the producer's build and integration jobs, with
both the workflow `uses` and `toolkit-revision` pinned to the same reviewed full
toolkit commit. The producer runs on GitHub-hosted Ubuntu; no shared filesystem,
self-hosted runner or consumer-side compilation of the library is required.

The caller supplies the exact `upload-artifact` ID and SHA-256 digest, a complete
archive filename list, qualifying job names and the subset of files to copy under
one destination prefix. The manifest schema is the OttPlay distribution schema 1:
artifact sizes/hashes plus a sorted source-file hash map and its aggregate hash.
Only successful `push` or manual runs on `main` may publish. PRs and merge-queue
builds can test their candidates but cannot access delivery credentials.

## Authentication and signing

Forward a cross-repository token as `DELIVERY_TOKEN`, with source read access and
consumer contents/pull-request write access. The helper reads producer Actions
metadata with the producer's `GITHUB_TOKEN`; it uses the cross-repository token
only for the consumer. The token's account authors the PR and triggers normal
consumer CI. Keep independent CODEOWNER/review requirements in place.

Supply a dedicated SSH private signing key as `DELIVERY_SIGNING_KEY` and set the
matching `committer-name`/`committer-email`. Register only its public key as a
GitHub signing key on the account owning that email. The private key is written
to runner temporary storage with restrictive permissions and removed even after
failure. Every pushed commit is created by Git, locally verified against that
key's fingerprint, and required to be verified by GitHub before opening the PR.

## Publication contract

The helper checks the source repository, workflow, commit, run attempt and every
qualifying job before reading the archive. It verifies the immutable archive
digest, exact file inventory, manifest hashes and receipt files against the
producer Git objects. ZIP traversal, symlinks, duplicate files, extra files and
oversized entries are rejected. The source subtree must still equal current
`main`; qualification is checked again before publication.

Consumer checkouts use the current default-branch commit, including when producer
and consumer are the same repository. Only explicitly allowlisted vendor paths
can change. Consumer hooks, filters and build commands are never executed with
delivery credentials. The script creates a signed Git tree using a temporary
index, leaving the checkout's files and index untouched.

Already matching consumer files are a no-op. A branch keyed by the complete
manifest hash permits retries to reuse exactly the same signed generated commit
and PR. Foreign or modified branches, unexpected parents/paths, closed PRs and
changed consumer bases fail with no force push. A newer source subtree refuses
an old run; dispatch a new producer run instead. If the consumer base advances
during publication, retry with a fresh checkout.

PRs are **drafts**, with the source commit, run URL, artifact ID/digest and receipt
hash in their description. They are not approved or merged by this workflow.
Review the generated diff and consumer CI before marking ready and merging. If
an approval workflow normally uses the PR author's token, configure an independent
reviewer or use manual review; the author cannot approve its own PR.

## Operation

The producer owns activation and its target list. OttPlay gates delivery with
`CORE_DELIVERY_ENABLED=true` and calls the workflow for FOSS, Android and its
in-repository FOSS2 client. Turning that variable off stops new deliveries without
affecting builds or committed consumer artifacts.

Actions archives are temporary transport. Once a PR is merged, consumer builds
and releases use Git-tracked vendor files and need neither the producer repository
nor an unexpired archive. Rerun all producer jobs if its transport artifact has
expired. A vendor PR does not itself publish a release: the consumer's existing
release policy decides what happens after merge.

Local validation: `python3 -m unittest discover -s tests -p 'test_vendor_update.py' -v`
and `actionlint .github/workflows/vendor-update.yml`. `--stage-only` performs the
same read-only remote qualification and prepares a signed local Git object, but
does not push or open a PR; it requires dedicated clean checkouts and configured
signing just like the workflow.
