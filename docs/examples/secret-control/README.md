# Synthetic catalog example

[catalog.synthetic.json](catalog.synthetic.json) contains no usable credential
or real destination resource. The OCI region is a public service region; every
OCID and HCP ID is deliberately illustrative. Do not publish this file.

Use it for the local encrypt, validate and plan steps in the
[catalog how-to](../../SECRETS_HOWTO.md). For a self-contained exercise, the
[offline drill](../../SECRETS_DRILL.md) generates its own synthetic catalogs and
fresh disposable keys; it does not read this file or any operator catalog. The example demonstrates:

- One stable entry with two explicitly selected consumers, one OCI secret and
  one HCP workspace variable. Sharing a value is deliberate here, not a default
  policy for unrelated consumers.
- A second entry bound to an existing HCP variable set. A target selects a
  workspace **or** a variable set, never both.
- A catalog UUID, generation and entry revisions, with values and all metadata
  encrypted together by the CLI.

Keep real operator JSON outside this checkout. Do not copy real values into this
example, replace its identifiers with production identifiers in Git, or use a
committed decryption identity. Generate a private test identity for the local
exercise, then follow the separate bootstrap and recovery procedures for actual
operations.
