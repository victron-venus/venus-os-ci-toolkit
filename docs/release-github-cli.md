# Qualified GitHub CLI for release operations

Generated release jobs install official GitHub CLI **2.103.0**, built with **Go 1.27.3**,
before prepare, candidate publication, stable promotion and final publication.
`.github/gh/release.json` pins both archive and extracted executable SHA-256 for
native Linux/macOS amd64/arm64. The checksums were checked against the official
[release](https://github.com/cli/cli/releases/tag/v2.103.0) and all four binaries
were inspected with `go version -m`. Source commit:
`7948e8056e45ac3c729b829e0eafe2d0797a95c6`. The upstream MIT notice is retained in
`.github/gh/LICENSE`; `gh licenses` lists the binary's dependency notices.

The installer requires Go 1.27.3 to run a small standard-library-only HTTPS
downloader under `GODEBUG=fips140=on`. It rejects HTTP, HTTPS downgrade redirects,
more than ten redirects, oversized responses and errors. No operator credential
is sent to the download endpoint. The installer checks the archive, reads exactly
one regular executable member without extracting paths, checks its hash and
version, and atomically replaces the selected output only after validation.
`--check` needs no Go compiler: the exact binary hash identifies the inspected
Go build and requires the same FIPS runtime setting. Unsupported platforms fail.

`--archive FILE --output PATH` installs an already downloaded archive without
network access or a Go compiler. The same size, archive hash, native executable
hash, FIPS runtime setting, version and atomic replacement checks still apply;
an invalid archive never falls back to downloading. Private runner image builds
use this mode after a bounded, unauthenticated HTTPS download. This does not
change the online installer's Go-based FIPS transport profile.

For local release helper use, select Go 1.27.3 on PATH, then:

```sh
export GODEBUG=fips140=on
python3 scripts/install_release_gh.py --output .release-tools/gh
export PATH="$PWD/.release-tools:$PATH"
export GH_HOST=github.com
python3 scripts/install_release_gh.py --check "$(command -v gh)"
python3 scripts/release.py doctor
```

Keep normal maintainer authentication in place. Do not use arbitrary extensions,
custom hosts, insecure TLS options, aliases or a replacement `gh` in this profile.
CI selects a dedicated CLI configuration directory and the existing explicit
GitHub token; permissions and publication approvals do not change. Build tools
and GitHub Actions have their own pins and are not described as part of this
runtime qualification.

The actual executable is tested against synthetic HTTPS endpoints under TLS 1.2
and 1.3. Ordinary-mode controls prove the fixtures work; FIPS mode accepts the
strong chain and rejects 1024-bit RSA at the leaf, intermediate and root before
HTTP. The test uses only temporary trust and synthetic tokens. It neither changes
the OS trust store nor connects to a real authenticated service.

## Scoped dependency assessment — 2026-10-22

This covers `gh api`, `gh repo view`, `gh workflow run`, `gh run list` and
`gh release upload` in the checked-in release helpers. The four official CLI
artifacts report Go 1.27.3, including the certificate distrust fix for
[GO-2026-6809](https://pkg.go.dev/vuln/GO-2026-6809). This does not mean all CLI
commands or dependencies are free of advisories.

Binary scanning still reports one upstream finding:
[GO-2026-5932](https://pkg.go.dev/vuln/GO-2026-5932), concerning unmaintained
`golang.org/x/crypto/openpgp`, with no fixed version. An import scan of the exact
upstream source found no direct OpenPGP imports. Its inspected
`go list -deps ./cmd/gh` graph contains 1,107 packages and no OpenPGP package;
source-mode `govulncheck` 1.8.0 with Go 1.27.3 on Linux and Darwin, each on
AMD64 and ARM64, reports no reachable or imported package vulnerabilities and
one module-only record. The scanned `go.mod` and `go.sum` match the exact
upstream commit. The wildcard binary report
therefore does not establish a linked or reachable OpenPGP path in that source
graph. The binary scanner result is still recorded, not reported as a clean
binary scan. See the
[tracked assessment](https://github.com/victron-venus/venus-os-ci-toolkit/issues/245).
Existing FIPS transport requirements, checksum validation and synthetic TLS-chain tests remain mandatory.

Reassess after an upstream release, a command change or a new advisory. This is
a scoped assessment of the stated binaries and release operations, not an
indefinite exception or a blanket clean scan.
