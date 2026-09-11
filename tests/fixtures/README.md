# Self-Test Fixtures

Minimal consumer-side workflow files + fixture repos for validating toolkit reusable workflows.

## Purpose

These fixtures exercise the toolkit's `workflow_call` contracts without needing a live GitHub Actions runner. Use [act](https://github.com/nektos/act) to run them locally.

## Fixtures

### `python-minimal/`

Minimal Python project: one module, one test, `pyproject.toml` with `[project.optional-dependencies]` dev/test extras.

**Validates:** `python-ci.yml` — lint, type-check, test, coverage upload.

### `python-type-error/`

An intentionally invalid return annotation used by `tests/test_python_typecheck.py`. The contract test executes the reusable workflow’s actual MyPy installation and check commands in a fresh virtual environment. It verifies that the invalid annotation fails, a corrected annotation passes, and `run-type-check: false` skips both installation and checking. Run it with `python -m unittest discover -s tests -p 'test_*.py' -v` after installing PyYAML. Network access is required to install MyPy into the isolated environment.

### `go-minimal/`

Minimal Go module: one package, one test, `go.mod`.

**Validates:** `go-ci.yml` — lint, vulncheck, test, coverage upload.

## How to Update the Toolkit SHA

The consumer workflow files in each fixture pin the toolkit to a commit SHA. When the toolkit changes and you want the fixture to exercise the new version, update the SHA:

```bash
# Find current toolkit HEAD SHA
cd /path/to/venus-os-ci-toolkit
git rev-parse HEAD

# Replace the old SHA with the new one in all fixture workflow files
sed -i 's/@<old-sha>/@<new-sha>/g' tests/fixtures/python-minimal/.github/workflows/ci.yml
sed -i 's/@<old-sha>/@<new-sha>/g' tests/fixtures/go-minimal/.github/workflows/ci.yml
```

The fixture project source files rarely need changes — they exist only to provide something for the CI steps to run against.

## Running with act

```bash
# Install act (macOS)
brew install act

# Validate python-ci.yml
cd tests/fixtures/python-minimal
act -W .github/workflows/ci.yml --container-architecture linux/amd64

# Validate go-ci.yml
cd ../go-minimal
act -W .github/workflows/ci.yml --container-architecture linux/amd64
```

`act` requires Docker. The first run pulls the GitHub Actions runner image. Subsequent runs use the cached image.

## Expected Outcomes

| Fixture | Steps that run | Failure modes |
|---|---|---|
| `python-minimal` | checkout, setup-python, install-deps, ruff check, ruff format --check, mypy, pytest, upload-coverage | ruff errors, test failures, coverage below threshold |
| `go-minimal` | checkout, setup-go, go mod download, golangci-lint, govulncheck, go test, check coverage, upload-coverage | lint errors, test failures, coverage below threshold |

A passing `act` run means the workflow's inputs are wired correctly and the steps execute without syntax errors. It does not substitute for a full GitHub Actions run (no Codecov upload will succeed without a real repo context).
