# Venus OS CI Toolkit - Starter Workflows

Copy these workflow files to your project's `.github/workflows/` directory and customize as needed.

## For Python Projects

### `.github/workflows/ci.yml`
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
      install-dependencies: true
      run-lint: true
      run-type-check: true
      run-tests: true
      test-args: '-v'
      coverage-threshold: 80
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/security.yml`
```yaml
name: Security
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  schedule:
    - cron: '0 6 * * 1'  # Weekly on Monday

jobs:
  security:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/security-scan.yml@main
    with:
      languages: 'python'
      working-directory: '.'
      run-codeql: true
      run-trivy: true
      run-dependency-review: true
      trivy-severity: 'HIGH,CRITICAL'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/scorecard.yml`
```yaml
name: Scorecard
on:
  push:
    branches: [main]
  schedule:
    - cron: '0 6 * * 0'  # Weekly on Sunday

jobs:
  scorecard:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/scorecard.yml@main
    with:
      working-directory: '.'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/auto-approve.yml`
```yaml
name: Auto Approve
on:
  pull_request_target:
    types: [opened, synchronize, reopened]

jobs:
  auto-approve:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/auto-approve.yml@main
    with:
      pr-author: 'dependabot[bot],renovate[bot]'
      required-reviews: '1'
      pr-types: 'dependencies'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/auto-merge.yml`
```yaml
name: Auto Merge
on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  pull_request_review:
    types: [submitted]

jobs:
  auto-merge:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/auto-merge.yml@main
    with:
      pr-author: 'dependabot[bot],renovate[bot]'
      merge-method: 'squash'
      required-status-checks: 'CI,codeql,trivy'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/release.yml`
```yaml
name: Release
on:
  push:
    tags:
      - 'v*'

jobs:
  release:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/release.yml@main
    with:
      tag-pattern: '^v[0-9]+\\.[0-9]+\\.[0-9]+$'
      release-name: 'Release ${{ github.ref_name }}'
      generate-notes: true
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/docker.yml`
```yaml
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
      image-name: 'your-project-name'
      dockerfile: 'Dockerfile'
      context: '.'
      platforms: 'linux/amd64,linux/arm64'
      push: ${{ github.event_name != 'pull_request' }}
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## For Go Projects

### `.github/workflows/ci.yml`
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
      run-lint: true
      run-tests: true
      test-args: '-race -coverprofile=coverage.out -covermode=atomic'
      coverage-threshold: 70
      run-vulncheck: true
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/security.yml`
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
      languages: 'go'
      working-directory: '.'
      run-codeql: true
      run-trivy: true
      run-dependency-review: true
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

### `.github/workflows/nightly.yml`
```yaml
name: Nightly
on:
  schedule:
    - cron: '0 2 * * *'  # 2 AM UTC daily
  workflow_dispatch:

jobs:
  nightly:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/nightly.yml@main
    with:
      cron: '0 2 * * *'
      workflow-to-trigger: 'ci.yml'
      workflow-inputs: '{}'
    secrets:
      GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

---

## Required Setup

1. **Enable GitHub Actions** in your repository settings
2. **Enable "Allow GitHub Actions to create and approve pull requests"** in Settings → Actions → General → Workflow permissions
3. **Add `GITHUB_TOKEN` secret** (automatically available)
4. **For CodeQL**: Enable "Code scanning alerts" in Settings → Security → Code scanning

## Customization

Override any input by changing the `with:` values in your workflow files.

For project-specific needs, copy the reusable workflow to your repo and modify directly.