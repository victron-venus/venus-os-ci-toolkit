# Shared coverage publication

Test coverage is collected once by the existing test job. Local test failures and
coverage thresholds stay mandatory. The shared uploader consumes an exact
same-run artifact in a separate job; test code does not receive OIDC credentials.
Public projects opt in through `.release-policy.json` and the existing renderer.

## Enable an existing report

Add a profile for each independent report. Replace the illustrative SHA below
with a reviewed toolkit commit containing `coverage-upload.yml`:

```json
{
  "coverage": {
    "toolkit_ref": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "reports": [
      {
        "name": "python",
        "workflow": "ci.yml",
        "job": "ci",
        "path": "coverage.xml",
        "format": "cobertura",
        "required": false
      }
    ]
  }
}
```

The workflow must already be in `validation_workflows`. `job` identifies its
existing producer; `path` is relative to the repository root, including any
working directory. Use exact filenames, without globs or expressions. Supported
formats are Cobertura XML, Go coverprofile and LCOV. The unique profile name is
also the Codecov component flag. Percentages remain configured in the project's
test command, not in this upload policy.

```bash
# Install once in the toolkit's Python environment, from the toolkit checkout.
python3 -m pip install --require-hashes --only-binary=:all: -r .github/requirements-generator.txt
python3 /path/to/venus-os-ci-toolkit/scripts/install_release.py /path/to/project --coverage-only
python3 /path/to/venus-os-ci-toolkit/scripts/install_release.py /path/to/project --coverage-only --check
```

`--coverage-only` adopts coverage in a project that already has a generated
Quality gate and `single_entry_ci: true`. It refreshes Quality gate, the selected
producers, change-scope helper and workflow contracts/tests/requirements. Review
these CI updates together. In an existing Release pipeline it changes only the
`checks` caller's OIDC permission, retaining the release behavior and comments.
It does not replace release clients, native build helpers, release tests, version
files or release documentation. Missing or ambiguous release callers fail before
any files are written. Its `--check` verifies only this bounded output set; it
does not certify that the full release engine is current. Use the installer
without `--coverage-only` for an independently reviewed full release-toolkit update.

Review the policy and generated diff together. The renderer preserves existing
test commands, comments, action-version labels and thresholds. For a direct call to shared Python/Go CI, it updates
the immutable workflow pin and opts into `coverage-artifact-name`. For a custom
job, it adds one pinned artifact-export step. TypeScript and Rust may use the same
custom-job adapter once their existing test command produces a report; enabling
an uploader alone does not collect coverage.

Each report becomes a separate upload caller after its validator in Quality
gate. The Release pipeline forwards OIDC to Quality gate when needed. Ordinary
test validators keep their current permission caps. Existing
`validation_oidc_workflows` remains available for unrelated, explicitly reviewed
OIDC adapters, but central coverage does not require it.

No profile means no generated coverage changes. The renderer refuses to silently
replace an existing inline Codecov step: review its removal first, preserving
any mandatory upload policy. Matrix producers, aliased/merged producer settings and indirect/custom reusable
workflow producers need a reviewed adapter with an unambiguous single report;
they are rejected rather than guessed. A profile is not a reason to add blanket
coverage percentages to Terraform, firmware YAML or external integration suites.

## Authentication and failure policy

The official, SHA-pinned Codecov action uses GitHub OIDC for trusted public
repository runs. It needs `id-token: write` on the uploader and each caller hop,
not a `BOT_PAT`, Codecov token or new secret. Fork PRs use the official tokenless
path, with no OIDC permission; fork publication is informational. The workflow
rejects unsupported events, private repositories, unsafe paths, malformed or
empty reports and extra files before uploading. It does not execute checked-out
consumer scripts. PR attribution uses the PR head; the tested merge SHA is recorded
separately. Push attribution uses the tested commit.

`required: false` makes failure of the **uploader step** informational, including
bootstrap or integrity errors. Its outcome is explicitly reported; this is not
proof of successful ingestion. `fail_ci_if_error: true` still prevents execution
of an unverified downloaded Codecov binary. Artifact download and report
validation remain mandatory in both modes. `required: true` also fails trusted
uploads on uploader errors. Local test/coverage failures remain failures in every
mode, even when the provider is unavailable.

Start new integrations with reporting enabled, verify accepted PR and protected
default-branch reports on Codecov for the exact commits, then explicitly select
`required: true` where provider availability should block merging. Do not weaken
an existing mandatory uploader during migration. Codecov project/patch status
thresholds are separate; initially keep those informational until a meaningful
baseline exists. Avoid treating generated files as tracked application coverage.

Artifacts use `coverage-<name>-<run_attempt>` with seven-day retention and a hard
error for missing files. To retry publication after a producer succeeded in an
older attempt, rerun **all** jobs so the report is recreated for the new attempt.
Reports are not taken from another run or silently reused after expiry.

## Maintain centrally

The upload implementation and report validation live in this toolkit.
`scripts/render_coverage_upload.py --check` verifies the workflow's embedded
validator matches its canonical source. Consumers only retain their declarative
profile, exported artifact and pinned caller. Generated producer adapters retain
repository-owned test commands; future edits to those commands remain local.

`renovate-ci.json` groups the coverage policy's toolkit digest with workflow digest
updates in one PR. Keep generated pins and policy in agreement; workflow contracts
reject partial updates. The initial profile and permission adoption still requires
one reviewed generated change per project: immutable pins cannot update themselves
when the toolkit merges. Use a fresh, explicitly selected inventory with the fleet
renderer/checker; historical `fleet.json` entries are not a current rollout list.

Official references: [Codecov Action/OIDC](https://github.com/codecov/codecov-action),
[GitHub reusable workflow permissions](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows),
[Renovate Git refs](https://docs.renovatebot.com/modules/datasource/git-refs/).
