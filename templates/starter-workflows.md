# Starter workflows

Use this guide to choose the maintained CI entry points for a new consumer.
Read the [consumer guide](../docs/CONSUMER_GUIDE.md) for inputs, permissions and
compatibility before copying a workflow.

## CI validation

Choose the reusable validator matching the project: `python-ci.yml`, `go-ci.yml`,
`typescript-ci.yml`, `rust-ci.yml` or `terraform-ci.yml` in
[`.github/workflows`](../.github/workflows). Pin each external `uses:` reference
to a reviewed full 40-character toolkit commit SHA.

- Declare the caller's permissions explicitly and forward only the secrets
  required by the selected workflow. `GITHUB_TOKEN` is provided automatically;
  do not create or pass it as a custom workflow secret.
- Python packages need `use-uv-lock: true` and a committed, current `uv.lock`.
  Script-only projects can use the documented hash-locked requirements mode.
- Choose toolchain versions supported by the validator and the project. Use the
  selected toolkit revision's input definitions as the source of truth.
- Keep test execution and the project's coverage threshold enabled.
- Run validation on pull requests and default-branch pushes. Do not exclude
  dependency files or workflow YAML through `paths-ignore`. For lighter
  documentation checks, use the supported [change-scope policy](../docs/CHANGE_SCOPE.md).

The toolkit's [contract-test workflow](../.github/workflows/contract-tests.yml)
shows current Python and Go calls against local fixtures. Those `./` references
are for toolkit self-tests; external consumers need the full repository path and
reviewed SHA.

## Security and repository automation

Use [security-scan.yml](../.github/workflows/security-scan.yml) for its documented
CodeQL, Trivy and dependency-review checks, with the corresponding caller
permissions. There is no reusable `scorecard.yml` in this revision; see the
[Scorecard adapter](../tools/scorecard-scan/README.md) for the maintained scanner.

Copy the maintained [approval caller](../docs/examples/auto-approve.yml) and
[merge caller](../docs/examples/auto-merge.yml) when those features are wanted.
Replace `TOOLKIT_COMMIT_SHA` with a reviewed full commit SHA and follow the
[approval and merge requirements](../docs/AUTO_MERGE.md). The examples include
credential forwarding and event handling; approval alone does not enable merging.

## Releases and container builds

Install the reviewed per-repository release policy and build-only adapters using
[the fleet rollout guide](../docs/FLEET_ROLLOUT.md). Review the generated files and
complete the documented checks before enabling publication.

The legacy reusable `release.yml`, `docker-build.yml` and `nightly.yml` entry
points are retired and fail with migration instructions. Do not create new
callers for them. Current channel, container-publication and promotion commands
are documented in [release operations](../docs/APPLICATION_RELEASES.md).

## Validation before adoption

Validate the proposed consumer's YAML, reusable-workflow inputs, permissions and
immutable references, then run its actual GitHub Actions checks. Local checks
help diagnose configuration errors but do not prove hosted token permissions,
artifact uploads or release readiness. See [CONTRIBUTING](../CONTRIBUTING.md) for
changes to the toolkit itself.
