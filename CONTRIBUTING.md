# Contributing

Use a GitHub issue for reproducible bugs and proposed improvements, and a pull
request for changes. English reports and contributions are welcome. Include the
affected toolkit revision, consumer policy, expected result and redacted logs.
Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Development and tests

Use Python 3.12 or later and actionlint 1.7.12. Create an isolated environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install --require-hashes --only-binary=:all: -r .github/requirements-generator.txt
bash scripts/ci.sh
```

The unittest suite exercises the Python generators, vendored release engine,
rejected authorization/provenance inputs and generated workflow contracts.
actionlint checks workflow syntax and semantics. Hash-locked Ruff checks Python
source and tests for undefined names, invalid constructs, import mistakes and
unused variables; its error-focused rules are in `ruff.toml`. Required GitHub checks also run
CodeQL and Trivy; inspect every check on the current PR revision before merging.
CodeQL covers Python, GitHub Actions and the Go Scorecard adapter. The Go job
rebuilds the locked module for extraction and participates in the same required
quality gate, including changes to documentation.

Sonar's automatic analysis includes the release helpers in `scripts/`. Keep
whole-file exclusions out of the first-party executable source. Investigate
individual findings and record any demonstrated false positive with its
reason; do not hide the rest of a file to suppress one finding. The supported
automatic-analysis settings are documented by
[SonarSource](https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/automatic-analysis#additional-analysis-configuration).
Keep reproducible false-positive assessments in
[the static-analysis review notes](docs/STATIC_ANALYSIS.md).

Add regression tests for bug fixes and tests for major new functionality. Test
both accepted and rejected inputs at trust boundaries. Preserve old consumer
behavior unless a migration is documented. Fix reported warnings; explain any
narrow false-positive annotation next to the affected code.

Edit generator source rather than only its output. Regenerate affected workflows
and verify that a second generation is clean. See [consumer installation and
interfaces](docs/CONSUMER_GUIDE.md) and [release operations](docs/APPLICATION_RELEASES.md).
Describe operator impact and compatibility changes in the PR. Never commit
credentials, private operator inventories, state files or production payloads.

Each toolkit revision is identified by its Git commit. Consumers pin reviewed
immutable commits and explicitly regenerate; updating this repository does not
automatically update their installed copy. Follow each consumer's build and
device acceptance procedure before publishing an application release.

## Review, licensing and coding conventions

Contributions must be compatible with the [MIT license](LICENSE); retain
third-party notices and identify copied or vendored source. Python code follows
PEP 8 naming and readable explicit control flow; use the checked-in Ruff rules.
Go code follows gofmt and go vet; shell entry points use explicit error handling
and quoted arguments; workflow YAML uses immutable third-party action pins and
least-privilege permissions. Keep inline JavaScript syntax checked. Terraform
examples follow `terraform fmt` and the checked-in provider lockfiles. Required
checks enforce the relevant configured tools; this is not a claim that every
possible style rule is enabled for every language.

The maintainer reviews scope, compatibility and reported evidence under
[GOVERNANCE.md](GOVERNANCE.md). All participants follow
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Major new functions require tests; every
bug fix requires a focused regression when technically feasible and an explicit
explanation otherwise. Document test commands/results in the pull request.
