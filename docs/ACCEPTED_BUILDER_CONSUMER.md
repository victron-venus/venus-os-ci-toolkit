# Consume the qualified native builder without rebuilding it

The manual runner artifact workflow imports the exact builder images accepted in [run 41787720088](https://github.com/victron-venus/venus-os-ci-toolkit/actions/runs/41787720088), source d42d0f0e7976a34d99a6b85f5d424bb148ffc909. Both native architectures passed image scans, TLS pulls, actual RUN/cgroup checks, multistage builds, persistent cache, OCI exports and owned cleanup. This does not qualify a new final runner image or authorize deployment.

The consumer remains amd64 and uses the same two sequential hosted jobs, public then private. The lock retains arm64 identities for a future reviewed consumer; this change does not claim an arm64 runner build.

## Immutable inputs and ownership

`.github/oci-tools/accepted-builder.json` binds the successful manual run/attempt, source, original artifact IDs, ZIP sizes/digests, both inner archive sizes/digests, native manifest/config, rootfs DiffIDs, copied files and every small evidence member. These remain the image identity and qualification contract regardless of its storage location.

`.github/oci-tools/accepted-builder-transport.json` selects the download source explicitly. Its `release` mode pins one published stable release ID, tag and source commit; the provenance asset; and the original OCI, Docker and evidence asset IDs, names, sizes and SHA-256 digests for both architectures. Acquisition checks release ownership and uploaded state, streams bounded original bytes, and replays the same evidence and OCI/Docker verification. It neither resolves `latest` nor calls Actions APIs or checks historical artifact expiry. A missing or changed release asset fails without a fallback.

The current transport is [stable v0.1.12](https://github.com/victron-venus/venus-os-ci-toolkit/releases/tag/v0.1.12), release ID `433653669`, source `31427ec808648f30b98fe81be466cd91f2971e88`. It was promoted unchanged from [RC v0.1.12-rc.1](https://github.com/victron-venus/venus-os-ci-toolkit/releases/tag/v0.1.12-rc.1) after its package, source, security and fuzzing checks. That toolkit source differs from the original image qualification source above. Updating transport does not requalify a final runner image.

Transport selection and its hash come from one immutable copy of the file bytes. Both architecture acquisitions in a publication use that same copy. A changed transport file aborts before a successful receipt, image import or publication checksum commit. Consumer receipts and the reviewed runner manifest bind the transport identity separately from the original image lock.

The explicit `actions` mode retains the original run/artifact metadata and expiry checks for historical workflows and tests. It is never selected automatically. Unsafe ZIP entries, wrong provenance and changed bytes fail in either mode.

The original Docker export is loaded only after the shared OCI/Docker verifiers pass. The loaded image must have the accepted config and rootfs. Buildx uses that config digest. The embedded import tag must be absent beforehand; cleanup refuses to remove it if it now refers to another image.

The docker-container driver, 2 CPU/4 GiB/no-swap limits, real cgroup/RUN proof, owned container/volume cleanup, full scans and invalid site CA remain. The distroless image uses its qualified `/usr/bin/oci-cgroup-reader`, instead of assuming it contains `cat`.

`GH_TOKEN` is available to the bounded downloader. Docker, Buildx and build processes continue to receive the sanitized environment. The workflow has only contents/actions read permissions; no registry writes, runner activation or cluster credentials. Archives stay on the disposable worker; local review needs only evidence.

## Final runner runc

The original verified OCI layout is an explicit `accepted-tools` build context. The Dockerfile copies `/usr/bin/buildkit-runc` to `/usr/local/bin/runc` with its original licenses. No host rootfs extraction or executable rebuild is involved. The accepted amd64 SHA-256 is:

`266ee9ba59f57fd4285d64f2542641bf70f61aacf88b68dc42ab92ff2767ef46`

The final gate requires that exact regular executable and all 27 expected Go package/version records, including Go 1.27.3 and x/net 0.62.0. Measured file size and full file evidence are retained from the final OCI filesystem. Missing, duplicate, partial or older runc inventory fails even with zero findings. New HIGH/CRITICAL findings still block.

The old runc exclusions for CVE-2026-46600 and CVE-2026-78669 are removed from active policy. Historical applicability/reassessment JSON remains unchanged as evidence for the previous binary; it grants no exclusion to the replacement. Other components' exact scoped policies are unchanged.

## Execution and troubleshooting

Review and merge the consumer source first. Dispatch the existing `runner-image-artifacts.yml` on main with `reviewed_commit` equal to that commit. Only attempt 1 is admitted; another execution requires a fresh reviewed dispatch. Import success is not final-image qualification: both full image scans, input and cleanup receipts, and the final gate must pass. Cold CI qualification remains separate.

The original Actions image artifacts expire November 11, 2026 UTC; evidence expires November 18. Those deadlines apply to explicit Actions acquisition. The retained release route uses independently pinned stable assets and keeps the original qualification metadata as historical evidence. If its assets are unavailable or no longer match, stop and review a replacement transport; never silently rebuild, change stores or resolve a moving tag. Durable publication uses the existing release adapter and RC/stable process with explicit architecture mapping and unchanged image configs/layers.

Direct Dockerfile builds now require the same verified `accepted-tools` OCI context. Do not substitute an arbitrary registry image. The supported artifact workflow prepares that context automatically.

## Review evidence without downloading images

Each stage also uploads `runner-image-evidence-<stage>-<run>-<attempt>` for 14 days. The existing complete image artifact remains unchanged. A fixed allowlist copies the original build/OCI receipts, complete scan and acceptance reports, file evidence, database metadata, scan input, cgroup proof and complete disk/cgroup observation logs. Its inventory binds every copied file by size and SHA-256. No image, Docker archive or layer is included.

The raw scan retains its existing 128 MiB limit; other proof files are limited to 8 MiB each, with a 192 MiB total payload cap. Only `build.log` can be truncated: its final 4 MiB are marked with original size, byte offset and completeness. A successful build must provide every required file. After failure, available diagnostics are retained with an explicit missing-file list; these are not successful qualification evidence. Special files, changed inputs and exceeded limits fail staging and remove only the new staging directory.

Inspect the compressed artifact size before a serialized local download, then enforce these producer-specific expanded limits. These bounds do not widen the smaller native-builder evidence collector. Verify the inventory and all original acceptance bindings before accepting a final image; logs or a successful upload alone are insufficient.
