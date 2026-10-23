# Patched release signer and verifier

Signed-release consumers use Cosign 3.1.3 source commit
`11926fa5bbbbde47e88fc006b625a17769b743b2`, rebuilt with Go 1.27.3 and the
reviewed `.github/cosign/go.mod` and `go.sum`. This is a downstream patched build,
not Sigstore's official 3.1.3 executable (which embeds Go 1.26.4). The Apache-2.0
license and source provenance are retained beside the locked module files.

The changed direct modules are x/net v0.60.0, x/text v0.42.0 and grpc v1.83.2.
Their transitive updates include x/crypto v0.57.0. No Cosign source code is
patched. Inspect the module-file diff against the pinned upstream commit when
updating this profile. The `+dirty` source marker is intentional and identifies
the changed dependency inputs.

## Build and use

Install Go 1.27.3, clone Sigstore's repository into a fresh trusted directory and
check out the exact commit above. In this project's reviewed checkout run:

```sh
python3 scripts/build_release_signer.py --source /trusted/cosign-source \
  --output /trusted/bin/cosign
GODEBUG=fips140=on /trusted/bin/cosign version
GODEBUG=fips140=on python3 scripts/release_signing.py verify /downloads/release \
  --root /trusted/project --source-sha EXPECTED_REVIEWED_SOURCE_COMMIT
```

Put `/trusted/bin` first in PATH before the verification command. The builder
rejects an incorrect or dirty source checkout and any other Go version. It
restores the upstream module files after building, retains Go's module checksum
verification, uses HTTPS-only module downloads and limits compilation to two
workers. It selects the native Linux/macOS amd64/arm64 target, disables CGO,
removes ambient Go/compiler overrides and verifies final build metadata. It does not sign, publish, install system files or alter credentials.

Generated signing and publication jobs perform this same build from an immutable
checkout and keep `GODEBUG=fips140=on` for all subsequent steps. The setting is
also inherited by GitHub CLI. The signer builder handles only Cosign; generated
release jobs separately install and verify the [qualified GitHub CLI](release-github-cli.md)
before authenticated operations.
Cosign's verified Go runtime restricts every certificate in a verified
TLS chain to supported modern keys (including RSA of at least 2048 bits), preserves
host and trust verification and permits TLS 1.2/1.3 with ephemeral key exchange.
This runtime setting is not a FIPS certificate. Default public Sigstore services
are the supported endpoint profile; custom mirrors, keys and providers require
separate qualification.

## Evidence and scope

The toolkit contract job builds the actual binary, initializes an empty Sigstore
cache, verifies the upstream checksum manifest with its bundled Sigstore evidence
and rejects a modified manifest. Expected publisher:
`keyless@projectsigstore.iam.gserviceaccount.com`; issuer:
`https://accounts.google.com`. The fixture is the public upstream 3.1.3 checksum
file and signature bundle, not a project release or a fabricated signing record.

The same binary is tested against synthetic loopback TLS 1.2 and TLS 1.3 servers.
Ordinary mode establishes HTTP over valid 2048-bit chains and over deliberately
weak 1024-bit root, intermediate and leaf variants. Strict mode retains the strong
connection and rejects each weak-chain variant before any HTTP request. A 404
response is the positive transport calibration. On macOS the fixture explicitly
sets Go 1.27 `x509sslcertoverrideplatform=1` to load only its temporary root file;
it does not modify the keychain or change production trust. This test does not weaken a live
service or publish anything.

On 2026-10-16 binary vulnerability analysis reported 27 reachable-symbol findings
for the upstream executable. This patched dependency/runtime profile leaves two:
GO-2026-6225 (Azure registry credential helper) and GO-2026-5932 (legacy OpenPGP
provider). The supported commands operate on local files: keyless `sign-blob`
and `verify-blob --bundle` with fixed identity/issuer. Signing selects the default
ephemeral signer in `signcommon.signerFromKeyOpts`; verification without a key
reference loads Sigstore's default trusted material. Neither path invokes OCI
registry credential selection or an OpenPGP key provider. This narrow source-path
disposition does not authorize those other features or claim all Cosign features
are free of vulnerabilities. Reassess on every dependency/command change.

Source references:

- [Cosign signing key selection](https://github.com/sigstore/cosign/blob/11926fa5bbbbde47e88fc006b625a17769b743b2/cmd/cosign/cli/signcommon/common.go)
- [Cosign bundle verification](https://github.com/sigstore/cosign/blob/11926fa5bbbbde47e88fc006b625a17769b743b2/cmd/cosign/cli/verify/verify_blob.go)
- [Go whole-chain TLS policy](https://github.com/golang/go/blob/go1.27.3/src/crypto/tls/common.go)
- [Go strict TLS parameters](https://github.com/golang/go/blob/go1.27.3/src/crypto/tls/defaults_fips140.go)
