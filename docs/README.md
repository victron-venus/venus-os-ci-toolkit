# Documentation index

These documents describe the reusable workflows, actions, runner images, secret
control plane and release process that make up the CI toolkit.

## Get started

| Document | Description |
| --- | --- |
| [Consumer guide](CONSUMER_GUIDE.md) | How to pin, update and validate reusable workflows from this toolkit. |
| [Architecture, security evidence and roadmap](project-assurance.md) | Overview of the components, the assurance case and planned work. |
| [Toolkit source candidates](self-release.md) | Publishing versioned toolkit source with an explicit file inventory. |

## Choose and run workflows

| Document | Description |
| --- | --- |
| [Checks selected by changed files](CHANGE_SCOPE.md) | The single change classifier and the jobs it selects. |
| [Native auto-merge for protected CI gates](AUTO_MERGE.md) | Reusable `auto-merge.yml` behaviour and its stability requirements. |
| [CodeRabbit review requests](CODERABBIT.md) | Advisory CodeRabbit review integration and its pinned caller. |
| [Review policy with a CodeRabbit quota exception](REVIEW_POLICY.md) | Optional policy that keeps a quota outage from blocking merges. |
| [Consumer-local reusable workflow distribution](WORKFLOW_DISTRIBUTION.md) | Distributing callers while keeping runner selection explicit. |
| [Dependency updates](DEPENDENCY_UPDATES.md) | Centrally scheduled updates of actions and reusable workflows. |
| [Shared coverage publication](COVERAGE.md) | Collecting coverage once in the test job and publishing it. |
| [Static-analysis review notes](STATIC_ANALYSIS.md) | Scope and review guidance for static-analysis findings. |
| [Browser and operating-system acceptance](BROWSER_COMPATIBILITY.md) | Opt-in browser matrix in the TypeScript CI workflow. |
| [Reviewed test-only automatic pushes](automatic-release-test-paths.md) | The narrow allowlist that permits automatic test-source pushes. |

## Runners and images

| Document | Description |
| --- | --- |
| [Optional self-hosted routing](RUNNERS.md) | Routing jobs to self-hosted runners; points to the recovery index for reinstalls. |
| [ARC recovery and reuse](ARC_RECOVERY.md) | Entry point for restoring or preparing the runner control plane. |
| [Synthetic ARC recovery examples](examples/arc-recovery/README.md) | Invented owners and nodes that grant no access and contain no secrets. |
| [Private ephemeral runners](PRIVATE_EPHEMERAL_RUNNERS.md) | Private ephemeral runner setup and its base toolchain. |
| [Isolated runner templates on k3s](ottplay-k3s-runners.md) | Unapplied reference templates for a private runner pool. |
| [Browser prerequisites in the reserve image](RUNNER_BROWSERS.md) | Why branded browser packages are installed in the image. |
| [Runner GStreamer backport](RUNNER_GSTREAMER_BACKPORT.md) | Retained GStreamer packages in the browser image. |
| [Hosted runner image artifacts](RUNNER_IMAGE_ARTIFACTS.md) | How the builder image comes from the accepted artifact chain. |
| [Runner image security refresh](RUNNER_IMAGE_SECURITY.md) | Rebuilding the runner image from the canonical base recipe. |
| [Reviewed gVisor VFS candidate build](gvisor-vfs-candidate-build.md) | Manual, quarantined candidate build from public source. |

## Secret control plane

| Document | Description |
| --- | --- |
| [Secret control plane](SECRETS.md) | Keeping reusable code public while operator data stays recoverable. |
| [Bootstrap the private operator environment](SECRETS_BOOTSTRAP.md) | Encryption identity, catalog and delivery bootstrap. |
| [Practice recovery with synthetic data](SECRETS_DRILL.md) | The offline drill to run before choosing production key custody. |
| [Use an encrypted catalog](SECRETS_HOWTO.md) | Command walkthrough for a reviewed toolkit checkout. |
| [Deliver recovered secrets to GitHub Actions](SECRETS_GITHUB.md) | Catalog schema 2 and organization/repository secret delivery. |
| [Rotation and outage runbooks](SECRETS_OPERATIONS.md) | Day-two operations, rotation and outage handling. |
| [Recovery runbook](SECRETS_RECOVERY.md) | Restoring an operator environment after loss. |
| [Synthetic catalog example](examples/secret-control/README.md) | Illustrative catalog data with no real destinations or values. |

## Releases and delivery

| Document | Description |
| --- | --- |
| [Application release operations](APPLICATION_RELEASES.md) | Runbook for public repositories with a `.release-policy.json`. |
| [Fleet rollout: gated CI and release channels](FLEET_ROLLOUT.md) | Shared rollout procedure for CI gates and channels. |
| [Closed builds in the public release workflow](closed-builds.md) | Opt-in local worker for a reviewed closed build. |
| [Verify a signed release](release-signatures.md) | Signature verification for repositories that enable release signing. |
| [Patched release signer and verifier](release-signer.md) | Exported signer/verifier binary contract. |
| [Compact stable distribution manifests](release-distribution.md) | Opt-in compact stable metadata for byte promotion. |
| [Qualified GitHub CLI for release operations](release-github-cli.md) | The exact `gh` build installed by release jobs. |
| [Release helper TLS profile](RELEASE_TLS_PROFILE.md) | Verified TLS profile used by release helpers. |
| [Verified vendor delivery](VENDOR_DELIVERY.md) | Calling verified vendor delivery after producer builds. |
| [Browser installation for ESP firmware](ESP_WEB_INSTALLER.md) | Turning public factory firmware into a browser installer. |

## Containers and provenance

| Document | Description |
| --- | --- |
| [Native multi-platform OCI packaging](NATIVE_OCI.md) | Versioned container helpers for multi-platform archives. |
| [Source-built OCI tool qualification](OCI_TOOL_QUALIFICATION.md) | Building candidate BuildKit and runc from pinned source. |
| [Prepare qualified OCI tools for publication](OCI_TOOL_PROMOTION.md) | Promotion and image testing of qualified OCI tools. |
| [Consume the qualified native builder](ACCEPTED_BUILDER_CONSUMER.md) | Importing the accepted builder images without rebuilding. |
| [Repeatable OCI payloads with detached build evidence](detached-provenance.md) | Separating runnable bytes from truthful build evidence. |

## Operations reference

| Document | Description |
| --- | --- |
| [Public source and operator configuration](OPERATOR_CONFIG.md) | What the checked-in `fleet.json` snapshot does and does not contain. |
| [Local nightly checks](LOCAL_NIGHTLY.md) | Running the nightly checks against selected local checkouts. |
| [RC fuzzing: process and verified results](rc-fuzzing-results.md) | Illustrated process and accepted RC reports with SVG diagrams. |
| [Documentation checks](DOCUMENTATION_CHECKS.md) | The shared `scripts/check_docs.py` checker: links, anchors, SVG, index coverage, fences, mermaid and version agreement. |

Shared repository policy: [release-workflow.md](release-workflow.md), [runner-preflight.md](runner-preflight.md), [VERSIONING.md](VERSIONING.md) and [rc-fuzzing.md](rc-fuzzing.md) are generated or shared boilerplate maintained in [victron-venus/venus-os-ci-toolkit](https://github.com/victron-venus/venus-os-ci-toolkit).
