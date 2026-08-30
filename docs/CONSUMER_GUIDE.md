# Consumer Guide

How to pin, update, and validate reusable workflows from this toolkit.

---

## Versioning Policy

### Pin to Full Commit SHA (Required for Reproducibility)

All consumers **must** pin reusable workflows to a full 40-character commit SHA, not a branch or tag.

```yaml
# Correct — reproducible
uses: victron-venus/venus-os-ci-toolkit/.github/workflows/python-ci.yml@5b18667880c7a0154b7c7e6686042eff5f47cdb7

# Wrong — moves on every push
uses: victron-venus/venus-os-ci-toolkit/.github/workflows/python-ci.yml@main

# Wrong — moves on every tag
uses: victron-venus/venus-os-ci-toolkit/.github/workflows/python-ci.yml@v1.0.0
```

Full SHA pinning prevents silent breakage when this toolkit is updated. GitHub Actions evaluates `uses:` at queue time, not at workflow authoring time — so even a committed workflow can pick up a newer commit if it pins to a mutable ref.

### Why Not `@main`?

`@main` is a branch ref. GitHub resolves it to a commit SHA at run time. Any change merged to `main` (bug fix, input rename, step reordering) silently propagates to every consumer the next time their CI runs. This has caused broken pipelines across the fleet in this org.

### Tags Exist But Are Not Used

This toolkit currently has **no `v*` tags**. Tags were never cut. All consumers pin to commit SHAs — that is the established convention and it is the correct one.

If tags are added in the future, the same pinning discipline applies: pin to the tag's commit SHA, not the tag name. The tag is only a human-readable alias for a SHA.

### When to Update Your Pin

Update the pinned SHA when you want to adopt a new toolkit feature or security fix. The toolkit maintains **backwards compatibility** within the SHA: inputs added after you pinned will use their defaults, so your workflow will not break if you do not update. But you will not get new inputs until you bump the SHA.

### How to Find the Current SHA

```bash
git ls-remote git@github.com:victron-venus/venus-os-ci-toolkit HEAD
```

Or from a local clone:

```bash
cd /path/to/your-project
# After pulling latest toolkit
git fetch git@github.com:victron-venus/venus-os-ci-toolkit main
git log --oneline -1 origin/main  # shows SHA
```

### Security Implications

- A compromised commit SHA cannot be overwritten in git. Tags and branches can be force-pushed.
- GitHub caches workflow files by SHA. A SHA that is deleted from `main` history (rewritten branch) may still be runnable if GitHub's cache retains it.
- If you suspect a toolkit SHA was compromised, rotate to the next good SHA immediately and report to the maintainers.

---

## Compatibility Matrix

All reusable workflows in this toolkit are called via `workflow_call`. A calling workflow uses `uses: ...@<sha>` with `secrets: inherit` or explicit secret forwarding.

### Supported Consumer Workflow Files

| Consumer workflow file | Toolkit workflow called | Notes |
|---|---|---|
| `.github/workflows/ci.yml` | `python-ci.yml` | Python lint, type check, test, coverage |
| `.github/workflows/ci.yml` | `go-ci.yml` | Go lint, vulncheck, test, coverage |
| `.github/workflows/ci.yml` | `rust-ci.yml` | Rust fmt, clippy, test, optional frontend |
| `.github/workflows/ci.yml` | `terraform-ci.yml` | Terraform fmt, init, validate, tflint |
| `.github/workflows/security.yml` | `security-scan.yml` | CodeQL, Trivy, dependency review |
| `.github/workflows/scorecard.yml` | `scorecard.yml` | OpenSSF Scorecard |
| `.github/workflows/docker.yml` | `docker-build.yml` | Multi-platform Docker build & push |
| `.github/workflows/release.yml` | `release.yml` | GitHub release from tag push |
| `.github/workflows/auto-approve.yml` | `auto-approve.yml` | Auto-approve bot PRs (direct trigger) |
| `.github/workflows/auto-approve.yml` | `auto-approve-reusable.yml` | Same, via reusable |
| `.github/workflows/auto-merge.yml` | `auto-merge.yml` | Auto-merge when checks pass |
| `.github/workflows/nightly.yml` | `nightly.yml` | Scheduled trigger of another workflow |

### Composite Actions

| Action | Description |
|---|---|
| `actions/setup-python` | Python + pip cache + ruff/pytest |
| `actions/setup-go` | Go + module cache + golangci-lint/govulncheck |
| `actions/setup-docker` | Docker Buildx + GHCR login |

These may also be pinned by consumers, though they are primarily consumed internally by the reusable workflows above.

### Workflow Inputs Reference

#### `python-ci.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `python-version` | string | `'3.12'` | No |
| `working-directory` | string | `'.'` | No |
| `install-dependencies` | boolean | `true` | No |
| `run-lint` | boolean | `true` | No |
| `run-type-check` | boolean | `true` | No |
| `run-tests` | boolean | `true` | No |
| `test-args` | string | `''` | No |
| `coverage-threshold` | number | `80` | No |

Secrets: `GITHUB_TOKEN` (codecov upload).

#### `go-ci.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `go-version` | string | `'1.23'` | No |
| `working-directory` | string | `'.'` | No |
| `run-lint` | boolean | `true` | No |
| `run-tests` | boolean | `true` | No |
| `run-vulncheck` | boolean | `true` | No |
| `test-args` | string | `'-race -coverprofile=coverage.out -covermode=atomic'` | No |
| `coverage-threshold` | number | `70` | No |

Secrets: `GITHUB_TOKEN` (codecov upload).

#### `rust-ci.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `working-directory` | string | `'.'` | No |
| `rust-toolchain` | string | `'stable'` | No |
| `components` | string | `'clippy,rustfmt'` | No |
| `enable-clippy` | boolean | `true` | No |
| `enable-fmt` | boolean | `true` | No |
| `enable-tests` | boolean | `true` | No |
| `test-args` | string | `''` | No |
| `apt-packages` | string | `''` | No |
| `enable-frontend` | boolean | `false` | No |
| `node-version` | string | `'24'` | No |
| `frontend-working-directory` | string | `''` | No |

#### `terraform-ci.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `working-directory` | string | `'.'` | No |
| `terraform-version` | string | `'1.5.x'` | No |
| `tflint-enabled` | boolean | `true` | No |

#### `security-scan.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `languages` | string | `'python,javascript,go'` | No |
| `working-directory` | string | `'.'` | No |
| `run-codeql` | boolean | `true` | No |
| `run-trivy` | boolean | `true` | No |
| `run-dependency-review` | boolean | `true` | No |
| `trivy-severity` | string | `'HIGH,CRITICAL'` | No |

#### `scorecard.yml`

No inputs. Requires `GITHUB_TOKEN` (provided automatically by GitHub Actions).

#### `docker-build.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `image-name` | string | — | **Yes** |
| `registry` | string | `'ghcr.io'` | No |
| `registry-owner` | string | `'${{ github.repository_owner }}'` | No |
| `image` | string | `''` | No |
| `dockerfile` | string | `'Dockerfile'` | No |
| `context` | string | `'.'` | No |
| `platforms` | string | `'linux/amd64,linux/arm64'` | No |
| `tags` | string | `''` | No |
| `push` | boolean | `true` | No |
| `sbom` | boolean | `false` | No |
| `provenance` | boolean | `false` | No |
| `cache-from` | string | `'type=gha'` | No |
| `cache-to` | string | `'type=gha,mode=max'` | No |

Secrets: `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` (optional; defaults to GHCR via GITHUB_TOKEN).

#### `release.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `tag-pattern` | string | `'^v[0-9]+\.[0-9]+\.[0-9]+$'` | No |
| `release-name` | string | `'Release ${{ github.ref_name }}'` | No |
| `draft` | boolean | `false` | No |
| `prerelease` | boolean | `false` | No |
| `generate-notes` | boolean | `true` | No |
| `body-file` | string | `''` | No |
| `skip-checkout` | boolean | `false` | No |

#### `auto-approve.yml` / `auto-approve-reusable.yml`

`auto-approve.yml` has no inputs (direct trigger). `auto-approve-reusable.yml` is `workflow_call`:

| Input | Type | Default | Required |
|---|---|---|---|
| `authors` | string | `'["dependabot[bot]","renovate[bot]","4alvit"]'` | No |

Secrets: `BOT_PAT` (required for approval action; skips if absent).

#### `auto-merge.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `pr-author` | string | `'dependabot[bot],renovate[bot]'` | No |
| `merge-method` | string | `'squash'` | No |
| `required-status-checks` | string | `'CI,codeql,trivy'` | No |

Secrets: `BOT_PAT` (required).

#### `nightly.yml`

| Input | Type | Default | Required |
|---|---|---|---|
| `cron` | string | `'0 2 * * *'` | No |
| `workflow-to-trigger` | string | — | **Yes** |
| `workflow-inputs` | string | `'{}'` | No |

---

## Verifying Your Workflow Locally

Before committing, validate YAML syntax and the reusable workflow contract with [act](https://github.com/nektos/act):

```bash
# Install act (macOS)
brew install act

# Run the python-ci workflow on a minimal Python fixture
cd tests/fixtures/python-minimal
act -W .github/workflows/ci.yml --container-architecture linux/amd64
```

The `tests/fixtures/` directory contains minimal consumer-side workflow files that exercise the toolkit. See `tests/fixtures/README.md` for details.
