# Bounded heavy CI v1 — candidate, not workload qualification

The optional NUC selection below keeps a 6 GiB Docker tmpfs and reduces the
worker memory request/limit from 13/16 GiB to **9/12 GiB**. It is intended for
measured, sequential container tests whose image does not fit standard CI's
2 GiB tmpfs. The memory ceiling is a candidate sizing decision, not a claim
that any application already passed in it.

Existing plans and all unselected profiles retain their exact rendered bytes.
This selection does not change CPU, disk limits, runtime, guard address,
networking, cross-namespace serialization, credentials or the listener. It
cannot be combined with light concurrent CI. The existing low-share CPU
selection may be used independently.

## Explicit selection

Add both fields to the complete NUC plan passed through `--nuc-kvm-plan`:

```json
{
  "targets": [
    {"scope": "example/runtime-tests", "profile": "ci"},
    {"scope": "example/other", "profile": "ci"}
  ],
  "image": "registry.example/private-worker@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "node_ip": "192.0.2.2",
  "bounded_heavy_ci_scopes": ["example/runtime-tests"],
  "bounded_heavy_image": "registry.example/private-worker@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
}
```

The selected inventory row must already be private, declare `ci`, and use
`ci_resource_class: heavy` with `ci_storage: tmpfs-6gi-heavy`. Missing, empty,
duplicate or partial selections are rejected. The derivative image must have
a distinct immutable reference. The generated admission policy binds each
image to the exact selected namespace, profile and scope, while its resource
and environment rule binds 9/12 GiB to
`ci-bounded-heavy-v1-tmpfs-6gi`. Swapping an image, mode or memory tuple between
profiles is rejected. An image allowlist alone is insufficient.

## Image preparation

The existing private image rejects the new tuple. Do not change live values
until a derivative has been built and reviewed. Build from the exact accepted
private-image digest and copy only the reviewed `sandbox-docker.sh` to
`/opt/victron-ci-reserve/sandbox-docker.sh`, owned by root with mode 0755.
Retain the existing guard CA, runtime configuration, labels and other layers.
No credentials or new secret material belong in the build context. Record
the wrapper/source hashes, base and resulting image digests, archive checksum,
platform, full configuration comparison and the single expected layer change.
Source provenance for this layer does not imply a rebuild of the base.

The versioned wrapper checks the exact namespace, CI profile, 9/12 GiB
resource tuple, fresh 9p mount, 6 GiB visible tmpfs capacity and 524288 inodes.
An old image or wrong memory tuple fails before Docker starts. Do not weaken
the bootstrap, rewrite its environment or bypass admission to make it start.

## Acceptance and rollback

1. Preserve complete current values, image availability and policy before any
   reviewed operation. Hold only owned pools and let assigned jobs finish.
2. Check scheduler requests **and actual available resources**. Budget the full
   12 GiB limit, 128 MiB runtime overhead, at least 2 GiB operational reserve and
   any absent service reservation. Keep the existing disk/eviction margin.
   The smaller worker ceiling does not lower these safety reserves.
3. Run real server-side positive and negative admission probes, including wrong
   image, scope, profile, mode and memory bounds. Offline expression tests are
   not a Kubernetes CEL typecheck or an admission receipt.
4. Observe an actual cold job at the exact required source/run/attempt. Bind
   imageID, runtime, active tmpfs/backend, resource limits, cgroup memory peak
   and OOM deltas, tmpfs occupancy and all required job results. Preserve a
   failure; an OOM or full filesystem does not authorize a larger retry.
5. Verify normal Pod, runner registration and JIT cleanup. Only then record
   acceptance for this selected image/profile/workload. Other profiles do not
   inherit the result.

On failure, stop assigning new jobs, retain evidence, drain normally and restore
only changes whose ownership and exact previous values were recorded. If the
old 16 GiB profile does not fit current capacity, keep that target held instead
of reactivating it as a rollback side effect. Never overwrite an image tag or
expand the new image to all NUC scopes to avoid a scoped policy update.
