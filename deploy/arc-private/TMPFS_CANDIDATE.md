# Private tmpfs candidates

These opt-in candidates put Docker's data in a bounded, in-guest tmpfs. They use
the existing `victron-reserve` handler, without changing its configuration or
restarting the node. The standard CI candidate and the new heavy CI/automation candidates require
separate runtime and application CI qualification; no new mode is qualified by
this source change. Do not enable a fleet from a successful render or unit test.

The proposed standard CI CPU request is 1900m rather than 900m. This is an
unqualified scheduling candidate, not a change to image contents or the CPU
quota. With runtime overhead it permits one standard CI worker at a time;
heavy CI, automation, release and native resource budgets are unchanged.

The pinned [gVisor Docker guide](https://github.com/google/gvisor/blob/release-20260928.0/g3doc/user_guide/tutorials/docker-in-gvisor.md#docker-storage-backend)
recommends tmpfs for Docker storage. Its
[reference bootstrap](https://github.com/google/gvisor/blob/release-20260928.0/images/basic/docker/start-dockerd.sh#L34-L45)
mounts tmpfs before starting Docker. Here the private image retains the shared
daemon implementation and adds a storage gate; the public image and bootstrap
are unchanged. Classic disk storage remains the default.

## Proposed containerd image-store backend

This draft changes only the three explicit private tmpfs modes below. It has not
been selected for deployment or proven by a runtime A/B. Source-level inspection
shows that containerd pulls avoid Moby's classic tar-split pipe and chroot
unpacking path; this is a testable performance hypothesis, not a speed guarantee.
The [pinned gVisor Docker guide](https://github.com/google/gvisor/blob/release-20260928.0/g3doc/user_guide/tutorials/docker-in-gvisor.md)
supports the image store when `/var/lib/docker` is backed by guest tmpfs.

The wrapper resolves both external daemon executables to absolute paths before
shadowing their launch functions or starting either process.
It rewrites exactly one `--feature containerd-snapshotter=false` pair to `true`,
requires exactly one `--storage-driver=overlayfs`, and rejects missing,
duplicated or alternate forms of these target arguments. Other launch arguments
are preserved; `exec` retains daemon PID and cleanup semantics. The public helper,
its standalone containerd configuration, CRI-disabled setting and local sockets
remain unchanged. No GOMAXPROCS or host runtime setting changes.

Both background launch functions set a child-only `umask 022` before `exec`.
The calling bootstrap retains `077` for its private files. This matters for the
standalone containerd process: its
[overlay snapshotter creates new filesystem roots with mode 0755](https://github.com/containerd/containerd/blob/v2.3.6/plugins/snapshots/overlay/overlay.go),
which an inherited `077` mask reduces to `0700`, blocking nonroot image users
from traversing `/`. The fix sets creation semantics before any snapshots exist;
it does not chmod existing images or change their declared file modes. Docker
29.8.2 [resets its own mask to zero during initialization](https://github.com/moby/moby/blob/8af9fe3a36bab3e039862a2ab1cef1880c9b4d03/daemon/command/daemon_unix.go#L47-L57),
so the wrapper's `022` is a launch contract, not a claim about Docker's eventual
runtime mask. Containerd's deliberately private state directories remain private.
Tests verify child masks, ordinary creation modes, argument preservation and PID
cleanup; the candidate still requires the unchanged nonroot PostgreSQL check.

The renderer pairs each named mode with `RUNNER_DOCKER_STORAGE_DRIVER=overlayfs`;
admission requires its exact namespace/profile, memory and CPU tuple, one literal
backend selector, and one valid mode selector. An absent mode retains `overlay2`.
The canonical qualifier stays byte-identical: its backend assertion follows the
selected driver, and BusyBox build/whiteout/run and PostgreSQL network/SQL checks
remain mandatory. A candidate also needs independent `DriverStatus` evidence
showing `driver-type=io.containerd.snapshotter.v1`.

Containerd retains compressed content as well as unpacked snapshots, both under
`/var/lib/docker/containerd/daemon` in this configuration. Existing static image
sizes or classic-driver peak measurements do not qualify the new backend's
capacity. Measure real occupancy and cgroup peak memory separately for the
2 GiB, 6 GiB and 3 GiB modes before enabling their pools. The numerical caps and
quotas below are unchanged; none is a claim that the workload fits.

## Select one standard CI candidate

In the protected operator inventory, explicitly set `ci_storage` for the
repository being qualified:

```json
{
  "name": "example/private-project",
  "visibility": "private",
  "profiles": ["ci", "automation"],
  "ci_storage": "tmpfs-2gi"
}
```

Build the private image from `deploy/arc-private/Dockerfile` as described in
[the operator guide](../../docs/PRIVATE_EPHEMERAL_RUNNERS.md), retain its exact
digest, and render with `--sandbox-runtime victron-reserve` (the default).
Rendering does not deploy, activate a pool, or change GitHub routing. The
candidate image must undergo isolated qualification before its digest is
admitted or used by any runner pool.

Only this repository's standard `ci` profile changes:

- Docker data: 2 GiB tmpfs, at most 262,144 inodes, mounted inside the sandbox
  over the existing fresh 16 GiB disk emptyDir. The disk volume declaration
  and its mount restrictions remain intact.
- Memory: 3 GiB request, 6 GiB limit. Candidate CPU: 1900m request / 4 CPU limit.
  RuntimeClass overhead adds 128 MiB and 100m for scheduling and quota.
- Explicit private tmpfs modes select `overlayfs` with the containerd image store.
  Disk/default modes retain classic `overlay2` with the snapshotter disabled.
  Network fencing, daemon readiness/cleanup and the nonroot job are unchanged.

The renderer rejects unknown storage modes, a missing matching profile, heavy
CI with the standard 2 GiB candidate, and tmpfs with the exclusive runtime.
Selecting `ci_storage` never changes automation, release, LAN or ARM. The new
automation mode has its own explicit selector and budget, described below.
Release and native profiles have no tmpfs mode.

Remove `ci_storage` or set it to `disk` to render the classic disk option again.
Do not interpret that as a claim that disk latency meets an application's
deadline. Rollback a deployed candidate only through the reviewed pool update
process; this document runs no deployment commands.

## Optional heavy CI candidate: 6 GiB

A protected private inventory row may explicitly select:

```json
{
  "name": "example/private-application",
  "visibility": "private",
  "profiles": ["ci", "automation"],
  "ci_resource_class": "heavy",
  "ci_storage": "tmpfs-6gi-heavy"
}
```

This selects `RUNNER_PRIVATE_DOCKER_STORAGE=ci-heavy-tmpfs-6gi`: exactly 6 GiB
of guest tmpfs, 524,288 inodes, a 13 GiB memory request and 16 GiB limit. The
existing heavy CPU request/limit remains 1500m/3 CPU. Both renderer and admission
require the CI namespace/profile and the heavy resource tuple. The bootstrap
also checks the exact memory request and limit, unlike the original standard
CI bootstrap range. This does not automatically promote any inventory row.

With overhead, one worker requests 13.125 GiB and 1.6 CPU, with a 16.125 GiB
memory limit. It fits the existing CI quota of 14 GiB requests / 17 GiB limits /
2 CPU requests alone. A standard tmpfs worker cannot coexist with it. The 6 GiB
scratch is a planning allowance from the measured image below, including an
additional 1 GiB beyond a 5 GiB sizing model; it is not a runtime fit result.

## Optional automation candidate: 3 GiB

Set `"automation_storage": "tmpfs-3gi"` only on an explicitly private row
whose `profiles` contains `automation`. It selects
`RUNNER_PRIVATE_DOCKER_STORAGE=automation-tmpfs-3gi`: exactly 3 GiB of guest
tmpfs, 262,144 inodes, a 5 GiB memory request and 6 GiB limit. CPU remains the
existing 50m request / 1 CPU limit. CI and other profiles on the row do not
change. The namespace/profile and resource tuple are checked by admission;
the bootstrap checks the corresponding namespace/profile and exact memory.

Only when at least one row explicitly selects this mode, the rendered
automation namespace quota changes `requests.memory` from 3 GiB to 6 GiB.
Its `limits.memory` remains 9 GiB; all other quota fields stay unchanged.
Including 128 MiB overhead per worker:

- One automation candidate requests 5.125 GiB with a 6.125 GiB limit: it fits.
- One candidate plus one ordinary automation worker requests 6.25 GiB with
  a 10.25 GiB limit: both quotas reject the combination.
- Two ordinary automation workers request 2.25 GiB with an 8.25 GiB limit:
  the existing two-worker capacity remains possible.

The new quota is a source proposal until reviewed and applied through the
normal operator procedure. No render installs it. A 3 GiB scratch plus a
workflow's 2 GiB child-container limit leaves at most 1 GiB within the 6 GiB
outer limit for the runner, daemons and other allocations. That allowance must
be measured; it is not a guarantee. The old 4 GiB automation limit must never
silently inherit this storage mode. Removing the selector restores the old
rendered quota; do not reduce a live quota or change pools without reviewing
running work and the rollback procedure.

## Measured image inputs, not runtime memory

Public Linux/amd64 image layers were downloaded one at a time and streamed
through decompression without extraction or execution. Each compressed layer
SHA-256 and its uncompressed OCI `rootfs.diff_ids` value was verified. These
are image inputs for sizing; they are not measurements of a running Docker
filesystem, working set, or cgroup peak.

- Home Assistant 2026.9.1 platform manifest:
  `sha256:31076d37e3b7dc9681b32d892aa4413fa866c9a90e5c8a50324b397dce415d17`.
  Its 22 zstd layers total 643,970,254 compressed bytes and 2,461,873,152 tar
  bytes. The largest tar layer is 1,453,772,800 bytes. There are 171,931 layer
  entries, including 153,790 regular files; no whiteouts or sparse entries.
  Regular file payload rounded to 4 KiB blocks totals 2,715,979,776 bytes,
  already exceeding a 2 GiB scratch before Docker metadata or writable data.
- Renovate 44.150.1 platform manifest:
  `sha256:3fd11b748de7f91b6de8a2d2b99b38f02244840084d679550ad85085fc46a267`.
  Its 13 gzip layers total 437,384,819 compressed bytes and 1,350,327,296 tar
  bytes. The largest tar layer is 563,703,808 bytes. There are 67,780 layer
  entries, including 52,797 regular files, and two removal whiteouts.
  Regular file payload rounded to 4 KiB blocks totals 1,447,792,640 bytes.

A conservative planning model adds the rounded file payload, all compressed
bytes, and one largest tar layer as a temporary-work allowance. A 6 GiB HA
scratch leaves 1,628,728,114 bytes beyond that model; a 3 GiB Renovate scratch
leaves 772,344,205 bytes. The extra space is for metadata and writes. The model
is not a proven upper bound: concurrent buffers, copy-up, caches, writable
volumes and memory for inode metadata still require actual measurement.
The inode caps also include headroom above layer-entry counts, which are not
exact installed inode counts because of hardlinks, repeated directories and
whiteouts. Lower layers can remain stored even when hidden by a whiteout;
the model does not substitute only the final visible filesystem size.

See the OCI [layer format](https://github.com/opencontainers/image-spec/blob/v1.1.1/layer.md)
and [DiffID definition](https://github.com/opencontainers/image-spec/blob/v1.1.1/config.md#layer-diffid).

## Startup and memory accounting

For the standard candidate, the renderer sets
`RUNNER_PRIVATE_DOCKER_STORAGE=ci-tmpfs-2gi`. Namespace,
profile and memory request/limit come from the downward API. The private
bootstrap requires guest root in gVisor, `arc-private-ci`, profile `ci`, exactly
6 GiB limit and a request between 3 and 6 GiB. Admission restricts the rendered
candidate to a 3 GiB request and the shared runtime. An absent storage selector
uses the original disk helper; an unknown selector fails.

The mount gate requires an empty, real directory on the existing 9p mount.
It never hides existing Docker data or falls back after failure. It verifies
tmpfs type, the selected exact capacity and inode cap before launching either
daemon. A failed mount or readback terminates startup.

Tmpfs consumes worker memory; the 16 GiB disk volume limit does not give it
extra capacity. A 2 GiB filesystem leaves at most 4 GiB of the worker's 6 GiB
limit for other data, daemon/job processes and filesystem metadata. The
filesystem size is not a reservation, and guest root can change guest mount
options; the outer cgroup limit remains the containment boundary.

Two standard candidates would request 6.25 GiB including RuntimeClass overhead,
within the CI namespace's existing 14 GiB memory request quota. Their combined
4-CPU request exceeds its unchanged 2-CPU request quota, so only one can run
at a time. Only explicit automation selection changes the quota described
above. The scheduler still needs available node capacity; inspect current
memory and workloads before qualification. Image downloads, unpacked layers,
build cache and database volumes all compete for this tmpfs. Fixture size
alone does not establish that a full application's CI fits.

## Qualification required before activation

Retain the original 1,800-second canonical probe and all its assertions:
nested build/run, deletion/opaque directories, PostgreSQL SQL and published
port. Also prove the 180-second cold create / 30-second run smoke contract.
Qualify each new mode separately, including the original application tests
that use the measured image. Record the actual mount, filesystem capacity
and inode use, cgroup memory peak/OOM events,
image/source digests, and cleanup. Repeat the relevant functional and network
checks for the candidate image. A passing synthetic fixture does not replace
current application CI, and does not qualify the other profiles.
