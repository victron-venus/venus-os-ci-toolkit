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
result is explicitly versioned `+victron.security.1`, not presented as an unmodified
official binary. The image retains upstream and compiled-dependency license and
notice files, exact package inventory and binary hashes under
`/usr/local/share/licenses/victron-go-tools/`.

Docker's dind image omits the standalone `containerd` executable used by the
runner bootstrap. This source build supplies that daemon together with the
matching `ctr` and `containerd-shim-runc-v2`; it does not mix standalone and
shim versions. Native image/bootstrap qualification remains necessary.

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
Any residual finding requires documented component-specific review; this change
adds no blanket suppression or automatic exception.
