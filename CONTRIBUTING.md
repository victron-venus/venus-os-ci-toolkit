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
actionlint checks workflow syntax and semantics. Required GitHub checks also run
CodeQL and Trivy; inspect every check on the current PR revision before merging.

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
