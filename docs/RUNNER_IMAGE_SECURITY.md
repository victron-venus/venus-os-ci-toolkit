# Runner image security refresh

The runner image is rebuilt from the canonical public base recipe and then the
private recipe. Refreshing its toolchain changes more than a wrapper layer;
qualification must bind the new source, complete OCI manifest and layer set.
An older parent-only or wrapper-only receipt cannot qualify this rebuild.

## Reviewed inputs

`deploy/runner-reserve/Dockerfile` pins the multi-platform image digests for:

- Docker 29.9.2, whose tools use Go 1.26.10.
- Node 24.22.1, retaining the Node 24 LTS runtime.
- Actions runner 2.338.1, including .NET 8.0.32 and Pkcs 10.0.13.
- Ubuntu 24.04, with a fresh package index used for the listed package installs.
  This is not an unqualified full-distribution upgrade; the complete resulting
  image still needs its actual package scan.

The existing GitHub CLI 2.103.0 release manifest remains authoritative. Its
official executables use Go 1.27.3. The Go versions address
[GO-2026-6809](https://pkg.go.dev/vuln/GO-2026-6809); source version strings alone
do not replace inspection and scanning of the actual image payloads.

Docker's current dind image omits the standalone `containerd` executable. The
bootstrap starts its own containerd process, so merely changing `FROM` would
break it. `scripts/install_runner_containerd.py` installs only that executable
from the matching official Docker static archive. The per-architecture archive,
member hash, size and ELF machine are verified against
`.github/containers/runner-containerd.json`. All other Docker tools still come
from the pinned image. No downloaded executable runs on the operator's host.

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
