# Pinned govulncheck protocol fixtures

These are unmodified golden command-test files from `golang/vuln` v1.8.0.
`sources.json` records their upstream URLs and exact hashes; `LICENSE` is the
upstream BSD license. Tests extract JSON records for individual command scenarios
without running Go or downloading modules.

The main-module binary fixture preserves SBOM messages and has both a clean
scenario and an affected scenario. The common source fixture preserves real
multi-frame call traces; upstream intentionally filters its SBOM, so it is used
for trace classification rather than as a complete accepted scan. Malformed and
wrong-target variants are generated only in the Python tests.
