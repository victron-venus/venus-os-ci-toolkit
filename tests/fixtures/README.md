# Self-Test Fixtures

Minimal consumer-side workflow files + fixture repos for validating toolkit reusable workflows.

## Purpose

The toolkit's [contract-test workflow](../../.github/workflows/contract-tests.yml)
calls the current Python and Go reusable workflows against these fixture projects.
Local Python tests also exercise individual contracts; see
[CONTRIBUTING](../../CONTRIBUTING.md) for dependencies and commands.

Standalone `.github/workflows/ci.yml` files inside `python-minimal` and
`go-minimal` retain historical toolkit pins. They exercise that older revision,
not the current checkout. Their results do not qualify current toolkit changes.

## Fixtures

### `python-minimal/`

Minimal Python project: one module, one test, `pyproject.toml` with `[project.optional-dependencies]` dev/test extras.

**Validates in the current contract workflow:** `python-ci.yml` — lint, type-check,
tests and coverage artifacts. Hosted publication requires its own evidence.

### `python-type-error/`

An intentionally invalid return annotation used by `tests/test_python_typecheck.py`. The contract test executes the reusable workflow’s actual MyPy installation and check commands in a fresh virtual environment. It verifies that the invalid annotation fails, a corrected annotation passes, and `run-type-check: false` skips both installation and checking. Run it with `python -m unittest discover -s tests -p 'test_*.py' -v` after installing PyYAML. Network access is required to install MyPy into the isolated environment.

### `go-minimal/`

Minimal Go module: one package, one test, `go.mod`.

**Validates in the current contract workflow:** `go-ci.yml` — lint, vulnerability
checks, tests and coverage artifacts.

## Updating a standalone consumer

Changing only a historical SHA is insufficient: review the new workflow's inputs,
permissions, supported toolchains and dependency installation mode as well. The
old Go consumer selects Go 1.23, and the old Python consumer omits the current
packaged-project lock opt-in. Neither is a current starter template. Use
[the starter guide](../../templates/starter-workflows.md) for new consumers.

The toolkit's current contract workflow uses local reusable references and needs
no SHA replacement to test the checked-out implementation.

## Running historical consumers with act

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

A passing `act` run covers only the selected historical pin and the steps that
actually executed. It does not substitute for current contract checks or a full
GitHub Actions run with real permissions, artifact storage and Codecov context.
