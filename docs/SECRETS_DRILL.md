# Practice recovery with synthetic data

[Architecture](SECRETS.md) · [Bootstrap](SECRETS_BOOTSTRAP.md) · [Recovery runbook](SECRETS_RECOVERY.md)

Start here before choosing production key custody or moving any real values.
The `drill` command exercises local recovery with disposable data. It does not
accept a catalog, an existing identity, an output directory or cloud credentials.
It does not connect to OCI, HCP or a backup provider. A successful local exercise
is not an off-device restore or permission to migrate production secrets.

![Offline exercise: create two synthetic generations and fresh disposable keys, simulate losing the operator key, recover with the second key, check expected refusals, then remove temporary files and emit a sanitized result. Cloud access and off-device retrieval are separate, untested boundaries.](images/secrets-drill.svg)

## Prepare once, then run offline

Use a reviewed checkout, Python 3.12 or newer, and verified `age` and
`age-keygen` executables. Install and verify them before the exercise. The
command does not install packages, initialize Terraform or download tools.
An OCI SDK and Terraform are not required for this local exercise.

Each executable's resolved path and all its ancestors must be owned by the
operator or root and must not be group- or world-writable. If a shared package
manager path fails that check, use a checksum-verified binary in an owned private
`0700` directory. Do not weaken shared directory permissions merely to pass.

From the toolkit checkout:

```sh
AGE_BIN="${AGE_BIN:-$(command -v age)}"
AGE_KEYGEN_BIN="${AGE_KEYGEN_BIN:-$(command -v age-keygen)}"

python3 -m tools.secret_control drill \
  --age "$AGE_BIN" \
  --age-keygen "$AGE_KEYGEN_BIN"
```

The command uses a new private temporary directory and fresh synthetic keys.
It creates two catalog generations, exercises recovery through the separate
recovery identity after simulated loss of the operator identity, and checks
wrong-key, tamper, rollback, digest and conflicting-output refusals. No real
key is removed. The result contains fixed check names and status, not catalog
values, private paths or key material.

Success reports `status: "passed"`, `scope: "offline-local-recovery-mechanics"`,
`syntheticOnly: true`, two generations and true results for the fixed checks.
Rollback is tested against the retained latest generation. Encryption alone
does not reject replay of an otherwise valid older ciphertext.

Treat exit status zero and a completed result as the success condition. A
refusal, traceback, interruption or absent result is not a pass. The normal
completion path removes temporary material before reporting success. Abrupt
termination or loss of power can prevent cleanup; do not infer secure erasure
from the absence of a success result.

## Check a restored synthetic catalog

`recovery-check` is a separate, read-only local check. It accepts existing private
files, so start with a synthetic catalog prepared using the
[local example](SECRETS_HOWTO.md#local-example-encrypt-validate-plan). Use a
recovery identity that was included as a recipient when that catalog was
encrypted. The exercise cannot add a recovery recipient after every usable
identity has been lost.

Before simulating loss, record the approved ciphertext SHA-256 and generation
in a separately trusted inventory. During recovery, retrieve that record through
its independent custody path. Do not calculate a new expected digest from the
possibly damaged copy and declare it trusted. The digest checks ciphertext
identity; it does not prove that a generation is current or its credentials
remain valid.

The following example expects an approved generation-1 synthetic catalog and an
already established private operator root. Supply the recovered paths and
trusted digest; do not place secret values in these variables.

```sh
: "${OPERATOR_ROOT:?Set the private operator root from the local example}"
: "${AGE_BIN:?Set the verified age executable path}"
: "${RECOVERED_CATALOG:?Set the restored synthetic ciphertext path}"
: "${RECOVERY_IDENTITY:?Set the private recovery identity path}"
: "${APPROVED_CIPHERTEXT_SHA256:?Read this from the trusted separate inventory}"

python3 -m tools.secret_control recovery-check \
  --catalog "$RECOVERED_CATALOG" \
  --identity "$RECOVERY_IDENTITY" \
  --age "$AGE_BIN" \
  --expected-sha256 "$APPROVED_CIPHERTEXT_SHA256" \
  --generation 1
```

The check compares the expected ciphertext digest before decryption, then
validates the catalog and exact expected generation. It does not write cloud
values, repair a catalog, update a local intent ledger or establish consumer
health. Keep the result private when checking anything other than synthetic data.

## Respond to the first failed boundary

- **Tool or file permission refusal:** inspect the selected executable and the
  specific private file and parent directory. Preserve an existing operator
  tree; do not apply recursive permission changes or follow a symlink to bypass
  the check.
- **Ciphertext digest mismatch:** stop before decryption. Preserve the suspect
  copy and compare another backup against the same trusted inventory. Changing
  the expected digest merely hides the mismatch.
- **Wrong identity or damaged ciphertext:** retrieve the intended identity or a
  verified alternate ciphertext. Do not remove authentication checks or print
  decrypted material for diagnosis.
- **Generation mismatch or rollback refusal:** reconcile the intended generation
  with the approved publication record. An old catalog can be authentic and
  still be unsafe to publish.
- **Conflicting materialized output:** keep both the existing file and the
  failure. Use a new approved output path or a separately reviewed replacement
  procedure; there is no overwrite switch to make a conflict disappear.

If the built-in synthetic drill fails, preserve its sanitized result and the
source/tool versions. Diagnose before running again, and retain the first
failure alongside any later success. Do not switch to real data to test a fix.

## Record what the exercise did not test

The result does not prove remote-backup availability, password-manager recovery,
MFA/account recovery, provider permissions, live consumer operation or an
acceptable recovery time for the complete system. No Dropbox restore is
performed. Keep production migration paused while recovery-key custody is
undecided. Use the [full recovery runbook](SECRETS_RECOVERY.md) to plan those
separate checks and record each result on its own evidence.
