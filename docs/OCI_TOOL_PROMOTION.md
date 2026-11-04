# Prepare qualified OCI tools for publication and image testing

`scripts/prepare_oci_promotion.py` stages the exact two-architecture candidates
from one successful source/runtime workflow. It neither compiles nor executes
the candidates and does not publish a release, build an image or deploy anything.
The checked-in `promotion.json` binds the fresh artifacts from
[run 41779973736](https://github.com/victron-venus/venus-os-ci-toolkit/actions/runs/41779973736),
source `25d4f9fb419c12889be0428eb25e94617c3ed76e`. Both native source/security
and actual runtime jobs passed on AMD64 and ARM64. Independent audits of the
original evidence archives verified the production package graphs, raw scanner
reports, installed inputs, seccomp/device restrictions and BuildKit confinement.
Staging and the existing reviewed image recipe are enabled for these exact
artifacts. Their actual outputs still need separate acceptance; this does not
approve publication or deployment. The earlier v0.34.1 artifacts are not reused.

## Current source refresh

The source locks now target BuildKit 0.34.2 and Buildx 0.38.2. BuildKit's patch
release fixes the resolver-cache panic affecting 0.34.1
([GHSA-2fqp-xg6f-v7gj](https://github.com/moby/buildkit/security/advisories/GHSA-2fqp-xg6f-v7gj));
the updated upstream sources also carry newer containerd, compression and fsutil
dependencies. Not every advisory repeated in release notes affects the previous
versions; applicability must be checked against the advisory ranges and source.

The first native image attempt,
[41774287612](https://github.com/victron-venus/venus-os-ci-toolkit/actions/runs/41774287612),
failed before image assembly because its Buildx binary scanner reported the
deprecated OpenPGP package and vulnerable `x/net` HTML code. That attempt remains
failed. The replacement locks pin `x/net` 0.62.0 and the corrected `x/crypto`
module layout, while preserving the existing `go-archive` fix and retained
namesgenerator source and notices.

[Source-only preparation 41775119688](https://github.com/victron-venus/venus-os-ci-toolkit/actions/runs/41775119688)
resolved and verified both full upstream trees without building binaries. Its
independently reviewed production closures contain no deprecated OpenPGP imports;
the separate deprecated module remains in the broader module graph. An explicit
production-package guard now rejects its reintroduction. This source evidence
does not substitute for clean scans of the actual replacement binaries.

Go remains 1.26.10 for Buildx and 1.27.3 for BuildKit/runc; runc remains 1.5.2.
The native inputs additionally pin matching zlib development headers so older
hosted images can install the reviewed runtime without removing Python/MySQL
development packages. The installer retains its no-download/no-remove policy;
the added package is included in the installed inventory and vulnerability scan.
Qualify the actual new image before publication.

For each architecture the reviewed lock must contain candidate and runtime
artifact IDs, SHA-256 digests and compressed sizes, plus all four binary hashes.
It also binds the complete source commit, source-input manifest hash and run ID.
The preparer checks the actual GitHub run, both required jobs per architecture,
artifact metadata and downloaded bytes, runtime/confinement results, candidate
metadata, source-security gate and binary hashes. Changed, missing or expired
inputs are fatal. A replacement run needs a new reviewed lock.

The manual `Stage qualified OCI tool bytes` workflow runs only on the default branch when its
`reviewed_commit` input matches the full workflow commit. It uses a disposable
GitHub-hosted Linux worker, a 15-minute limit and read-only contents/actions
permissions. Dispatch it after reviewing that exact recipe commit:

```sh
gh workflow run oci-tool-staging.yml --ref main \
  -f reviewed_commit=FULL_REVIEWED_RECIPE_COMMIT
```

Its only staging command runs as the ordinary runner user:

```sh
python3 scripts/prepare_oci_promotion.py
```

The new `$RUNNER_TEMP/oci-promotion/` contains the original candidate archives,
an index with archive and binary hashes, and `amd64/` and `arm64/` builder contexts.
Each staging context contains the three accepted operational binaries and the
image recipe. Image qualification separately adds the freshly tested cgroup
reader and notices before the context is buildable. The original archives retain `oci-probe`, evidence and license
notices. Candidate metadata, including `release: false`, is preserved; later
publication must use a separate reviewed acceptance index. An existing output
directory is never reused.

`oci-staged-index` contains only the small acceptance index for independent
review of the actual inner archive hashes. `oci-staged-bytes` retains the
original archives and prepared contexts on GitHub for subsequent qualification;
do not download that bulk artifact merely to inspect the index. No candidate
program is executed, no Docker build is started and no release is published by
this workflow. Artifacts expire after 14 and 7 days respectively; replacement
inputs require fresh qualification and lock review.

## Builder image acceptance remains required

The manual `oci-builder-qualification.yml` workflow remains gated by
`builder-qualification.json` and an exact reviewed default-branch commit. It
uses separate disposable native AMD64 and ARM64 workers. It first revalidates
the accepted artifacts, builds the existing source-locked Buildx client with
Go 1.26.10, and builds the small cgroup reader with Go 1.27.3. Both new binaries
must pass their inventory and vulnerability checks before they are used.

The scan directory uses byte-identical copies with explicit mode `0755`.
Trivy's Go binary analyzer requires executable files; `shutil.copyfile` alone
does not preserve those permissions. This caused
[attempt 41781949282](https://github.com/victron-venus/venus-os-ci-toolkit/actions/runs/41781949282)
to stop before assembly with `Incomplete builder tool scan`, even though the
separate Go checks passed. An empty or partial Trivy inventory remains fatal;
do not bypass that check or treat the failed attempt as image acceptance.

The reader accepts no arguments and only reads `/proc/1/cgroup`. Its unit tests
cover a 64 KiB limit, read errors, incomplete input and short/error writes.
Hosted executable checks compare its output with that fixed file and reject
an alternate-path argument. The later container check exercises the same
argument rejection and preserves host PID/start-time correlation.

The accepted standalone BuildKit assembles a COPY-only image under an owned
transient systemd service. The service has a 2-CPU quota, 4 GiB memory limit,
zero swap allowance, 256-task limit and a 10-minute lifetime; actual cgroup
limits and process identity are checked during assembly. The ordinary-user
BuildKit client has a bounded command deadline and connects only through the
owned Unix socket. An OCI archive and a Docker archive come from the same
solve and source timestamp; Docker-export layers are explicitly uncompressed
so their bytes can be compared directly with OCI DiffIDs. The verifier checks every OCI blob, uncompressed layer digest, final
binary and notice before scanning the image. It checks that the Docker
archive has the same configuration and uncompressed layers before importing
it; importer support for arbitrary OCI tar files is not assumed.
Trivy receives this verified Docker archive before import. Its archive reader
accepts Docker tar files and OCI layout directories, but not the OCI tar export.
Attempt 41783797057 reached this boundary and failed before any image scan
verdict; switching formats preserves the already verified configuration and
layer bytes, without extracting or rebuilding them.

Only then does the workflow start the image with Buildx's `docker-container`
driver. It proves the real RUN process inherits the existing CPU/memory
limits, verifies native platform selection, pulls the pinned test base over
registry TLS, and exercises multi-stage builds, persistent cache mounts and
OCI export. It removes only the identified builder container, state volume,
assembly service and imported image reference. Failures retain evidence.
The final image includes upstream, Go, native-package and project notices
from the original accepted archive and reviewed source.

`oci-builder-evidence-{amd64,arm64}` contains review evidence;
`oci-builder-image-{amd64,arm64}` retains the original successful archives on
GitHub. No registry write or release permission is granted. Both native jobs
and independent evidence review are required before a separate publication.

`builder.Dockerfile` is a native rootful candidate with an explicit runc path,
native snapshotter and OCI worker. It uses the base already pinned by the
Headlamp recipe for CA certificates and basic runtime files. It does not promise
support for rootless mode, emulation, Git contexts, CNI, or arbitrary frontend
extensions. These are outside the current native local-context consumers.

Before publishing or adopting the image, review its build invocation and run on
both native architectures: a full final-filesystem scan; actual copied-binary
hash checks; Buildx `docker-container` handshake; registry TLS; local multi-stage
builds and cache mounts; OCI export; resource-limit proof; failure handling and
cleanup of the exact owned container/volume. Preserve the existing runner-image
executor's CPU, memory, disk, identity and cgroup contracts. Record actual image
digests only after those checks; do not infer them from the binary hashes.

Publication of staged tool archives and accepted builder images is a separate
reviewed operation. Full public/private runner images and Headlamp/cloudflared
images then need their own scans and runtime acceptance before activation.
Toolkit issue 261 and k3s-self-healing issue 276 stay open for those downstream
requirements.

## Scoped acceptance of rootful privilege

The final `USER 0:0` in `.github/oci-tools/builder.Dockerfile` is a true positive
for Trivy `DS-0002` and Sonar `docker:S6471`. Its privilege is intentional: this
rootful BuildKit worker creates mount namespaces, mounts and cgroups and invokes
runc. Changing only `USER` would break that implementation; a rootless design
needs separately qualified RootlessKit, bootstrap and cgroup behavior.

Accept this risk only for disposable GitHub-hosted native image qualification
using trusted, reviewed local contexts. This decision does not authorize a
long-lived privileged service, untrusted pull-request builds, household
deployment or access to household credentials. The image still requires full
filesystem scanning, exact-binary verification, resource and isolation probes,
owned-resource cleanup and independent review before publication or adoption.
Compromise of this privileged daemon can compromise its disposable host;
cgroup/resource limits and cleanup are not a container-escape security boundary.

The inline Trivy disposition applies only to that explicit `USER` instruction
and expires on 2026-12-04. All other checks and Dockerfiles remain in scope.
Sonar's individual finding is accepted as an intentional risk, never marked
fixed or false positive. Reassess this decision when execution scope changes.
The original findings are retained in [PR 367](https://github.com/victron-venus/venus-os-ci-toolkit/pull/367)
and its first full-source scan, run `41770467501`.
