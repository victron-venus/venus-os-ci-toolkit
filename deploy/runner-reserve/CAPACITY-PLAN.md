# Public worker capacity plans

This is an **offline rendering gate**. It does not qualify the public runtime,
install pools, change GitHub routing, or grant permission to retry the previously
rejected runtime qualification. Keep public projects on GitHub runners until the
runtime hold is resolved separately. `--held` continues to render zero-worker
pools without a capacity plan and preserves its existing output.

An active render now requires `--capacity-plan PATH`. The plan allocates every
scope/profile explicitly as zero or one; omitted pools are errors, not implicit
permission. The renderer binds these allocations to each pool's `maxRunners`
and to the aggregate ResourceQuota in each of the three worker namespaces.
The sum of namespace pod quotas equals the sum of per-pool allocations. This
prevents six independent scale sets from each treating the same free space as
its own budget. Apply the complete matching foundations and pool set as one
reviewed configuration; stale or additional deployments/quotas must not be mixed
with a new plan. Rendering does not prove that a live cluster matches the plan.

The JSON object has exactly these fields:

- `schema`: integer `1`.
- `node`: the worker node supplied to `--node` (normally `mp`).
- `captured_at`: UTC ISO timestamp ending in `Z`, at most 300 seconds old and
  not in the future when rendering.
- `host_available_bytes`: free bytes on the **physical macOS filesystem holding
  the VDI**, measured at that timestamp. Guest `df` space is not a substitute.
- `host_reserve_bytes`: untouched headroom, at least 2 GiB to cover the current
  VM pause threshold. **8 GiB is the recommended planning floor**, not a change
  to the installed 2 GiB guard. Operators must justify any smaller reserve.
- `image_growth_bytes`: additional physical space budget for image pulls,
  unpacking and shared image caches; estimate conservatively.
- `other_growth_bytes`: budget for other workloads, logs, and host writes during
  the intended operating window; estimate conservatively.
- `overshoot_bytes`: positive extra budget for polling, RPC/termination delay,
  buffered writes, and delayed kubelet accounting/eviction.
- `allocations`: a complete map of every inventory scope to all three profiles
  (`ci`, `automation`, `release`), with integer values `0` or `1`.

All byte values must be nonnegative integers. JSON booleans, duplicate keys,
unknown fields, symlink inputs, missing scope/profile entries, and over-budget
plans are rejected before an output directory is created.

The admitted planning inequality is:

```
20 GiB * sum(all pool allocations)
  + host_reserve_bytes + image_growth_bytes
  + other_growth_bytes + overshoot_bytes
  <= host_available_bytes
```

Each allocated worker keeps the existing 20 GiB ephemeral-storage limit.
Namespace quotas include the RuntimeClass overhead of 100m CPU and 128 MiB
per worker. A zero allocation for a profile produces a zero pod quota there.
The namespace controller/listeners live separately; confirm this topology and
absence of unrelated pods before any future application.

For illustration only, a **61.5 GiB** measurement with 8 GiB reserve, 5 GiB
image budget, 4 GiB other-write budget and 4 GiB overshoot can budget two workers
(61 GiB total), but not three. This is not a live capacity proof, and those budget
estimates are not measured guarantees. The older rounded 65 GiB value is not
current evidence. Construct the full allocation map from the selected inventory,
fill in a fresh host measurement and operator-approved budgets, then render.

Kubernetes ephemeral-storage limits and quotas are **not hard physical disk
limits**. VDI allocation and concurrent host writes can consume space before
kubelet eviction. A one-time fresh plan is also not continuous admission control.
Before activation, separately verify live enforcement, a host-level low-space
stop/admission policy with adequate margin, image growth, allocation-to-cluster
reconciliation and sustained load behavior. None of these runtime checks or
configuration changes is performed by this source change. Do not lower the
installed guard or treat offline acceptance as public-runtime qualification.
