# Maintained Cosign build inputs

Upstream: https://github.com/sigstore/cosign
Source: `11926fa5bbbbde47e88fc006b625a17769b743b2` (v3.1.3).
License: Apache-2.0; the complete upstream license is retained in `LICENSE`.

These are the upstream module files with x/net upgraded to v0.60.0,
x/text to v0.42.0, grpc to v1.83.2, and klauspost/compress to v1.18.7
(including their required transitive updates). The compress update fixes
CVE-2026-63209; it is the upstream security-only patch release. Build with
Go 1.27.2. The resulting executable is a downstream patched build, not an official Sigstore binary. Source code is otherwise unchanged.
The renderer vendors these exact files and the fail-closed builder into signed
release consumers. See `docs/release-signer.md` for provenance and limitations.
