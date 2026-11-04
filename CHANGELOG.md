# Changelog

## [0.1.13] - 2026-11-04

### Fixed

- Acquire accepted native builder archives from an explicitly pinned stable toolkit release without depending on expired Actions artifacts. Preserve the original image, filesystem and qualification checks; missing or changed assets fail without fallback.
- Bind transport selection and its digest to one immutable input snapshot throughout acquisition, preparation and two-architecture publication. Reject changes before successful receipts, imports or publication commit.
- Reduce native runner snapshot boundaries by grouping the existing allowlisted Go/browser inputs and compiling all three Go tools in one unprivileged RUN. Clean module/build caches before committing that layer, retaining the existing tests, output binaries and notices.
- Record bounded builder and Go-directory storage observations and exact cleanup steps. Preserve the original build failure when diagnostics, final disk checks or import cleanup also fail; unresolved cleanup remains a failure.
- Assemble the final runner through read-only pinned stage mounts in one RUN, retaining payload paths, modes, override order and installation checks while avoiding repeated copies of the browser-equipped root filesystem. Keep the final listener qualification unprivileged.
- Apply the same snapshot reduction to the private child image: stage its existing six input files separately and combine installation and wrapper replacement, preserving the inherited CA and runtime configuration.

### Upgrade

Use the reviewed transport lock together with the unchanged original builder identity lock. The runner manifest and consumer receipt bind both separately. The retained release route requires exact release, tag, source and asset identities; it does not resolve the latest release or publish new image bytes. Final runner qualification and activation remain separate.

The storage changes require a fresh artifact-only runner build and full scan. Source tests do not establish that the worker has enough disk space. The accepted native builder, two-CPU/4-GiB limits and 2-GiB free-space floor remain unchanged.

### Security

Release storage does not replace the original archive, root filesystem and qualification verification. Only reviewed immutable asset identities are accepted; changed transport inputs abort the operation. The downloader retains bounded reads and read-only access, without new credentials, registry writes or deployment permissions.

## [0.1.12] - 2026-11-03

### Added

- Consume the accepted native BuildKit and runc artifacts in the existing runner image workflow, with exact source, archive, image, filesystem and qualification checks. Preserve bounded full proof reports separately from image archives.
- Retain the original amd64 and arm64 OCI archives, Docker exports and qualification evidence in toolkit releases. Record image qualification separately from toolkit release provenance; include every payload in checksums and the existing version-input receipt.
- Map the two immutable image archives to architecture-specific builder repositories through the existing verified stable publisher, without rebuilding, relabeling or creating a multiarch index.

### Upgrade

Use reviewed source and an explicitly accepted stable release. Original image and release identities are different and remain recorded separately. Retained release assets require a subsequent exact release-asset transport lock before expiry-bound Actions consumers can use them. Final runner image qualification and activation are separate steps.

### Security

Builder acquisition is bounded, rejects expired or changed inputs and preserves original licenses. Publication runs on a disposable hosted worker without executing the images; registry writes retain existing RC/stable provenance, destination and no-overwrite checks. No deployment or household access is added.

## [0.1.11] - 2026-11-03

### Fixed

- Reject required build-receipt filenames longer than 200 ASCII characters during policy validation, matching final asset staging and identifying invalid inventories before builds begin.
- Keep inventory-only migrations compatible with the filename check through a separately hashed validation layer. Preserve accepted historical source states, exact reversal, idempotence, and rejection of unknown or partially migrated files.
- Keep status-shaped lines inside partial release-asset and Actions ZIP bodies from being mistaken for HTTP denials. Inspect the validated response header without copying the partial body so permitted whole-read retries can recover.
- Release each failed buffered response before waiting or allocating the next attempt, avoiding overlapping capture buffers during recovery. Treat explicit authorization and Unauthorized diagnostics as terminal even when accompanied by EOF or timeout text.
- Handle disabled native auto-merge in the default shared workflow by requiring stable head/base/check/review observations, an independent current-head approval and successful CI, then submitting an ordinary protected merge. Explicit native mode reports a disabled setting; unknown capability metadata fails closed.

### Upgrade

Use a reviewed toolkit refresh or the inventory-only migration for recognized source states to receive this validation change in copied consumers. Existing valid receipt inventories retain their names and publication requirements. The binary-read correction requires a reviewed release-client refresh or targeted backport; the inventory-only migration does not change the download engine. Adopt an immutable updated shared-workflow reference to receive the auto-merge capability handling.

### Security

The receipt filename correction rejects invalid configuration earlier; it does not add privileges or change approval policy. Existing source, producer, checksum and complete-asset checks remain required. Real HTTP denials, authentication, certificate and rate-limit failures continue to fail closed, and publication writes are not retried. Read recovery still allows at most three total attempts. The shared workflow never changes repository settings or uses administrator merge; existing server-side review, check and freshness rules remain enforced.

## [0.1.10] - 2026-11-03

### Fixed

- Recover metadata, buffered release-asset and Actions ZIP reads from recognized transient transport failures and HTTP 500, 502, 503 or 504 responses. Retry only read-only GET requests, at most three times, with 30-second metadata and 120-second buffered-download attempt limits. Discard failed responses instead of appending partial bytes.
- Reject copied successful validation jobs on release reruns. Check their source, attempt and execution intervals against the exact attempt start; a new job ID or attempt number cannot make earlier CI or browser verdicts fresh.
- Preserve multiple pending manual and scheduled releases in generated workflows and the toolkit's own publisher, up to [GitHub's 100-run queue limit](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency#example-queueing-multiple-pending-runs). Running releases remain protected from cancellation.

### Changed

- Pin the optional ARC controller candidate to 0.15.1, retain scale-set charts at 0.15.0, and record controller chart, archive and image identities separately. Record the four bounded reconciliation settings explicitly.
- Support the exact 0.15.0 and 0.15.1 controller user-agent values during the private-worker rollback window, while retaining duplicate, substitution and unexpected-variable rejection.
- Document controller recovery using a durable record of the deployed Helm revision, owned Deployment and Kubernetes context. Keep controller readiness and a completed canary as separate rollout checks.
- Refresh the locked Renovate runtime, GitHub Actions and reusable-workflow references.

### Upgrade

Adopt a reviewed immutable toolkit revision explicitly. Existing copied release clients and generated workflows need a reviewed installer refresh to receive the GET recovery, rerun validation and release queue changes. This source package does not update consumer pins, register runners or deploy the ARC controller. Qualify any controller rollout separately using the documented admission, capacity, canary and rollback checks.

### Security

Authentication, certificate, missing-resource and rate-limit failures are not retried. Writes remain single attempts; streamed downloads retain their existing time, size, disk-space and digest limits. Rerun validation fails closed on missing or inconsistent job and step timing; first-attempt behavior remains unchanged. Candidate source, provenance, version, complete asset inventory and payload checks remain required. The controller admission change allows only the two exact reviewed user-agent values and does not broaden worker permissions.

## [0.1.9] - 2026-11-02

### Fixed

- Reassess 211 changed upstream advisory records against their previous canonical contents and the retained runner-image source evidence. Update only reviewed advisory identities; keep the existing source, component, prerequisite and complete-scan acceptance checks.
- Retain explicit evidence of severity, CVSS, title, date and reference changes, including materially revised CVSS vectors. A changed description or affected source path still requires a new review.

### Upgrade

Adopt a reviewed immutable toolkit revision explicitly when building runner images. This source package does not update existing consumer pins, register runners or deploy images. Its release qualification and each image producer run have separate exact-source identities; a source package release does not substitute for image or site/runtime acceptance.

### Security

This release updates reviewed applicability metadata, not upstream dependency versions. Existing component/source proofs must continue to establish non-applicability for every retained finding. New or unexplained advisory changes remain blocked by the full scan gate; no module-wide or rule-wide suppression is added.

## [0.1.8] - 2026-10-27

### Changed

- Separate standalone fuzz candidate evidence from release evidence that requires a frozen publication plan.
- Add opt-in historical Firefox qualification, quota-aware review scheduling and bounded runner resource profiles. Default runner selection and required application checks remain explicit consumer choices.
- Ship native runner-image build, dependency and browser preparation helpers with artifact provenance, architecture checks and source-archive validation.
- Add a read-only capacity preflight that accounts for assigned requests, init containers, runtime overhead and namespace quotas. Unsupported or incomplete resource evidence cannot produce a passing result; available resources do not establish scheduling or runtime acceptance.

### Fixed

- Use OSV's current Python enricher identifier so vulnerability scans inspect declared requirements without inventing a separate resolved dependency graph. Preserve actual findings, errors and the existing Scorecard reporting policy.
- Reject incomplete current-attempt package sets when `versioning.required_build_receipts` declares the expected producers. The dedicated `--release-inventory-only` migration adds this check to known runtimes while preserving earlier receipt-identity checks and historical promotion policy.
- Bind release package receipts and Actions artifact selection to the exact producer run attempt, including native assembly inputs. A partial rerun cannot publish earlier-attempt packages; complete reruns retain source, version, signature and payload checks.
- Use the provisioned Ubuntu fallback package mirror for hosted browser workers while preserving the existing planning/browser budgets and required compatibility checks.
- Preserve usable version metadata when migrating immutable workflow references to the canonical atomic Renovate updater, including coupled fuzz workflow and runtime pins.
- Upload release assets through the existing draft release ID, with one bounded write and complete byte readback. Wait within one fixed deadline for current-run gate metadata without accepting skipped, failed or mismatched checks.
- Validate source-bound Upgrade and Security guidance before reserving release versions. Preserve existing immutable version reservations and publication checks.
- Retain required aggregate checks after workflow cancellation so incomplete dependencies reach their failure assertions.
- Correct runner-image evidence, pinned browser prerequisites and VFS runtime source bindings; deployment still requires separate qualification.

### Upgrade

This source package does not deploy runner images or update consumer workflow pins. Adopt a reviewed immutable toolkit revision explicitly. Existing vendored publisher clients require the corresponding reviewed file update, and native image changes require artifact and workload qualification before activation. Keep the persistent release-version ledger and the previous qualified runtime for rollback.

Refresh release producers, collectors and shared fuzz workflow pins together with the installer. Before publication starts, use **Re-run all jobs** only when the allocator and policy permit reusing the reservation. After publication begins or its outcome is uncertain, inspect tags, releases and assets before reserving a new policy-authorized run. Never reset the ledger or overwrite an immutable release to retry; earlier-attempt packages cannot fill a current-attempt gap.

### Security

Update the containerd build overlay to cilium/ebpf 0.22.0 with matching dependency and source hashes. Runner tooling builds use an unprivileged account and bind acceptance to the actual image payload. Remove the unused legacy Docker Distribution client. Update the locked scanner's x/net dependency to 0.62.0 and its required companion modules for the HTML parser fixes. Other runner dependency upgrades and image acceptance remain separately tracked; source publication is not a claim that every runner dependency finding is resolved.

Consumers must retain least-privilege permissions, reviewed immutable workflow pins and trusted runner isolation. Publishing this source package does not rotate credentials or authorize changes to running services.

## [0.1.7] - 2026-10-24

### Security

- Ship the maintained Cosign module overlay with go-chi/chi/v5 5.4.0, replacing the version affected by GHSA-j6qf-3m9c-vwj3. The source distribution includes these canonical generator inputs. No claim is made that the Cosign CLI exposes the affected HTTP middleware.

### Upgrade

Use this source package when regenerating signed-release consumers. Existing immutable workflow pins and copied files require an explicit reviewed update; publishing the package does not change runner routing, credentials, or deployed services.

## [0.1.6] - 2026-10-20

### Changed

- Reserve 250m CPU for native LAN CI workers to improve startup under contention; the CPU limit and isolation settings remain unchanged.
- Add explicit Linux browser scope and unavailable-runner handling while retaining the separate release qualification requirements.
- Extend opt-in ARC configuration with reviewed listener placement, physical-capacity budgets, held reserve inventories and recovery guidance. Add bounded multi-profile NUC configuration and isolated gVisor candidate build artifacts.
- Support optional closed build workers for qualified published tags, including named environment secrets, nightly rebuilds and promotion receipts bound to the publisher's time window.

### Fixed

- Prevent the macOS output-limit regression fixture from blocking on a full pipe before its simulated process wait, while retaining the output-limit assertion.
- Preserve native ARC listener failure exit codes and bound admission checks without treating runner registration as accepted runtime capacity.
- Read all paginated jobs after partial workflow reruns and validate every Scorecard result before upload.
- Keep workflow/helper pins aligned and preserve consumer policy formatting during explicit runner adoption.
- Document both shipped dependency locks needed to run the source distribution's generator and secret-adapter contracts.

### Upgrade

The source package version remains independent of consumer workflow pins. Review and adopt an immutable commit explicitly; publishing this candidate does not change existing runner routing, release policies or credentials.

### Security

Runner pools and candidate runtimes retain their existing admission holds. Source packaging, repository CI and beta publication do not establish isolation, storage, concurrency or workload acceptance, and do not authorize infrastructure activation.

## [0.1.5] - 2026-10-18

### Changed

- Route Dependabot approval metadata through the trusted default branch, including manual recovery. Select the official `GITHUB_TOKEN` for Dependabot and retain the existing bot credential for maintainer-authored changes.
- Add opt-in disposable worker profiles with bounded CPU and scratch storage, including disk, tmpfs and exclusive bind configurations. Preserve local runner images while pools are idle and wait for sandbox containerd before starting Docker.
- Include an optional, single-repository NUC KVM CI profile with fixed resource limits and admission guards. It remains unqualified until the intended host, image, network and unchanged application jobs pass their separate checks; source publication does not activate it.
- Validate resource maps and scratch limits against the Kubernetes CEL schema, and verify the visible filesystem before admitting tmpfs work.
- Enable guest `sudo` for installers inside the dedicated gVisor runtime, preserving the host capability, filesystem and network boundaries.
- Document Terraform-owned routing, staged qualification, quota waits and return to standard runner capacity.
- Update the pinned Renovate runtime to 44.166.0 and align the internal setup and fuzz workflow references.

### Fixed

- Handle macOS process-group signal denial when a secret-control tool exits during an output-limit check. Preserve the redacted failure, bound process cleanup, and always close its pipes and selector, including when termination fails.
- Install the pinned GitHub CLI in private runner images so metadata workflows can use `gh api --paginate --slurp`. Support bounded offline archive installation with the same artifact, executable and runtime checks; deployment still requires separate image qualification.

### Upgrade

The source package version is independent of consumer workflow pins. Adopt a reviewed immutable commit explicitly, keep the previous pin for rollback, and validate each selected worker profile before activation. Publishing this source release does not switch existing runner routing or qualify a cluster deployment.

### Security

Approval recovery reads trusted PR metadata without executing pull-request code and preserves the distinction between the Dependabot token and the existing review bot. Worker profiles remain opt-in; repository CI and source-package acceptance do not replace isolation, storage, concurrency or workload acceptance on the intended host. No credentials or installation-specific configuration are included in the source archive.

## [0.1.4] - 2026-10-17

- Add explicit private runner selection and repository-scoped, disposable ARC
  worker profiles with bounded capacity, isolated metadata execution and native
  Docker cgroup admission. Include both switch directions and retirement guidance.
- Distribute the complete reusable-workflow closure from immutable commits into
  private callers, retaining provenance hashes and rejecting source collisions.

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


- Honor configured Git commit signing in automatic version-preparation PRs,
  failing before publication if the requested signature cannot be created.
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
