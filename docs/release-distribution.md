# Compact stable distribution manifests

Compact stable metadata is an explicit per-repository opt-in for byte promotion.
Set the current driver's top-level `.release-policy.json` field to:

```json
"stable_distribution": "rc-payloads-v1"
```

An absent field preserves the existing full-manifest copy behavior. Unknown,
null or boolean values fail validation. The accepted historical RC policy does
not need this field. This option does not rebuild or restamp packages, change
the accepted source tag, allocate a replacement build number, or relax gates.
Final-build releases and existing schema-1 releases retain their existing paths.

This compact profile currently rejects full-inventory signing (`sign_releases`
or an enforced full release-signature mode). That signature binds the complete
inventory, including receipts omitted from compact shipping; it must not be
silently reinterpreted. Ordinary updater/package `.sig` files are payloads and
remain byte-for-byte. This restriction does not disable or weaken those checks.

An opted-in stable byte promotion emits a new schema-2 manifest with
`kind: "rc-payloads-v1"`. It lists each shipped payload's exact size and SHA-256
and binds the complete schema-1 RC manifest by its raw digest, repository, RC tag,
source, run and attempt. Only declared, verified build/validation receipts and
the declared exact fuzz report are omitted from the stable distribution. Runtime
JSON, updater feeds, TV metadata, checksums and signatures remain unchanged. No
extension filter, arbitrary exclusion list or manifest-supplied download URL is
accepted. The new stable manifest is metadata, not a copied RC manifest or an
independent promotion signature.

A file named `fuzz-report.json` remains a shipped payload when the RC does not
declare fuzz validation. Its name alone does not make it disposable evidence.
Declared fuzz validation must include its authenticated receipt, profile and
report; an incomplete declaration is rejected.

Promotion still downloads and validates the entire RC, including omitted files,
before selecting shipped payloads. Receipt, embedded metadata and signature
checks use complete RC staging. Stable readers resolve the authenticated RC and
recompute the deterministic projection before accepting the exact stable
inventory. Old schema-1 stable releases remain supported and are not rewritten.

Keep the referenced RC release, manifest and all evidence assets available while
a stable release depends on them. The existing immutable Actions evidence must
also remain available and unexpired; a public RC manifest alone does not replace
that check. Missing, altered or expired evidence fails closed. Retention tooling
must not delete referenced RCs. No retention policy or release deletion is
changed by this feature.

## Narrow consumer migration

The complete renderer can install the helper for new versioned consumers. For
the independently reviewed existing FOSS release engine, use only the targeted
mode from the reviewed canonical toolkit checkout:

```sh
python3 scripts/install_release.py /path/to/consumer --release-distribution-only --check
python3 scripts/install_release.py /path/to/consumer --release-distribution-only
python3 scripts/install_release.py /path/to/consumer --release-distribution-only --check
```

This mode admits only the exact known consumer source shapes recorded in the
reviewed migration delta. It applies scripts, generated tests and documentation
only. It preserves the complete `.github/workflows` tree, existing bounded
readback/transport behavior, signing restrictions, native policies and the
consumer's partial toolkit revision. Optional modules that were not installed
remain absent. It does not enable `stable_distribution`; review that policy
change separately. Do not invoke the broad renderer on a consumer whose
accepted RC depends on unchanged workflows.

All preimages are checked before writes. New contents are staged and synced,
then each destination is replaced by an atomic rename; caught write failures
roll back already replaced files. A process crash is not a multi-file atomic
transaction: mixed states are rejected on the next invocation and require
explicit review. Unknown drift, symlinks, hard links or partly migrated bundles
are refused. A second successful invocation makes no changes.

Record the reviewed canonical commit, selected file hashes and targeted diff in
the consumer change. This is a narrow backport, not a whole-toolkit upgrade.
Verify the entire workflow tree against the accepted RC source before dispatch.
The scripts-only driver may be newer than the accepted RC, but all existing
workflow compatibility, ancestry, source-policy, attempt, gate, ledger and
publication checks still apply. The stable tag continues to identify the RC
source; the driver SHA is not a replacement application source.
