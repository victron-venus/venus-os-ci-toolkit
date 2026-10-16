# Bootstrap the private operator environment

[Architecture](SECRETS.md) · [Catalog workflow](SECRETS_HOWTO.md) · [Recovery](SECRETS_RECOVERY.md)

Bootstrap has three independent parts: a recoverable encryption identity,
administrative access to the destination accounts, and reviewed public tooling.
None can be recovered solely from a Vault that does not yet exist.

## Choose the smallest starting path

For the [offline synthetic exercise](SECRETS_DRILL.md), use Python 3.12 or newer,
a reviewed toolkit checkout, and verified `age` / `age-keygen` binaries. No OCI
SDK, Terraform provider, cloud credentials or existing decryption identity is
needed. Install and verify dependencies before going offline; the drill does
not download or initialize anything.

For a local catalog, add an owned private operator directory and an age identity.
For HCP publication, additionally prepare the separate HCP Terraform root,
locked provider and narrowly scoped HCP access. For OCI publication, additionally
install the [locked SDK dependencies](../tools/secret_control/requirements-oci-lock.txt)
and select explicit OCI authentication. OCI topology provisioning uses its own
Terraform root and credentials. Do not provision either cloud merely to try the
local exercise.

Before using production values, name the recovery-key custodian, agree who can
retrieve the independent backup, and exercise both paths. A second directory on
the same laptop is useful for a drill but shares the laptop's failure domain.
Until those prerequisites are complete, keep examples and drills synthetic.

## 1. Choose storage and recovery custodians

Use a private directory outside every source checkout and public Git repository.
The examples use `$HOME/operator-secrets`; substitute an approved location on an
encrypted disk. Never redefine `HOME` to make a tool write somewhere else.

```sh
umask 077
OPERATOR_ROOT="$HOME/operator-secrets"
mkdir -p "$OPERATOR_ROOT/catalogs" "$OPERATOR_ROOT/receipts" "$OPERATOR_ROOT/work"
chmod 700 "$OPERATOR_ROOT" "$OPERATOR_ROOT/catalogs" "$OPERATOR_ROOT/receipts" "$OPERATOR_ROOT/work"
```

Use owner-only access for files and directories. Permissions limit other local
users; they are not encryption or backup. Do not put the plaintext catalog in a
cloud-synced folder, shared workspace, shell history or editor with unreviewed
telemetry and backup behavior.

Create at least two independent copies of encrypted catalog generations and
private asset archives, with one offline or offsite. Keep a recovery identity
and account/MFA recovery instructions accessible without OCI, HCP or the lost
operator laptop. A second bucket in the same account is not independent of that
account's suspension or compromise.

## 2. Establish an age identity

Install reviewed `age` and `age-keygen` binaries and verify their release
authenticity using the upstream instructions. Their resolved locations must
satisfy the [executable ownership checks](SECRETS_HOWTO.md). If a shared
package-manager directory is writable by a group, use a verified private binary
installation rather than changing permissions on the shared directory.
Generate a new identity only if
you are setting up a new recovery domain; do not overwrite an existing key.

```sh
umask 077
age-keygen -o "$OPERATOR_ROOT/identity.txt"
age-keygen -y "$OPERATOR_ROOT/identity.txt" > "$OPERATOR_ROOT/recipient.txt"
chmod 600 "$OPERATOR_ROOT/identity.txt" "$OPERATOR_ROOT/recipient.txt"
```

`identity.txt` is private decryption material. The recipient is public encryption
material. Back up the identity separately from its encrypted payloads and test
that the recovery copy can decrypt a synthetic catalog. Merely copying the
ciphertext is not a recovery test. This CLI uses native X25519 age recipients
(`age1...`) and identity files. Standard age encryption authenticates the
ciphertext's integrity, but possession of a public recipient does not identify
the author of a catalog; approve its provenance and generation separately.
Encryption does not hide file names, lengths or timestamps; choose generic
external filenames and keep the whole operator directory private.
[age usage and release verification](https://github.com/FiloSottile/age#readme)

## 3. Recover account access before cloud payloads

Maintain an independently recoverable route to:

- OCI administration needed to create or import the selected vault and encryption
  key, grant narrowly scoped publishing access, and inspect the account's limits.
- HCP administration needed to create or select the intended workspaces or
  variable sets and authorize variable management.
- The backup location and its decryption identity, including account recovery
  and MFA requirements.

Use the documented authentication mechanism of each selected provider. Supply
credentials through the local authenticated environment or protected provider
configuration, not command-line secret values or files committed to this toolkit.
Disable shell tracing (`set +x`) before any credential handling. Never assume an
application's runtime token has administrative recovery permissions.

## 4. Review topology separately from payloads

Read the [OCI input contract](../deploy/secret-bootstrap/oci/variables.tf) and
[HCP input contract](../deploy/secret-bootstrap/hcp/variables.tf) before
initializing or applying either module. Select the intended account, region,
compartment, workspace and variable-set bindings deliberately. Protect local
Terraform state, state backups, lock files and plan artifacts according to their
contents. An encrypted catalog does not encrypt Terraform state automatically.

Keep `deploy/secret-bootstrap/oci` and `deploy/secret-bootstrap/hcp` in separate
private working directories with separate state. The HCP preparation command
copies only the HCP root. Do not combine these modules into one mutable working
directory or give an HCP-only publication access to OCI administration credentials. The OCI module uses Terraform
`prevent_destroy` as an additional guard; this is not an account policy or cloud
lock and does not protect against out-of-band deletion or configuration/state
removal.

A cloud-writing apply is an operator action. Review its plan and resource scope;
do not use an empty state to recreate resources that still exist. Import or
restore the appropriate state first. Record new resource identifiers in a new
catalog generation rather than assuming identifiers survive reconstruction.

### If the new vault endpoint is not ready

A vault can report `ACTIVE` while its new management endpoint still returns
DNS `NXDOMAIN`. A failed apply may already have persisted the compartment and
vault in Terraform state. Preserve that state and the original failure; do not
destroy and recreate the vault merely to retry key creation. Confirm DNS
readiness, then take a fresh plan and check that it contains only the expected
pending key operation. Investigate any additional change before applying. Use
observed readiness rather than a fixed sleep as evidence that retrying is safe.

## 5. Create and review the first catalog

Follow the [local catalog workflow](SECRETS_HOWTO.md) with the synthetic example
first. Only after the recovery prerequisites and a production migration are
separately approved should real values be prepared in the protected operator
environment. For each entry, establish its owner, intended consumers,
expiry/revocation process and rotation authority in encrypted operational records.

Before first production publication, a second review should confirm that the
targets are the intended accounts and resources, the recovery copy decrypts,
and no consumer receives administrative bootstrap credentials.
