# Runner image security refresh

The runner image is rebuilt from the canonical public base recipe and then the
private recipe. Refreshing its toolchain changes more than a wrapper layer;
qualification must bind the new source, complete OCI manifest and layer set.
An older parent-only or wrapper-only receipt cannot qualify this rebuild.

## Reviewed inputs

`deploy/runner-reserve/Dockerfile` pins the multi-platform image digests for:

- Docker 29.9.2, retaining its engine, CLI and the reviewed runc executable.
  The affected containerd, Buildx and Compose executables are rebuilt separately
  with Go 1.26.10 and the fixed module locks described below.
- Node 24.22.1, retaining the Node 24 LTS runtime.
- Actions runner 2.338.1, including .NET 8.0.32 and Pkcs 10.0.13.
- Ubuntu 24.04, with a fresh package index used for the listed package installs.
  This is not an unqualified full-distribution upgrade; the complete resulting
  image still needs its actual package scan.

The existing GitHub CLI 2.103.0 release manifest remains authoritative. Its
official executables use Go 1.27.3. The Go versions address
[GO-2026-6809](https://pkg.go.dev/vuln/GO-2026-6809); source version strings alone
do not replace inspection and scanning of the actual image payloads.

## Docker tool source and dependency updates

The official release sources for containerd 2.4.2, Buildx 0.38.1 and Compose
5.6.1 still include older `golang.org/x/net` versions. A recent Go compiler alone
does not fix the separately imported HTTP/2 package. The canonical recipe
builds those three components from their exact upstream commits, with
`golang.org/x/net` 0.60.0 and its required compatible module graph. Buildx also
uses `github.com/moby/go-archive` 0.3.0. These address
[GO-2026-6611](https://pkg.go.dev/vuln/GO-2026-6611) and the
[go-archive advisory](https://github.com/moby/go-archive/security/advisories/GHSA-hfg8-hc9c-6c3h).

`.github/containers/runner-go-tools.json` binds each release tag, commit, source
archive hash, original locks and reviewed replacement locks. The separate
`inputs.json` next to each lock binds it back to the original source, so changing
a source pin without revalidating its locks fails before compilation. Containerd's
locks follow its upstream
[release/2.4 security backport](https://github.com/containerd/containerd/commit/1350ed10d68320f0f3e944b86b92c4dfdf4791a3),
including the required x/crypto, x/term and x/text changes. The build uses these
module locks with `-mod=readonly`, rather than silently relying on the old vendor
tree. Buildx and Compose have similarly reviewed, checked-in minimal lock updates.

Buildx's release imports the legacy Docker module only for its frozen
`namesgenerator` utility. The build retains the exact vendored utility source,
LICENSE and NOTICE under Buildx's internal package, changes its single import,
and removes the unused legacy module requirement. The manifest binds every
retained file and the import's before/after hashes; the separate lock record
binds the same adjustment. The utility's code and behavior are unchanged.
Runtime provenance rejects any remaining legacy Docker package and retains
the original utility notices. Production and unit-test dependency graphs are
checked with readonly locks, and the normal upstream tests still run. This
reduces an unnecessary dependency without adding a scanner exception.

`scripts/build_runner_go_tools.py` verifies the source before extraction and
allows only the manifest's exact internal symlinks. It checks the original and
fixed locks, uses the public Go checksum service without inherited private-module
exclusions, runs the upstream root-module unit scope and builds the executables.
Containerd excludes its integration tree; its separate API module is not part of
this unit-test claim. One source-hash-bound containerd test correction checks
that a nonempty `GOMAXPROCS` preserves the actual pre-call runtime value, instead
of assuming it equals the machine's CPU count. This keeps the assertion valid
under the build's explicit `GOMAXPROCS=2` bound and container-aware Go defaults;
no production code or test selection changes. Both original/result test hashes
are bound to the source manifest and rechecked after compilation.
Buildx uses its upstream unit-test setting
`SKIP_INTEGRATION_TESTS=1`; Compose excludes its daemon-dependent e2e tree. The
build preserves the respective production build tags and version flags. Each
result carries an explicit `+victron.security.N` revision, not presented as an unmodified
official binary. The image retains upstream and compiled-dependency license and
notice files, exact package inventory and binary hashes under
`/usr/local/share/licenses/victron-go-tools/`.

Docker's dind image omits the standalone `containerd` executable used by the
runner bootstrap. This source build supplies that daemon together with the
matching `ctr` and `containerd-shim-runc-v2`; it does not mix standalone and
shim versions. Native image/bootstrap qualification remains necessary.

The containerd security revision 2 lock also updates `github.com/cilium/ebpf`
from 0.17.3 to 0.22.0, the first fixed release for
[GHSA-xhgw-qwwf-pg32](https://github.com/advisories/GHSA-xhgw-qwwf-pg32).
Its upstream source revision is unchanged. The revised binary version and build
revision identify this dependency change separately from security revision 1.
Lock preparation and cross-compilation alone do not qualify a runner image;
the normal upstream unit tests, image scans and runtime acceptance remain required.

The existing contract-tests adapter tests and compiles all three components in
separate, sequential Linux jobs when their inputs change, using the same builder
script and Go 1.26.10. Each job has a 45-minute bound; a timeout fails the check.
These jobs need no image push, private CA, site credentials or new runner. They
retain source/package/license/binary-hash provenance. This is native amd64 source
qualification; it is not an arm64 or complete image qualification claim.

For updates, review the upstream release and advisory disposition first. Refresh
the immutable archive and original-lock hashes, then regenerate only the required
module updates with the pinned Go toolchain. Review the complete lock diff and
refresh the separate source binding. Keep the downstream suffix until the
release is unmodified. Dependency bots may propose upstream pins but cannot skip
this lock-origin contract. Run the boundary tests, all three source build jobs
and complete final-image scans; rerun the sandbox/bootstrap and cold-job gates
before deployment. A source pin or binary change invalidates prior applicability
or runtime evidence.

### Nonroot source-build unit stage

The `patched-go-tools` stage installs packages and provisions its account and
directories as root, then runs all three source builds—containerd, Buildx and
Compose—as `10001:10001`, with nonzero real and effective UIDs. It does not switch
back to root before the next `FROM`. The final runner identity remains
`1001:1001`; this change concerns the temporary source-build stage.

The builder owns its home, `/opt/victron-go-tools-build`,
`/opt/victron-go-tools`, and the explicitly configured absolute `GOPATH`,
`GOMODCACHE` and `GOCACHE` directories. Temporary storage remains writable.
`/opt/victron-go-tools-inputs`, including the builder script, manifest and
dependency locks, remains root-owned, readable and not writable by the builder.
The Dockerfile checks the stage identity and writable paths before the source
builds. Each builder invocation sets `umask 022` so exported binaries and notices
remain readable, with executable permissions retained where required.

Source pins, lock verification, package selection, tags, concurrency and timeouts
remain unchanged. `go test -count=1 -v` retains individual test and skip outcomes
and disables Go's test-result cache when the build step executes. Upstream tests
that require root may skip under this identity.
Skipped tests are not passing tests or coverage of privileged behavior. There
is no fixed claim that exactly two tests skip: additional upstream tests inspect
the UID. The existing integration exclusions remain in place; this change adds
no test filters or blanket skip setting. Privileged integration,
sandbox/bootstrap and cold-job acceptance remain separate gates.

The host `--ci` jobs use exclusive Actions temporary directories. They verify
source compilation and tests, but do not exercise Docker-stage ownership,
`USER` transitions or `/opt` permissions. Qualification also requires an actual
image build that executes all three builders under the intended identity.
Retain UID/EUID and writable-path observations, source provenance and test
outcomes, including upstream skip names and reasons. Cached Go results or reused
Docker layers are not evidence of fresh test execution. Contract tests and a
successful source job alone do not qualify an image or establish deployment.

#### Troubleshooting writable paths

If preparation, extraction, Go downloads, compilation or notice collection fails
with permission errors, inspect the effective UID/GID, `HOME`,
`go env GOPATH GOMODCACHE GOCACHE`, the temporary directory and ownership of the
two writable `/opt` directories. Correct provisioning before `USER`; do not make
the input manifest writable, switch the builds back to root, grant additional
capabilities, suppress failures or filter failing tests. Preserve the failed
build log and distinguish permission failures from timeouts or resource limits.
An observed resource limit is not a measured memory peak.

### Advisory metadata reassessment (2026-10-27)

The [2026-10-27 advisory review](RUNNER_ADVISORY_REVIEW_2026_10_27.json)
records 51 updated advisory fingerprints covering 52 findings. The changes are
references, vendor severity, CVSS, modification dates and one expanded title.
The retained descriptions, package/version status and source-applicability
reasons are unchanged. CVSS and vendor severity changes are recorded as
substantive assessments, not dismissed as harmless formatting. The manifest
binds each old/new fingerprint, changed fields, source documents and the scanned
source/database identity. The existing exact package, binary and prerequisite
checks remain mandatory; a subsequent metadata change still fails closed.

This review does not rewrite or accept the original failed scan. Updated source
and passing unit tests do not qualify an image, a host kernel or deployment.
Complete image and runtime qualification remain separate requirements.

### Exact retained runc applicability

The module scanner's runc findings are retained. For the exact Linux amd64 binary
in `runner-runc-applicability.json`, the upstream release source and its matching
CGO/build-tag dependency graph import only `golang.org/x/net/bpf`. Neither the
external `golang.org/x/net/http2` package from
[GO-2026-6611](https://pkg.go.dev/vuln/GO-2026-6611) nor
`golang.org/x/net/dns/dnsmessage` from
[GO-2026-5942](https://pkg.go.dev/vuln/GO-2026-5942) is imported. The actual binary's
package/symbol inventory independently corroborates that graph. Its Go standard
library was built with Go 1.26.10.

This is a narrow non-applicability assessment for two advisory IDs and one
binary hash, not an ignore rule or a zero-findings claim. No other x/net finding,
architecture, component or changed binary inherits it. The raw full scan remains
part of image qualification; the JSON record contains only public source and
binary identities, not deployment inventory.

### Source-bound libbpf backport

The installed Ubuntu `libbpf1` 1.3.0 library also contains code affected by
[CVE-2026-98626](https://ubuntu.com/security/CVE-2026-98626), independently of the
scanner's Linux-header package attribution. Header applicability cannot clear
that installed library.

The `patched-libbpf` stage rebuilds the pinned Ubuntu `1.3.0-2build2` source with
the single [upstream relocation fix](https://github.com/torvalds/linux/commit/394ae398337c5f87e567f6cd63b937fc2b2f6ddc).
The resulting package is `1:1.3.0-2build2+victron1`; its SONAME remains
`libbpf.so.1` and its library filename remains `libbpf.so.1.3.0`. No upstream
version is claimed. Builds run as UID 10001 using the distro package rules and
two build workers. Quilt remains applied through the after-build hook, and the
source hash is rechecked after compilation.

Qualification compares the complete versioned public symbol set, ELF
architecture/type and SONAME, then runs a small ordinary userspace BTF/API
probe. It does not exercise privileged kernel BPF operations. The final image
retains the original licenses and distro copyright, pinned source manifest and
patch, `.buildinfo`, symbol evidence and build provenance under
`/usr/local/share/victron-libbpf/`. The installed library is checked against the
actual rebuilt package hash before temporary package files are removed.

### Payload-bound scan acceptance

`scan_acceptance_schema: 1` explicitly enables the new assessment contract.
Historical manifests and failed runs retain their original meaning and are
not accepted retroactively. The policy is immutable data inside the hash-bound
hosted helper, with per-CVE [human-readable review](RUNNER_SCAN_APPLICABILITY.json).

The scanner still reports every finding at every severity. Its raw count and
zero-raw-findings gate remain unchanged. A separate `scan-acceptance.json`
records each assessed occurrence, each unresolved occurrence and failed
prerequisites. Its binding includes source commit, run/attempt, stage, original
archive/index, runtime manifest/config, platform, scanner identity, actual
database file hashes, raw report hash, input projection hash and effective-file
evidence hash. Main database and metadata hashes are mandatory. Java database
and metadata hashes must both be present or both absent; absence is accepted
only for the explicitly reviewed non-Java target types. Native site scanning
retains its stricter Java database requirement.

For each HIGH/CRITICAL occurrence, the gate matches exact advisory ID, scanned
package/version/PURL, target, source package, location and verified image layer.
It also hashes all advisory fields except image/package-location fields, so new
descriptions, references, severities, fixes or other advisory changes require
review even when the CVE ID is unchanged. A newer database is allowed only when
those advisory contents and all other evidence still match.

The effective-files reader checks every runtime layer hash and uncompressed
diff-ID, applies ordered whiteouts, and resolves selected final ancestors. It
never extracts or executes image files. Selected hardlinks, symlink ancestors,
stale dpkg metadata and mismatched package checksums fail. The exact 990 regular
files in the reviewed `linux-libc-dev` payload match an Ubuntu-signed archive
chain; its complete 1042-entry inventory and modes must match again in each
image. The sanitized conntrack header is separately pinned. Kernel/module and
perf payloads must be absent. This establishes the header package's contents,
not the security of the host kernel.

The two runc module findings additionally require the exact previously reviewed
binary SHA256 and final file identity. The libbpf backport is an unconditional
prerequisite, even if the raw scanner reports no libbpf finding. The gate binds
its installed package/library, exact source and patch, `.buildinfo` package
checksum, ABI evidence and ordinary userspace probe. It refuses the known old
library. New or changed findings and changed effective payloads remain unresolved;
there is no module-wide, package-wide or image-wide exception.

## npm and action runtimes

Global npm remains on major 11 to retain its CLI contracts, including the array
returned by `npm pack --json`. The reviewed distribution is npm 11.21.1 with
two bundled dependencies replaced by official packages: undici 6.29.0 becomes
6.29.1 and ip-address 10.7.2 becomes 10.7.4. This is a documented local dependency patch, not a claim that
upstream npm 11.21.1 already contains that fix.

The installer checks both registry SHA512 integrities and pinned SHA256 hashes,
rejects links, duplicate or noncanonical archive paths, and permits changes only
under `node_modules/undici/` and `node_modules/ip-address/`. It verifies the
original consumers: node-gyp accepts undici `^6.25.0` and socks accepts ip-address
`^10.1.1`. The replacements satisfy those ranges; there is
no broad override, lifecycle script or network dependency resolution.
`.github/containers/runner-npm.json` pins the complete assembled file inventory
hash. The installed `npm/runner-bundle-lock.json` records every original and
resulting file hash/mode plus the upstream package identities. The upstream
published bundles do not contain an npm lockfile, so this explicit bundle lock records
the patch without pretending to be an upstream resolver lock.

The image build runs `qualify-npm.cjs`: offline package creation, installation,
lockfile generation, clean `npm ci`, local binary execution and dependency
inspection. An intentionally failing postinstall script proves that lifecycle
hooks remain disabled in this probe.

Actions runner's [Node action handler](https://github.com/actions/runner/blob/e78522a9a7b01c3bcd7ac0da6e3a9bb6cd513a76/src/Runner.Worker/Handlers/NodeScriptActionHandler.cs)
invokes `externals/<runtime>/bin/node` directly. The image removes only the
unused npm packages and their exact npm/npx launchers from its pinned Node 20
and Node 24 distributions. It preserves each action engine byte-for-byte and
leaves other tools in place. The pruner refuses changed package versions or
unexpected paths, and the build executes both retained Node engines. Workflow
shell steps continue to receive the separately maintained global npm.

Relevant upstream releases are [npm 11.21.1](https://github.com/npm/cli/releases/tag/v11.21.1),
[undici 6.29.1](https://github.com/nodejs/undici/releases/tag/v6.29.1), and
[Docker 29.9.2](https://docs.docker.com/engine/release-notes/29/#2992).

## Build and qualification boundary

The optional [hosted artifact workflow](RUNNER_IMAGE_ARTIFACTS.md) performs the
build, OCI integrity and full-scan stages with an invalid CA sentinel. It retains
raw evidence without publishing or deploying images; site configuration and
runtime qualification remain separate.

1. Freeze a reviewed source commit and transfer only the exact Docker build
   context allowlist. Supply the existing public guard CA separately; never
   copy its private key or rotate credentials as part of an image rebuild.
2. Build the public base, then the private derivative against that exact new
   base digest. Retain build logs, input hashes, platform/config/layer descriptors
   and any provenance statements. Record what each statement actually proves.
3. Inspect the actual binaries and complete package inventory. Run the full
   current vulnerability scan without severity filters or ignore rules. Keep
   failed scans as evidence; a successful image build is not a security pass.
4. Perform the reviewed sandbox/bootstrap checks against the new image and
   actual runtime before changing repository routing. Retain exact Node, npm,
   Docker, containerd and runner results.
5. A bounded-memory profile remains a candidate until real cold jobs pass with
   the expected image, hard limits, start/end observations and zero memory
   limit/OOM events. Source tests and a bootstrap probe do not substitute for
   that acceptance.

The compiler toolchain requires `linux-libc-dev` userspace headers. Scanners may
map source-kernel advisories to that package; those records do not establish
that executable kernel code or the host's vulnerable kernel is in this image.
Keep every finding, its package/file evidence and the host assessment separate.
Any residual finding requires documented component-specific review. The
versioned payload assessment above enforces only the explicit reviewed
occurrences; it adds no blanket suppression or scanner ignore rule.
