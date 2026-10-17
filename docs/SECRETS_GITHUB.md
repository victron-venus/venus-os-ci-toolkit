# Deliver recovered secrets to GitHub Actions

`github-publish` delivers approved private files to existing GitHub Actions
secrets. It supports an organization's `BOT_PAT` and its repository's
`RENOVATE_SIGNING_KEY`. The operator supplies the exact destination bindings;
the tool does not discover recipients or distribute a token across repositories.
Actions continue using their native GitHub secrets. They do not receive an OCI
administration credential or the recovery key.

The encrypted recovery catalog remains the source of recovery. Materialize a
selected recovered value into a protected file before delivery. A publication
manifest records approved destinations and byte digests, **not secret values**;
it is not a second secret database. Keep the manifest, sources and journal in
private operator storage, outside Git. Never place source digests in public logs:
a digest can reveal a low-entropy value through guessing.

## Installation and prerequisites

Use Python 3.12 or later on the existing POSIX operator environment. Install the
optional encryption dependency with its transitive hash lock:

```sh
python -m pip install --require-hashes --only-binary=:all: \
  -r tools/secret_control/requirements-github-lock.txt
```

Before publication:

1. Restore-test the encrypted source and establish custody of its separate key.
2. Validate the token/key with its issuer and confirm which consumers should use
   it. Existing GitHub secret metadata cannot establish equality with the local
   source. Retain the previous known value if rollback is required; GitHub cannot
   return a stored secret's value.
3. Agree that this operator is the sole writer during the operation. Pause any
   independent rotation that can update the same destinations.
4. Obtain fresh actor, organization and repository IDs, existing secret
   `created_at`/`updated_at`, and organization visibility/selected repository IDs
   through the GitHub API. Review these against the intended consumer configuration.
5. Store the publisher's authentication token separately in a protected file.
   The token must identify the manifest's actor, have administration access to
   the repository and, for an organization target, active organization owner
   membership. It also needs the API's relevant secret write permissions. This
   publisher credential is separate from the `BOT_PAT` being delivered.

Files must be mode `0600`, immediate parent directories `0700`. Symlinks,
multiple hard links and unsafe writable ancestors are refused. Use canonical
absolute source paths. BOT_PAT bytes must be a nonempty single line without
CR/LF; normalize the source explicitly if its original file has a trailing line
break. Signing-key bytes, including line breaks, are preserved exactly.

## Review a private manifest

This synthetic example describes both supported destinations; either entry can
be omitted. Replace the synthetic IDs, timestamps and digests with separately
reviewed metadata. Generate a new UUID for each new publication operation.

```json
{
  "schema_version": 1,
  "operation_id": "6b5b6a90-f1af-4be7-8926-57e295617a88",
  "actor": {"login": "operator", "id": 17},
  "organization": {"login": "example-org", "id": 18},
  "repository": {
    "full_name": "example-org/toolkit", "id": 19, "owner_id": 18
  },
  "secrets": [
    {
      "scope": "organization", "name": "BOT_PAT",
      "source": "/private/operator/github/BOT_PAT",
      "source_sha256": "REPLACE_WITH_REVIEWED_64_CHARACTER_SHA256",
      "expected": {
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "visibility": "all", "selected_repository_ids": []
      }
    },
    {
      "scope": "repository", "name": "RENOVATE_SIGNING_KEY",
      "source": "/private/operator/renovate-signing/id_ed25519",
      "source_sha256": "REPLACE_WITH_REVIEWED_64_CHARACTER_SHA256",
      "expected": {
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z"
      }
    }
  ]
}
```

Organization visibility is preserved, never broadened. For `selected`, list
all approved repository IDs, sorted and unique (at most 1,000); API pagination
must match completely. For `all` or `private`, use an empty list. A missing
existing secret is a refusal, including a missing repository secret inherited
from an organization: the publisher cannot create a repository override.

Hash and approve the **finished manifest bytes** independently. Supplying a hash
computed from an unreviewed manifest on the same command line is not a review.
The tool checks this approved hash, exact schemas, source hashes and permissions
before contacting GitHub. It checks all destinations before the first write,
and encrypts their exact in-memory bytes with their separate GitHub public keys.

## Preflight, apply, verify

These arguments contain paths and the manifest digest, never secret values.
Run from the reviewed checkout (or a runtime installed from its exact revision).
`--token-file` supplies authentication explicitly; ambient `GH_TOKEN`, proxies,
`gh` login state and arbitrary endpoints are not used.

```sh
python -m tools.secret_control github-publish \
  --manifest /private/operator/github/publication.json \
  --manifest-sha256 APPROVED_MANIFEST_SHA256 \
  --token-file /private/operator/auth/github-publisher
```

Preflight performs read-only API requests and validates encryption readiness.
Its output contains counts and status only. Read access and administrator
membership do not prove that a fine-grained token has secret write permission;
verify that grant independently. A later PUT can still fail after an earlier
target was updated, which is why the journal records partial outcomes.
Once approved, add the two flags:

```sh
python -m tools.secret_control github-publish \
  --manifest /private/operator/github/publication.json \
  --manifest-sha256 APPROVED_MANIFEST_SHA256 \
  --token-file /private/operator/auth/github-publisher \
  --apply --journal-dir /private/operator/github/journal
```

Create the journal directory with mode `0700` first. Keep one canonical journal
for these destinations. A local exclusive lock prevents concurrent publishers
using that journal. Before any PUT, a durable pending intent is recorded. Before
each PUT the publisher rechecks identity, ownership, the target's public key and
existing metadata/policy. HTTPS uses the fixed GitHub API origin, bounded
responses and timeouts, with no redirects or automatic retries.

A completed result says `accepted` and `consumer_verified: false`. HTTP 204 and
subsequent metadata observations prove only API acceptance. They cannot prove
payload equality, correct token permissions or a working consumer. For BOT_PAT,
verify the expected bot identity and run the authorized workflow that uses it.
For a Renovate SSH signing key, first register its public half as a **signing**
key under the intended author, run the central dry run, then verify a fresh real
Renovate commit against the expected public fingerprint and GitHub verification.
Do not retire an old signing key until those checks pass. Preserve only redacted
status/fingerprint evidence with the encrypted recovery checkpoint.

## A stopped or uncertain publication

GitHub provides an upsert API, not a compare-and-swap update. Metadata checks
reduce drift but cannot eliminate a remote race between the final GET and PUT.
If a different writer deletes the destination in that window, HTTP 201 signals
an unexpected creation and stops the batch; the tool does not delete it or retry.
Organization visibility/selection similarly requires exclusive writer ownership.

A timeout can occur after GitHub accepted a write. Pending intents and accepted
receipts make that uncertainty visible. Every attempted operation UUID is
blocked from replay, including failed operations; the tool never retries a PUT
or claims rollback. Inspect the private journal, current metadata and intended
consumer before proceeding. An earlier target may already have changed.

For a needed follow-up, first reconcile the outcome, obtain fresh metadata,
prepare and review a new manifest with a new UUID, and retain the original
journal. Do not delete intent files to force a retry. A metadata timestamp alone
is insufficient to decide which secret value is installed. Recover or reissue
through the value's owner when necessary.

References: [GitHub Actions secrets API](https://docs.github.com/en/rest/actions/secrets),
[GitHub secret encryption](https://docs.github.com/en/rest/guides/encrypting-secrets-for-the-rest-api),
[organization membership API](https://docs.github.com/en/rest/orgs/members).
