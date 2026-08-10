# Venus OS CI Toolkit

Reusable GitHub Actions workflows and composite actions for Victron Venus OS projects.

## Overview

This toolkit provides standardized, reusable CI/CD workflows that can be referenced by any repository in the `victron-venus` organization. It eliminates duplication and ensures consistent practices across all projects.

## Structure

```
venus-os-ci-toolkit/
├── .github/workflows/          # Reusable workflows
│   ├── python-ci.yml           # Python CI (lint, type-check, test, coverage)
│   ├── go-ci.yml               # Go CI (lint, vulncheck, test, coverage)
│   ├── docker-build.yml        # Docker build & publish to GHCR
│   ├── release.yml             # GitHub release automation
│   ├── security-scan.yml       # Security scanning (CodeQL, Trivy, Dependency Review)
│   ├── scorecard.yml           # OpenSSF Scorecard
│   ├── auto-approve.yml        # Auto-approve Dependabot/Renovate PRs
│   ├── auto-merge.yml          # Auto-merge when checks pass
│   └── nightly.yml             # Nightly build trigger
├── actions/                    # Composite actions
│   ├── setup-python/           # Python setup with caching
│   ├── setup-go/               # Go setup with caching
│   └── setup-docker/           # Docker Buildx setup
└── templates/                  # Starter workflow templates
    └── starter-workflows.md    # Copy-paste templates for projects
```

## Quick Start

### For Python Projects

Create `.github/workflows/ci.yml` in your project:

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/python-ci.yml@main
    with:
      python-version: '3.12'
      working-directory: '.'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Add security scanning (`.github/workflows/security.yml`):

```yaml
name: Security
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: '0 6 * * 1'

jobs:
  security:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/security-scan.yml@main
    with:
      languages: 'python'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### For Go Projects

Create `.github/workflows/ci.yml`:

```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/go-ci.yml@main
    with:
      go-version: '1.23'
      working-directory: '.'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## Available Workflows

| Workflow | Description | Inputs |
|----------|-------------|--------|
| `python-ci.yml` | Python lint, type-check, test, coverage | python-version, working-directory, coverage-threshold |
| `go-ci.yml` | Go lint, vulncheck, test, coverage | go-version, working-directory, coverage-threshold |
| `docker-build.yml` | Multi-platform Docker build & push | image-name, dockerfile, platforms, push |
| `release.yml` | GitHub release from tags | tag-pattern, release-name, draft, prerelease |
| `security-scan.yml` | CodeQL, Trivy, Dependency Review | languages, trivy-severity |
| `scorecard.yml` | OpenSSF Scorecard | working-directory |
| `auto-approve.yml` | Auto-approve bot PRs | pr-author, required-reviews |
| `auto-merge.yml` | Auto-merge when checks pass | pr-author, merge-method, required-status-checks |
| `nightly.yml` | Trigger workflow on schedule | cron, workflow-to-trigger |

## Composite Actions

| Action | Description |
|--------|-------------|
| `actions/setup-python` | Python + pip cache + ruff/pytest |
| `actions/setup-go` | Go + module cache + golangci-lint/govulncheck |
| `actions/setup-docker` | Docker Buildx + GHCR login |

## Full Example: Complete Python Project Setup

```yaml
# .github/workflows/ci.yml
name: CI
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  ci:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/python-ci.yml@main
    with:
      python-version: '3.12'
      test-args: '-v --tb=short'
      coverage-threshold: 85

# .github/workflows/security.yml
name: Security
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: '0 6 * * 1'

jobs:
  security:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/security-scan.yml@main
    with:
      languages: 'python'

# .github/workflows/docker.yml
name: Docker
on:
  push:
    branches: [main]
    tags: ['v*']
  pull_request:
    branches: [main]

jobs:
  docker:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/docker-build.yml@main
    with:
      image-name: 'my-python-service'
      push: ${{ github.event_name != 'pull_request' }}

# .github/workflows/release.yml
name: Release
on:
  push:
    tags: ['v*']

jobs:
  release:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/release.yml@main
    with:
      generate-notes: true
```

## Required Repository Settings

1. **Settings → Actions → General → Workflow permissions**: Enable "Allow GitHub Actions to create and approve pull requests"
2. **Settings → Security → Code scanning**: Enable CodeQL (for CodeQL workflow)
3. **Branch protection**: Require status checks (CI, CodeQL, Trivy) before merge

## Contributing

1. Changes to reusable workflows should be backwards compatible
2. Test changes by referencing `@main` in a test repository
3. Tag releases for stable versions (e.g., `v1.0.0`)
4. Update `templates/starter-workflows.md` when adding new workflows

## License

MIT License - see [LICENSE](LICENSE)