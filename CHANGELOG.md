# Changelog

## [0.1.4] - 2026-10-17

- Add operator-controlled GitHub secret delivery with explicit organization and
  repository ownership, preserved organization visibility, pinned source bytes,
  sealed-box encryption and durable journals for partial or uncertain writes.

- Report whether an HCP secret plan has changes without exposing Terraform
  output, and document convergence checks before consumer cutover.

- Allow 35 minutes for each browser target, covering cold toolchain and browser
  setup plus application acceptance and evidence upload. Planner, release-verdict
  and application test timeouts are unchanged.
- Add an offline synthetic recovery drill and independently checkpointed catalog
  verification, with operator instructions and a recovery-exercise diagram.
- Reject unsafe path ancestors and dangling HCP state links; bound external
  process groups and reject incomplete input delivery.
- Add an opt-in strict OpenSSL 3 transport profile for owned Python HTTPS calls,
  preserving stricter platform defaults and rejecting credential redirects.
- Document project governance, complete source-release security scope and
  OpenSSF evidence with an official live assessment badge.

- Make Ed448 signing-preflight regression tests deterministic with an explicitly
  public test key, retaining real signing, verification and tamper rejection.
- Allow 15 minutes for browser planning, including cold Node 22 installation on
  newly created runners. Browser execution and release-verdict budgets are unchanged.
- Add an age-encrypted recovery catalog, explicit OCI secret publication with
  revision and concurrency checks, and immutable verified private-file delivery.
- Add isolated local Terraform roots for OCI topology and HCP write-only
  variables; include recovery, rotation and partial-failure runbooks.
- Test real encryption recovery on Linux and macOS with Python 3.12 and 3.14.
  Adoption is opt-in: existing consumers and their credentials are unchanged.


- Install both shipped dependency locks before source-archive acceptance so the
  secret adapter contracts run in the isolated test environment.
- Qualify the native GitHub CLI and patched Cosign release tooling; validate the
  current Scorecard report and explicit Ubuntu 26.04 browser targets.
- Preserve literal HCP variable descriptions and explicitly bind repository
  Dependabot secret delivery to its configured destination.

### Upgrade

Download the source archive and run its documented acceptance checks. Workflow
consumers retain their existing immutable pins until a separate reviewed update.
Secret-control adoption is explicit: preserve an independently recoverable key,
verify the encrypted backup and review exact destinations before publication.
Existing services and infrastructure are not deployed by this source release.

### Security

Secret delivery now validates destination ownership, ciphertext provenance and
partial-write receipts. The TLS and release-signing changes retain verification
and fail closed on mismatched evidence. Archive acceptance includes the real
secret-adapter contracts. No deployment, device or remote backup acceptance is
claimed by these source checks.

## [0.1.3] - 2026-10-16

- Accept explicitly selected Intel macOS runners in the complete browser release
  matrix while preserving all 35 targets, the five required targets and exact
  browser-generation pins.
- Treat ARM and Intel macOS runners as the same operating-system slot when
  detecting duplicate coverage; reject Intel targets on a non-x64 runner.

### Upgrade

Consumer workflow pins remain unchanged until explicitly updated. A consumer
that requires Intel for a browser generation must review its inventory override
and qualify the same application scenarios on that runner. Regenerating the
default inventory does not preserve custom runner choices automatically.

### Security

This corrects release admission for an already supported runner label without
allowing duplicate macOS entries to replace missing Linux or Windows coverage.
Candidate identity, complete browser evidence and existing release gates remain
required. No deployment or physical-device compatibility is implied.

## [0.1.2] - 2026-10-14

- Require positive engine execution counts and retained instrumentation evidence
  for Python seed replay and RC campaigns; reject setup-only false passes.
- Validate required seed corpora and Go module files before campaigns, and retain
  bounded generated Go corpus snapshots with measured counts and sizes.
- Bound source Go regression retention across packages, excluding links, saved
  diagnostics and vendored dependencies, and reserve the internal preflight name.
- Reject explicitly malformed optional toolkit workflow configurations instead
  of silently treating falsey values as absent.
- Add selective, bounded Python fuzz preflight to ordinary CI while keeping
  time-budgeted mutation campaigns exclusive to public-repository RCs.
- Enforce matching immutable fuzz workflow references and preserve generated
  workflow and source-package contracts.

### Upgrade

Download the source archive and follow `docs/self-release.md` to install the
shipped locked dependencies and validate the extracted source. Consumer workflow
pins remain independent of this source-package version. Consumers adopting this
runtime must keep their policy, wrapper and runtime references consistent and
include valid seed corpora; existing invalid configurations now fail early.

### Security

Schema 2 campaign reports bind actual executions, coverage and retained preflight
proof to the candidate source. Required seed corpora reject links and truncation,
and Go module selection remains confined to the repository. This closes evidence
validation gaps without weakening ordinary CI or claiming untested deployment
and device compatibility.

## [0.1.1] - 2026-10-13

- Add versioned source candidates with deterministic archives, an explicit source
  inventory, version receipts and contract tests against the extracted source.
- Require bounded Linux Atheris campaigns for RCs over the owned OCI metadata
  and version-policy parsers; record evidence for the exact candidate source.
- Reject JSON numbers that overflow finite floating-point representation before
  accepting OCI metadata, preserving canonical JSON round trips.
- Keep source distribution versions independent of consumer workflow commit pins.

### Upgrade

Download the candidate source archive and follow `docs/self-release.md` to install
its locked development dependencies and rerun the extracted-source tests. Existing
consumers keep their immutable workflow commits until explicitly updated; source
candidate versions are independent of those workflow references.

### Security

The OCI metadata reader now rejects overflowing JSON numbers. RCs additionally
record bounded, isolated Linux campaigns over the metadata and version-policy
parsers. Review the evidence attached to the exact candidate tag before adopting
it. These software checks do not certify hardware integrations or deployed systems.

## 0.1.0 - 2026-09-12

- First tagged source release of the reusable GitHub Actions workflows.
- Document the additional native Venus OS checks required for service installers:
  BusyBox compatibility, persistent paths, repeat installs, offline dependencies,
  bounded logging and target Python/architecture compatibility.

Passing host CI does not establish device compatibility. This repository provides
CI workflows, not a GX runtime package. Consumers pinned to an immutable commit
remain on that commit until explicitly updated.
