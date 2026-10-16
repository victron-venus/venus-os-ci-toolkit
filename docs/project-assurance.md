# Architecture, security evidence and roadmap

## Scope and getting started

The toolkit contains reusable GitHub Actions workflows and composite actions,
Python release/generation/validation tools, a Go Scorecard adapter, shell and
JavaScript workflow fragments, and optional operator tools and Terraform
examples. Begin with [CONTRIBUTING](../CONTRIBUTING.md),
[consumer installation/interfaces](CONSUMER_GUIDE.md) and
[source installation/acceptance](self-release.md). The toolkit runs on developer
or CI hosts; it does not install itself on a Venus OS device.

## Architecture and trust boundaries

Policies feed `scripts/install_release.py` and renderer modules that generate
reviewable consumer workflows. `workflow_contracts.py` checks generated
interfaces against immutable references. `release_control.py`, `version_plan.py`
and provenance helpers bind source, channel, version, artifact identities and
acceptance evidence. `toolkit_source.py` verifies every declared archive member
before extraction. The Go adapter reports Scorecard evidence independently of
application tests. Pull-request metadata automation does not execute untrusted
PR code with its approval credentials.

Optional `tools/secret_control` keeps an age-encrypted local catalog separate
from public configuration and publishes only explicit OCI/HCP destinations.
It bounds untrusted inputs, verifies lineage and concurrency, pins immutable
secret versions, and handles private files with owner/mode/no-symlink checks.
The [secret control-plane guide](SECRETS.md) explains assumptions, rotation and
recovery. A documented recovery procedure does **not** mean a second project
maintainer has been designated or exercised repository access recovery.

## Cryptography and supported configuration

Cryptographic operations use maintained external implementations: Python/OpenSSL,
Go TLS, GnuPG/OpenSSH, age and vendor SDK/providers. SHA-256 identifies artifact
content; SHA-1 appears only where the Git object format requires a legacy object
identifier, not as a new authentication mechanism. Signing-key preflight rejects
RSA below 2048 bits and unsupported SSH/OpenPGP curves before contacting GitHub.
The Ed448 regression uses an explicitly public synthetic key and verifies a real
signature and rejection of tampered content; it is never an operational key.

[The transport profile](RELEASE_TLS_PROFILE.md) defines a complete conservative
configuration for generation, validation and GitHub source-release operations:
verified GitHub CLI/Go with `GODEBUG=fips140=on`, and `VENUS_STRICT_TLS=1` for the
two toolkit-owned Python HTTPS callers. The Python profile retains ordinary
chain/hostname verification, requires OpenSSL 3, TLS 1.2+, forward-secret cipher
selection and security level at least 3. It prohibits plaintext redirects and
cross-origin forwarding of Authorization. Normal compatibility defaults remain
available outside this opt-in profile.

Optional OCI/HCP publication, container publication with skopeo, cluster runner
operation, and arbitrary consumer-deployed workflows are separate integrations.
They are not enabled by the source-release profile. Their default SDK/tool TLS
checks remain enabled, but the exact certificate-key-strength boundary has not
been demonstrated for every runtime or endpoint. No profile claiming that
coverage is made here. Before extending the profile, qualify those real
connections, API/signing keys and trust stores rather than treating a local unit
test or a passing provider plan as evidence of remote TLS policy.

There is no external-user password-verifier database. Private operator catalogs
contain credentials needed to authenticate to other systems; encryption and
file-access controls protect them. That is distinct from password hashing.
Cryptographic randomness comes from maintained libraries and operating-system
CSPRNGs (`secrets`, age, OpenSSL, GnuPG and Go); test input generators are not used
for operational credentials.

## Test and analysis evidence

`bash scripts/ci.sh` runs the unit/contract suite, actionlint and Ruff. Required
CI also exercises the Go adapter, downloaded Python actions, JavaScript,
Terraform validation and synthetic age recovery on supported platforms.
Source-archive acceptance installs the shipped hash-locked dependencies and runs
the same contract suite against the extracted inventory. A test skipped because
an optional external tool is absent does not prove that integration; consult its
separate required CI job. CodeQL covers Python, Actions and Go; Trivy, Bandit,
dependency updates and provider-specific checks supply independent feedback.
Investigate actual findings, not only the workflow conclusion.

RC-only Atheris campaigns target owned OCI/version-policy parsers as documented
in [source qualification](self-release.md); fixed-corpus replay and unit tests
are not mutation campaigns. The live run/source identity is required for any
claim about a particular candidate. None of these tests establishes device or
production deployment acceptance.

## Roadmap: October 2026 through October 2027

Over the next twelve months, priorities are complete maintained-source coverage measurement; qualification
of optional operator/network integrations; preserving narrow, explainable
static-analysis exceptions; broader malformed-input and partial-failure tests;
and a verified second-person repository/CI/release recovery arrangement.

The project will not infer physical-device acceptance from CI, operate consumer
production credentials without an explicit operator action, or claim that a
source-only archive qualifies external services.

[The changelog](../CHANGELOG.md) and public PR history record delivered changes.
The existing source archive is normalized, but no claim is made that every
external compiler/toolchain produces bit-identical artifacts. Full Silver
coverage, universal accessibility/internationalization, comprehensive input
validation and a complete security assurance case remain unproved. A bot does
not replace accountable human maintainership. See [governance](../GOVERNANCE.md)
for the confirmed absence of a backup maintainer and one-week access recovery.

[The official assessment](https://www.bestpractices.dev/en/projects/15552) is the
source of truth for the awarded badge and criterion-by-criterion status.
