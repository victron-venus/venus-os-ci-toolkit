# Rotation and outage runbooks

[Architecture](SECRETS.md) · [Catalog workflow](SECRETS_HOWTO.md) · [Recovery](SECRETS_RECOVERY.md)

The publisher distributes selected values. It does not rotate credentials at
their issuer, reload every consumer, provide a distributed transaction, or
decide which conflicting version is authoritative. Those decisions belong to
the operator responsible for the entry.

## Planned credential rotation

1. Identify the issuer, entry ID, all consumers and the allowed overlap period.
   Check whether both old and new credentials can remain valid during cutover.
   If they cannot, schedule the required interruption explicitly.
2. Obtain the new credential from the issuer through its normal secure process.
   Do not revoke the old credential before an approved cutover point. Confirm
   the new credential's scope and expiry without recording its value in a ticket.
3. Update the protected catalog input, increase that entry's revision and the
   catalog generation, then encrypt to a new file. Preserve the previous
   ciphertext and its publication record. Keep revision numbers monotonic;
   restoring old bytes is not a new rotation unless the operator approves it.
4. Review the local plan, target accounts and changed entries. A destination
   change deserves its own review even if the value is unchanged.
5. Publish separately to each intended destination. Keep the exact outcome of
   each attempt. Stop on ambiguous results rather than blindly replaying an
   entire generation.
6. Activate or reload consumers through their own documented deployment process.
   Verify a minimal safe operation using the new credential. Watch for clients
   that cache old values or fetch a fixed secret version.
7. Revoke the old credential at the issuer after the agreed overlap. Confirm
   revocation, update the private operational record and back up the completed
   generation independently.

If the issuer rotates autonomously, an old local catalog can become dangerous.
Agree which process exports the new value and commits its approved encrypted
generation, or remove that entry from this publisher's management. There is no
automatic bidirectional conflict resolution.

## Rotate the catalog encryption identity

Generate a replacement identity in a trusted environment and test its separate
recovery copy. Re-encrypt approved catalogs and asset archives to the intended
recipients; check that the new identity can decrypt the resulting copies before
retiring the old identity. Treat this as an operator recovery operation, not an
application-secret rotation.

Re-encryption does not revoke access to ciphertext already copied with an old
key. If that key was compromised, assess every exposed catalog and rotate the
application credentials it contained. Retained backups may legitimately need
an old key under restricted custody; document that retention deliberately.

## Publication stopped or its result is unknown

**Trigger:** timeout, process interruption, provider error, unexpected revision,
or only one destination succeeds.

Stop and preserve the local outcome records. A timeout after a write is not
proof that no write occurred. Identify the last completed operation and inspect
the selected destination with an approved read-only method. Compare its managed
identity and revision against the intended entry. Do not adopt an unrelated
same-name secret or advance a revision solely to bypass a conflict.

HCP sensitive values are not readable for comparison. A matching variable name
or revision record is not plaintext proof. Use a separately authorized consumer
check or a controlled re-publication decision. Do not copy unknown cloud values
back into the catalog automatically.

After reconciling a successful HCP publication, repeat `hcp-plan` against the
same approved generation and require `changes: false`. A result of
`changes: true` needs investigation, not an automatic retry. This convergence
check does not replace the consumer check for HCP's write-only values.

Resume only after choosing the intended generation and reconciling partial
results. This tool does not automatically roll back a successful destination
when another fails. Record that decision and rerun only through the supported,
reviewed workflow; do not edit local receipts to make an attempt appear complete.

## OCI or HCP is unavailable

Leave the encrypted catalog and independent backups intact. Pause publication
and rotation if consumers cannot tolerate a partial cutover. Existing consumers
may continue only if their actual caching and credential lifetimes permit it;
the publisher makes no offline-runtime availability guarantee.

Do not fail over by putting plaintext values in public repository variables,
workflow files, issue comments or CI artifacts. If a replacement destination is
needed, use the recovery runbook, review its new bindings, and activate consumers
through their separate deployment process.

## Operator laptop or recovery key is lost

Use the independent recovery kit on a clean machine. If the laptop might be
compromised, revoke its cloud publishing access and assess exposure of decrypted
data. Restore the age identity only through its approved separate custody path.
If every usable identity is lost, ciphertext is not recoverable: recreate the
catalog from issuers and other independently authorized records, then rotate
credentials as needed.

## Suspected exposure

Contain access, preserve evidence privately, identify affected generations and
revoke or rotate credentials at their issuers. Removing a value from the current
catalog does not remove it from logs, Terraform state history, backups or a
recipient's copy. Work with the owners of those retention systems. Do not post
raw tokens, decrypted catalogs or private identifiers in public support reports.

## Safe operational records

Keep only the metadata needed to reconcile a publication: catalog generation,
entry revision, destination, tool/source version, timestamp and observed
outcome. Even identifiers and plan output can reveal private topology, so keep
receipts in the private operator root. Never record plaintext values, encoded
equivalents or unkeyed hashes of low-entropy credentials as public proof.

## Refusal before publication

For a local-only diagnosis, start with `validate` or the
[synthetic recovery drill](SECRETS_DRILL.md). `plan` is local, but `oci-publish`
without `--apply` and `hcp-plan` can still contact their destination. Select the
command according to the access you intend to exercise.

For an executable or private-file ownership refusal, inspect the selected path
and its parent permissions. Use a verified private binary if a shared package
manager tree is group-writable. Do not disable ownership checks, recursively
change an existing operator tree, or place an identity in the public checkout.

For an HCP value refusal, check the documented
[NFC and single-line environment-variable requirements](SECRETS_HOWTO.md#prepare-and-publish-hcp-variables).
Never normalize, strip newlines or encode an existing credential solely to pass
validation. Obtain an issuer-approved replacement or use an explicit
consumer-supported Terraform-variable or file contract. Literal `${` and `%{`
are data; adding Terraform interpolation escapes changes the intended value.

A stale generation, conflicting intent ledger or unexpected Terraform state is
a reconciliation problem. Keep state, the ledger key and intent files together.
Restore a known consistent set and compare the approved generation; deleting
one component to force a run destroys the evidence needed for recovery.

Keep the HCP state and workspace-selector paths as the expected private regular
files. A dangling symlink is still a conflicting path, not an absent state file.
Do not bypass a refusal by replacing it with an empty state; determine what the
original path should contain and restore or reconcile the intended state first.

Use the verified executable directly rather than an unreviewed wrapper that may
ignore or truncate its input. An input-delivery failure or external-tool timeout
must not be treated as successful decryption or publication. Preserve the failed
attempt; if the command could have written to a cloud, inspect the destination
read-only before deciding whether to retry. Local process cleanup does not undo
a remote write.

## Repeated ETag conflicts or delayed OCI readiness

A preflight-to-write ETag change means the destination changed while it was
being inspected. Stop, preserve the attempt and perform a fresh read-only
reconciliation. If another publisher is active, establish one rotation owner
before trying again. Do not remove the ETag guard or loop writes until they pass.
A previous confirmed write may already be complete even if a later repeat fails.

For a newly created vault whose management endpoint is not yet resolvable,
follow the [bootstrap readiness procedure](SECRETS_BOOTSTRAP.md#if-the-new-vault-endpoint-is-not-ready).
Preserve partial Terraform state and check the next plan; recreating the vault
can turn a temporary readiness delay into a different recovery problem.
