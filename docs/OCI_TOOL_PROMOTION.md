# Prepare qualified OCI tools for publication and image testing

`scripts/prepare_oci_promotion.py` stages the exact two-architecture candidates
from one successful source/runtime workflow. It neither compiles nor executes
the candidates and does not publish a release, build an image or deploy anything.
The checked-in `promotion.json` is intentionally unreviewed and unbound until the
final runtime run has succeeded and its identities have received independent
review.

For each architecture the reviewed lock must contain candidate and runtime
artifact IDs, SHA-256 digests and compressed sizes, plus all four binary hashes.
It also binds the complete source commit, source-input manifest hash and run ID.
The preparer checks the actual GitHub run, both required jobs per architecture,
artifact metadata and downloaded bytes, runtime/confinement results, candidate
metadata, source-security gate and binary hashes. Changed, missing or expired
inputs are fatal. A replacement run needs a new reviewed lock.

On a disposable GitHub-hosted Linux worker, run as the ordinary runner user:

```sh
python3 scripts/prepare_oci_promotion.py
```

The new `$RUNNER_TEMP/oci-promotion/` contains the original candidate archives,
an index with archive and binary hashes, and `amd64/` and `arm64/` builder contexts.
Each context copies only the three accepted operational binaries into the pinned
distroless base. The original archives retain `oci-probe`, evidence and license
notices. Candidate metadata, including `release: false`, is preserved; later
publication must use a separate reviewed acceptance index. An existing output
directory is never reused.

## Builder image acceptance remains required

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
