# Maintained Cosign build inputs

Upstream: https://github.com/sigstore/cosign
Source: `11926fa5bbbbde47e88fc006b625a17769b743b2` (v3.1.3).
License: Apache-2.0; the complete upstream license is retained in `LICENSE`.

These are the upstream module files with x/net upgraded to v0.62.0,
x/text to v0.43.0, grpc to v1.83.2, klauspost/compress to v1.18.7,
go-chi/chi/v5 to v5.4.0, and go-jose/v4 to v4.1.6
(including their required transitive updates). The compress update fixes
CVE-2026-63209; it is the upstream security-only patch release. The chi update
removes the affected ClientIPFromXFF version (GHSA-j6qf-3m9c-vwj3); no claim
is made that the Cosign CLI exposes that HTTP middleware. Build with
Go 1.27.4. The resulting executable is a downstream patched build, not an official Sigstore binary. Source code is otherwise unchanged.
The renderer vendors these exact files and the fail-closed builder into signed
release consumers. See `docs/release-signer.md` for provenance and limitations.

The go-jose update addresses [GHSA-mgcc-cww5-fp2q](https://github.com/go-jose/go-jose/releases/tag/v4.1.6).
The upstream default now rejects JWK RSA moduli larger than 4096 bits; this build
retains that default. Existing binaries are unchanged until rebuilt from these
locks and checked through the normal signing qualification.

The x/net update addresses [GO-2026-6860 / CVE-2026-78661](https://pkg.go.dev/vuln/GO-2026-6860),
which fixes quadratic text coalescing in the HTML parser. Existing binaries
remain unchanged until rebuilt and qualified.

The OpenTelemetry trace dependency is updated to `1.47.0` for
[GHSA-q7gw-24fr-5p69](https://github.com/open-telemetry/opentelemetry-go/security/advisories/GHSA-q7gw-24fr-5p69),
which repairs configured attribute-length truncation for valid replacement
characters. The module graph, actual source build, signature/tamper tests and
weak/strong TLS calibration qualify a new signer; earlier binaries and receipts
retain their original identities.
