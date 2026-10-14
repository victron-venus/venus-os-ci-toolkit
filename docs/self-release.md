# Toolkit source candidates

The toolkit's source distribution contains its reusable workflows, actions,
Python command-line tools, contract fixtures and documentation. `SOURCE_VERSION`
identifies this distribution independently of consumer workflow references.
Consumers continue to use reviewed, immutable commits; downloading a source
candidate does not update a consumer or install an action into GitHub.

The source package uses an explicit file inventory. Packaging refuses untracked,
missing or symlinked inputs and writes a deterministic archive with normalized
ownership and timestamps while preserving executable bits. Acceptance checks the
exact member inventory, bytes and modes, extracts into a temporary directory,
installs the shipped hash-locked Python test dependencies and runs the existing
contract suite against that extracted source. Existing repository CI remains
required, including the Go, Python, Node, Terraform and security adapters.

The RC-only Linux Atheris campaign covers two owned, side-effect-free boundaries:

- OCI metadata: strict JSON object parsing and tar member name normalization in
  `scripts/merge_oci_archives.py`. Assertions cover deterministic parsing,
  supported JSON round trips, confinement and idempotent name normalization.
- Version policy: real path/field/value/ecosystem selectors in
  `scripts/version_plan.py`, deterministic plan construction, unchanged policy
  inputs and rejection of a changed source SHA during plan verification.

Each target receives at most 4 KiB per input and runs for 60 seconds. The seeded
inputs are synthetic. Callbacks do not open archives, extract files, invoke
commands, contact GitHub or publish releases. This exercises the actual shared
parsers, not replacement implementations. Full archive and publication behavior
remains covered separately by the existing contract fixtures and required CI.

A beta verifies source packaging and existing checks. An RC additionally requires
the bounded campaign report bound to that exact source, workflow run and attempt.
The release manifest records source and artifact identities, version receipts
and asset hashes. These checks qualify the named source candidate; they do not
prove every consumer configuration or grant approval to deploy software.
