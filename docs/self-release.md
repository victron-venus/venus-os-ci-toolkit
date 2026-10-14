# Toolkit source candidates

The toolkit's source distribution contains its reusable workflows, actions,
Python command-line tools, contract fixtures and documentation. `SOURCE_VERSION`
identifies this distribution independently of consumer workflow references.
Consumers continue to use reviewed, immutable commits; downloading a source
candidate does not update a consumer or install an action into GitHub.

## Use the source distribution

Download the candidate's source tarball and `SHA256SUMS` from the same release,
check the archive hash, and extract it into a new directory. The Python tools
require Python 3.12 or newer. Create an environment and install the included
generator dependencies before running the contracts:

```sh
cd venus-os-ci-toolkit
python3.12 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: \
  -r .github/requirements-generator.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Some contract fixtures also invoke Git, OpenSSH, the GitHub CLI, Go, Node or uv.
The required repository CI supplies these tools and exercises the supported
platform matrix. A local run that skips an unavailable-tool test is not evidence
that that external-tool integration passed. Source archive acceptance installs
Python dependencies in a fresh temporary environment and uses the CI toolchain.

The source archive is not a Python wheel and is not installed with `pip install
venus-os-ci-toolkit`. Invoke its documented scripts from the extracted tree, or
adopt workflows using an independently reviewed immutable GitHub commit.

## Candidate acceptance

The source package uses an explicit file inventory. Packaging refuses untracked,
missing or symlinked inputs and writes a deterministic archive with normalized
ownership and timestamps while preserving executable bits. Acceptance checks the
exact member inventory, bytes and modes, extracts into a temporary directory,
installs the shipped hash-locked Python test dependencies and runs the existing
contract suite against that extracted source. Existing repository CI remains
required, including the Go, Python, Node, Terraform and security adapters.

## RC-only parser campaign

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
