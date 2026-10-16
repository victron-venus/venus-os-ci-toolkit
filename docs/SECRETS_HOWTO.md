# Use an encrypted catalog

[Architecture](SECRETS.md) · [Bootstrap](SECRETS_BOOTSTRAP.md) · [Recovery](SECRETS_RECOVERY.md)

Run the commands below from a reviewed toolkit checkout with the documented
Python 3.12+ and tool dependencies available. The cloud API paths additionally
require the optional [hash-locked OCI dependencies](../tools/secret_control/requirements-oci-lock.txt).
Provider versions are fixed in the separate
[OCI](../deploy/secret-bootstrap/oci/versions.tf) and
[HCP](../deploy/secret-bootstrap/hcp/versions.tf) roots; install dependencies
through a reviewed process before publishing, using `--require-hashes` for the
Python dependency lock. The first section uses only synthetic
data and local files. The later cloud sections are separate, deliberate operator
actions; never publish the example destinations.

The CLI checks each resolved executable and all its parent directories: they
must be owned by the operator or root and must not be group- or world-writable.
A package-manager installation can fail this check even when its binary is
executable. Do not change global package-manager permissions to bypass it.
Install a checksum-verified pinned binary in an owned `0700` private `bin`
directory instead, and supply its explicit `--age` or `--terraform` path. The
examples honor preselected `AGE_BIN`, `AGE_KEYGEN_BIN` and `TERRAFORM_BIN` values.

## Local example: encrypt, validate, plan

Complete the [bootstrap storage and age identity steps](SECRETS_BOOTSTRAP.md)
first. Keep the private operator root outside the toolkit checkout. Use a new
output filename for every approved catalog generation.

```sh
umask 077
OPERATOR_ROOT="$HOME/operator-secrets"
AGE_BIN="${AGE_BIN:-$(command -v age)}"
AGE_KEYGEN_BIN="${AGE_KEYGEN_BIN:-$(command -v age-keygen)}"
AGE_RECIPIENT="$("$AGE_KEYGEN_BIN" -y "$OPERATOR_ROOT/identity.txt")"

python3 -m tools.secret_control encrypt --age "$AGE_BIN" \
  --recipient "$AGE_RECIPIENT" \
  --output "$OPERATOR_ROOT/catalogs/demo-g1.json.age" \
  < docs/examples/secret-control/catalog.synthetic.json

python3 -m tools.secret_control validate --age "$AGE_BIN" \
  --catalog "$OPERATOR_ROOT/catalogs/demo-g1.json.age" \
  --identity "$OPERATOR_ROOT/identity.txt"

python3 -m tools.secret_control plan --age "$AGE_BIN" \
  --catalog "$OPERATOR_ROOT/catalogs/demo-g1.json.age" \
  --identity "$OPERATOR_ROOT/identity.txt"
```

`plan` decrypts and inspects the local catalog; it does not contact either cloud.
Keep even redacted plan metadata private. Run `python3 -m tools.secret_control
--help` and the selected subcommand's `--help` when checking the exact interface.

For production recovery, encrypt to the approved operator recipient and, when
using separate recovery keys, add another `--recipient "$RECOVERY_RECIPIENT"`.
Obtain that public recipient through an authenticated custody process and keep
its private identity separately. Test decryption with the recovery identity;
adding a recipient string alone does not establish a working recovery path.

For a real catalog, replace the synthetic input with an approved protected file
or secure producer on standard input. Catalog and identity files must be owned
regular files with mode `0600`, beneath owner-only `0700` parents; symlinks and
hard-linked files are refused. Do not put a plaintext JSON object, token
or password in command arguments. Avoid shell tracing, terminal transcripts and
unreviewed editor backup files. Whole-file encryption protects the stored catalog;
decryption and publication still require plaintext in trusted process memory.

## Catalog contract

The root contains `schema_version`, `catalog_id`, `generation` and `entries`.
Schema version is `1`, catalog ID is a canonical UUID, and generation is a
positive integer. Each entry has a stable `id`, positive `revision`, string
`value`, and explicit `targets`.

An OCI target names `type: "oci"`, region, compartment, vault, key and secret
name. An HCP target names `type: "hcp"`, variable key and category, plus exactly
one of `workspace_id` or `variable_set_id`. The [synthetic example](examples/secret-control/catalog.synthetic.json)
shows both HCP binding shapes. A variable-set binding uses an existing selected
set; it does not grant the publisher authority to create a global set.

The implementation limits each value to 16 KiB and the catalog to 2 MiB. These
local bounds do not replace destination quotas or establish that a deployment
fits the OCI free allowance. Do not split a large private asset across secret
entries; use an independently encrypted asset archive instead.

Do not add arbitrary operational fields to this schema. Keep expiry, issuer,
human approval and recovery instructions in a separate encrypted operator record.
All target identifiers in a real catalog are private metadata encrypted with the
values. A change to a target can disclose a value to a new consumer and requires
review even if its value is unchanged.

## A new generation

Prepare the next approved JSON input privately. Preserve `catalog_id` and stable
entry IDs, increase the generation, and advance the revisions of changed
entries according to the validator's contract. Use the previous encrypted
catalog as the explicit comparison input. The generation advances by exactly
one; each existing entry advances its revision by exactly one if its value or
targets change, and retains its revision otherwise. New entries start at revision
one. Removing entries is not supported:


```sh
python3 -m tools.secret_control encrypt --age "$AGE_BIN" \
  --recipient "$AGE_RECIPIENT" \
  --previous "$OPERATOR_ROOT/catalogs/production-g1.json.age" \
  --identity "$OPERATOR_ROOT/identity.txt" \
  --output "$OPERATOR_ROOT/catalogs/production-g2.json.age" \
  < "$OPERATOR_ROOT/work/approved-g2.json"
```

This example assumes generation 1 already exists and generation 2 was reviewed.
The tool does not recover an issuer's lost password or make a revoked credential
valid. Preserve prior ciphertext and publication outcomes for reconciliation;
follow your retention procedure for temporary plaintext files.

## Publish to OCI explicitly

Provision or select the intended resources with the separate
[OCI Terraform root](../deploy/secret-bootstrap/oci/main.tf). Use least-privilege
credentials for publication. The publisher accepts explicit API-key configuration
or instance-principal authentication. The example below selects an API-key
configuration file; it does not contain or print the key.

```sh
OCI_CONFIG="$OPERATOR_ROOT/oci-config"

# Contacts OCI for read-only preflight; does not publish values.
python3 -m tools.secret_control oci-publish --age "$AGE_BIN" \
  --catalog "$OPERATOR_ROOT/catalogs/production-g2.json.age" \
  --identity "$OPERATOR_ROOT/identity.txt" \
  --auth api-key --config "$OCI_CONFIG" --profile DEFAULT

# Cloud write: run only after reviewing generation 2 and its destinations.
python3 -m tools.secret_control oci-publish --age "$AGE_BIN" \
  --catalog "$OPERATOR_ROOT/catalogs/production-g2.json.age" \
  --identity "$OPERATOR_ROOT/identity.txt" \
  --auth api-key --config "$OCI_CONFIG" --profile DEFAULT \
  --apply --generation 2
```

Without `--apply`, this command performs remote read-only preflight. An apply
checks all selected OCI targets before sequential writes. The implementation
does not adopt unrelated same-name secrets or automatically delete old entries.
An interruption can leave a partial result; follow the
[partial-publication runbook](SECRETS_OPERATIONS.md#publication-stopped-or-its-result-is-unknown)
before retrying. For unchanged bytes, the same revision is a no-op; a higher
revision advances the managed metadata through an ETag-guarded update without
creating another payload version. Accepted writes receive bounded read-only
completion checks; this does not retry the write. Changing the catalog does not
reload existing applications.

## Prepare and publish HCP variables

Use a new private working directory for the HCP-only Terraform root. It must not
share the OCI root's state. Configure the provider's `TFE_TOKEN` through an
approved private credential loader, with only the permissions needed for the
selected workspaces or variable sets. Never pass that token as a CLI argument.

```sh
TERRAFORM_BIN="${TERRAFORM_BIN:-$(command -v terraform)}"
HCP_DIR="$OPERATOR_ROOT/work/hcp"

python3 -m tools.secret_control hcp-prepare --terraform-dir "$HCP_DIR"

# Explicit dependency initialization; review provider versions and lock changes.
"$TERRAFORM_BIN" -chdir="$HCP_DIR" init

python3 -m tools.secret_control hcp-plan --age "$AGE_BIN" --terraform "$TERRAFORM_BIN" \
  --catalog "$OPERATOR_ROOT/catalogs/production-g2.json.age" \
  --identity "$OPERATOR_ROOT/identity.txt" \
  --terraform-dir "$HCP_DIR"

# Cloud write: run only after independent review of this generation and its targets.
python3 -m tools.secret_control hcp-apply --age "$AGE_BIN" --terraform "$TERRAFORM_BIN" \
  --catalog "$OPERATOR_ROOT/catalogs/production-g2.json.age" \
  --identity "$OPERATOR_ROOT/identity.txt" \
  --terraform-dir "$HCP_DIR" --generation 2
```

Unlike the catalog's local `plan`, `hcp-plan` runs Terraform and may contact HCP.
The wrapper withholds raw Terraform output to prevent secret diagnostics from
reaching the terminal. Its success reports a completed planning operation, not
a human-readable plan or approval. Independently review the catalog generation,
target bindings and module before authorizing an apply.
Preparation copies trusted HCP configuration only; provider initialization is
an explicit dependency download step. Keep the resulting state, lock file and
operation records private. Read the [HCP input contract](../deploy/secret-bootstrap/hcp/variables.tf)
for exact version and input requirements.

Preserve the HCP state together with `.secret-control-ledger-key` and every
`.secret-control-intent-*.json` file in an encrypted backup. The local HMAC ledger
binds intended values and revisions without storing plain value hashes. It is
not a remote readback proof: HCP write-only values cannot be compared with it.
Do not copy only the state file, delete the ledger to bypass a refusal, or assume
an intent file proves its apply completed.

The wrapper requires its exact bundled configuration/provider lock, a local
backend, the default workspace and only its listed managed variable resources.
Changing or removing an HCP destination needs a separate migration plan; the
publisher does not silently delete or adopt variables. Keep unrelated Terraform
resources in their own roots and state.

HCP values travel through an ephemeral, sensitive JSON string that Terraform
decodes as data. Literal `${` and `%{` sequences are supported; use ordinary JSON
escaping in the catalog, without adding Terraform template escapes.

Values with any HCP target must already use **Unicode NFC**. Decomposed or other
non-NFC values are rejected before cloud changes or local intent-ledger writes;
the tool never normalizes them. This avoids silently changing a credential's
value because Terraform normalizes strings. Obtain an NFC-compatible value from
its issuer if necessary, rather than rewriting an existing credential. This
restriction is specific to HCP targets; OCI-only values retain their existing
validation and byte-preserving publication contract.
[Terraform string normalization](https://developer.hashicorp.com/terraform/language/functions/base64encode)

HCP targets with category `env` also require a single-line value: carriage
returns and line feeds are rejected before cloud changes or local intent-ledger
writes. The tool does not strip line breaks or automatically encode credentials.
Multiline values remain supported for HCP category `terraform` and OCI targets.
If a consumer needs multiline material, choose an explicit consumer-supported
Terraform-variable or file contract rather than silently changing the value.

Write-only values and ephemeral inputs keep the payload out of ordinary
Terraform state. That does not remove every sensitive identifier from state or
prove consumers can use the value. HCP does not return sensitive variable values
for backup; the encrypted catalog remains the recovery source.

## Fetch one immutable OCI version

Use an explicitly selected secret version and an independently approved digest
when materializing a consumer input. Do not obtain the expected digest from the
same untrusted response and call it approval.

`fetch` requires `--region`, `--secret-id`, `--version-number`,
`--expected-sha256`, `--output` and the selected authentication parameters.
Keep the destination under an owner-only parent. An existing identical protected
file is a no-op; a conflicting file is refused rather than overwritten. Treat
the result as plaintext private data and do not stage it into public Git.

The fetch operation is a materialization primitive, not a consumer deployment,
service restart or authorization to change a running system.
