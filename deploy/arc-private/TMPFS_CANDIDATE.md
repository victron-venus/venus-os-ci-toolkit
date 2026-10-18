# Private CI tmpfs candidate

This opt-in candidate puts Docker's data in a bounded, in-guest tmpfs. It uses
the existing `victron-reserve` handler, without changing its configuration or
restarting the node. It has not yet passed runtime or application CI
qualification. Do not enable a fleet from a successful render or unit test.

The pinned [gVisor Docker guide](https://github.com/google/gvisor/blob/release-20260928.0/g3doc/user_guide/tutorials/docker-in-gvisor.md#docker-storage-backend)
recommends tmpfs for Docker storage. Its
[reference bootstrap](https://github.com/google/gvisor/blob/release-20260928.0/images/basic/docker/start-dockerd.sh#L34-L45)
mounts tmpfs before starting Docker. Here the private image retains the shared
daemon implementation and adds a storage gate; the public image and bootstrap
are unchanged. Classic disk storage remains the default.

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
- Memory: 3 GiB request, 6 GiB limit. CPU remains 900m request / 4 CPU limit.
  RuntimeClass overhead adds 128 MiB and 100m for scheduling and quota.
- Docker remains classic `overlay2` with the containerd snapshotter disabled.
  Network fencing, daemon readiness/cleanup and the nonroot job are unchanged.

The renderer rejects unknown storage modes, tmpfs without CI, heavy CI with
this candidate, and tmpfs with the exclusive runtime. Automation, release,
LAN and ARM keep their existing storage and resources. Lower automation/release
memory limits cannot inherit the CI setting; any future modes need explicit
profile budgets and their own qualification. Heavy CI can support a different
budget within its 16 GiB limit, but that mode is not implemented here.

Remove `ci_storage` or set it to `disk` to render the classic disk option again.
Do not interpret that as a claim that disk latency meets an application's
deadline. Rollback a deployed candidate only through the reviewed pool update
process; this document runs no deployment commands.

## Startup and memory accounting

The renderer sets `RUNNER_PRIVATE_DOCKER_STORAGE=ci-tmpfs-2gi`. Namespace,
profile and memory request/limit come from the downward API. The private
bootstrap requires guest root in gVisor, `arc-private-ci`, profile `ci`, exactly
6 GiB limit and a request between 3 and 6 GiB. Admission restricts the rendered
candidate to a 3 GiB request and the shared runtime. An absent storage selector
uses the original disk helper; an unknown selector fails.

The mount gate requires an empty, real directory on the existing 9p mount.
It never hides existing Docker data or falls back after failure. It verifies
tmpfs type, exactly 2 GiB capacity and the inode cap before launching either
daemon. A failed mount or readback terminates startup.

Tmpfs consumes worker memory; the 16 GiB disk volume limit does not give it
extra capacity. A 2 GiB filesystem leaves at most 4 GiB of the worker's 6 GiB
limit for other data, daemon/job processes and filesystem metadata. The
filesystem size is not a reservation, and guest root can change guest mount
options; the outer cgroup limit remains the containment boundary.

Two standard candidates request 6.25 GiB including RuntimeClass overhead,
within the CI namespace's existing 14 GiB request quota. Quotas and limits are
unchanged. The scheduler still needs available node capacity; inspect current
memory and workloads before qualification. Image downloads, unpacked layers,
build cache and database volumes all compete for this tmpfs. Fixture size
alone does not establish that a full application's CI fits.

## Qualification required before activation

Retain the original 1,800-second canonical probe and all its assertions:
nested build/run, deletion/opaque directories, PostgreSQL SQL and published
port. Also prove the 180-second cold create / 30-second run smoke contract.
Record the actual mount, filesystem capacity, cgroup memory peak/OOM events,
image/source digests, and cleanup. Repeat the relevant functional and network
checks for the candidate image. A passing synthetic fixture does not replace
current application CI, and does not qualify the other profiles.
