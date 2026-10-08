# Fail-closed Scorecard scanner

Build with Go 1.27.2 and run with Git available on PATH:

```sh
go build -mod=readonly -o scorecard-scan .
GITHUB_AUTH_TOKEN="$SCORECARD_TOKEN" ./scorecard-scan \
  -repo "$GITHUB_REPOSITORY" -commit "$GITHUB_SHA" -output results.sarif
```

The token must be able to read the repository metadata required by Scorecard.
The workflow can fall back to `github.token`; if its permissions cannot complete
an enforced check, the job fails before SARIF upload. The scanner does not publish
to OpenSSF, upload an artifact, or change repository settings.

One official Scorecard v5.5.0 `Run` result is validated and passed to the official
`Result.AsSARIF` formatter. Every expected check must appear exactly once. All
16 enforced checks must have a score from 0 through 10 and no runtime error,
with one precise exception: in v5.5.0 `Packaging` returns -1 for the ordinary
absence of a recognized publishing workflow. The adapter accepts only version 2
of that check with no error, reason exactly `packaging workflow not detected`, and
one `packagedWithAutomatedWorkflow` probe with a false outcome. Unknown reasons,
failed or missing probes, and runtime errors remain fatal. Upstream Packaging
returns only 10, that absence result, or a runtime error; neither normal result
creates a Packaging SARIF alert. The official formatter remains unchanged.
See [the pinned evaluator](https://github.com/ossf/scorecard/blob/c395761df6afe1a69e476bc60a013a94bcbc153f/checks/evaluation/packaging.go).
The two checks disabled by the upstream reporting policy, Contributors and
Signed-Releases, are still scanned and must be present; an inconclusive score
for either remains unreported, as in the original action.

The policy, thresholds, SARIF rule IDs, locations, tool identity (`Scorecard`)
and categories retain the behavior of
[the pinned upstream action](https://github.com/ossf/scorecard-action/blob/55891bbd73f2425e97637d96e306fc9d491d0b21/policies/template.yml).
The test fixture is a verbatim copy of that Apache-licensed policy.

GitHub branch and repository metadata checks upstream require a HEAD scan.
The returned factual commit SHA must match the mandatory `-commit` argument;
a queued workflow whose HEAD moved fails instead of attributing findings to the
wrong commit. Any scan error, missing check, invalid score or commit mismatch
removes stale output and returns an error. SARIF is written atomically only after
validation and formatting succeed. The scanner exits with an error after a 15-minute deadline even when an upstream
rate-limit wait ignores context cancellation. A late scan result cannot write SARIF.

Dependencies are locked by `go.mod` and `go.sum`. OSV Scanner is updated to 2.6.0
to replace the obsolete whole Docker module with the maintained Moby client/API
modules. Additional transitive patches address reported advisories. YAML v4 is
explicitly resolved to rc.3 because Scorecard's actionlint parser needs its rich
error API; the newer YAML API used by OSV's unrelated command test helper is
incompatible. No vulnerability exclusions are added.

```sh
go test -mod=readonly ./...
go vet -mod=readonly ./...
go mod verify
```

Regression tests check incomplete scans, stale output removal, commit mismatch,
a single scan invocation, the exact upstream reporting policy, and official
SARIF generation for both clean results and findings with file locations.
