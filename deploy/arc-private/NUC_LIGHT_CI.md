# Two light CI workers on NUC

This is a render-only opt-in. It permits at most two light CI workers across
repositories, while all other NUC workers remain exclusive. Rendering and
offline tests do not qualify the runtime or an application's performance.
Existing plans keep their exact output when this option is omitted or empty.

Add `light_ci_scopes` to the complete private NUC plan described in
[NUC_KVM_PROFILES.md](NUC_KVM_PROFILES.md). For example:

```json
{
  "image": "localhost/private-worker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "node_ip": "192.0.2.2",
  "targets": [
    {"scope": "example-owner/small-tool", "profile": "ci"},
    {"scope": "example-owner/application", "profile": "ci"},
    {"scope": "example-owner/application", "profile": "automation"}
  ],
  "light_ci_scopes": ["example-owner/small-tool"]
}
```

Use the existing `--nuc-kvm-plan` render command and actual reviewed image and
guard address. Each light scope must already be selected as CI, have the
standard resource class, and use `ci_storage: tmpfs-2gi`. Heavy CI cannot opt in.
Automation and release retain their existing resources and exclusive scheduling,
including those from a repository whose CI is light.

The option changes the repository's **whole existing CI pool**. It does not
classify jobs, create a second route, change workflow labels or make individual
steps light. Inspect every workflow that can select that pool before opting in.
A repository needing both heavy and light CI routes requires a separately
reviewed routing change. Do not lower a heavy repository's resource class merely
to pass this validation.

## Fixed resources and the global bound

Each light worker requests 200m CPU and 3 GiB memory, with limits of 1 CPU and
6 GiB. Its existing 2 GiB tmpfs and disposable storage contract stay unchanged.
The RuntimeClass still adds 100m CPU and 128 MiB memory per worker. A selected
light scale set renders `maxRunners: 2`; `minRunners` stays unchanged.

Two workers therefore charge the namespace 600m CPU requests, 2200m CPU limits,
6400 MiB memory requests, 12544 MiB memory limits, 2 GiB ephemeral-storage
requests and 40 GiB ephemeral-storage limits. Node capacity must additionally
cover services, listeners and all other workloads. For example, 3500m
allocatable CPU minus 2790m existing requests leaves 710m; two light workers
leave 110m. This is a scheduling calculation, not evidence that a job meets its
deadline or that running services keep acceptable latency.

The cap relies on three controls working together:

1. The exact admission tuple permits light workers only for selected CI scopes
   in `arc-private-ci`. It binds the light label, namespace, resources and
   bootstrap environment. A heavy, automation or release worker cannot borrow
   that class. Identity updates, including `pods/status`, cannot add, remove or
   change the concurrency label. The existing default-scheduler and direct
   binding restrictions remain in place.
2. The unchanged, unscoped `reserve-budget` ResourceQuota in that namespace has
   `pods: "2"`. It caps non-terminal Pods across every repository's scale set,
   not two per repository. Pending workers also consume quota. Independent
   namespaces have no authority to admit a light worker. Do not replace this
   quota with a per-scope quota or raise the Pod count.
3. All NUC workers retain `serial-domain: nuc-kvm`. Light workers exclude every
   peer whose concurrency label is not `light`, using a global namespace
   selector and hostname topology. Kubernetes `NotIn` includes an absent
   label, so existing exclusive workers remain excluded. Exclusive workers
   continue to exclude **all** NUC workers. These rules prevent a heavy,
   automation or release worker from sharing with a light worker in either
   scheduling order and across namespaces.

The quota and scheduler do not prove orphaned sandboxes have stopped. Never
force-delete a worker or fabricate terminal status to free capacity. Verify
normal Pod, registration, JIT Secret, sandbox and scratch cleanup. Queued heavy
workers can consume the CI quota and prevent two light workers from being
admitted; this change does not promise queue fairness or job preemption.

## Controlled activation and rollback

Use one operator for the live change. Keep existing heavy pools and unrelated
services intact; coordinate with any ongoing ARC maintenance before proceeding.

1. Snapshot the full current plan, scale-set templates, admission policies and
   bindings, RuntimeClass, quotas and LimitRanges. Confirm that every selected
   light pool is appropriate from its authored workflows. Re-render the entire
   current plan; review the exact diff rather than applying a partial allowlist.
2. Stop new assignments to affected NUC pools and let active workers finish.
   Verify zero old active workers and no orphaned sandboxes. Preserve their
   previous activation settings for rollback.
3. Read fresh node allocatable resources, all Pod requests and workload health.
   Read every overlapping quota's `spec.hard`, `status.hard`, `status.used`,
   scopes and LimitRanges. Require the unscoped CI `pods: "2"` budget, converged
   quota status and enough capacity in **every** dimension for two light
   workers. The example headroom above must not substitute for this check.
4. Apply only the reviewed policy and exact selected templates with live
   UID/resourceVersion preconditions. This feature needs no quota, RuntimeClass,
   image, firewall or service-resource change. Do not apply the whole
   foundations bundle. Keep assignment admission closed until verification
   finishes; ARC listener scaling fields legitimately track the selected
   `maxRunners`, so stale fixed-value checks must not reopen or close other pools.
5. Require the actual API server to accept and typecheck the complete policy.
   Test positive pending and bound status updates and scheduler bindings.
   Negative cases must cover label/class changes, namespace and resource
   substitution, weakened selectors, preassigned nodes, alternate schedulers
   and both direct binding endpoints. An unrelated rejection is not proof.
6. With controlled synthetic workers, prove two light Pods run concurrently and
   a third is rejected by the quota. Record both arrival orders for light versus
   exclusive workers across namespaces, with the expected Pending reason and
   verified runtime cleanup between cases. Test two workers from the same pool
   as well as two different pools. Respect the CI quota when choosing fixtures;
   quota rejection alone does not prove cross-namespace anti-affinity.
7. Qualify an actual lightweight job under the new 200m/1 CPU contract while a
   second light worker and ordinary services run. Check cold startup/downloads,
   nested Docker when used, application deadlines, memory, CPU pressure, service
   latency and cleanup. Restore assignments only for the verified selected
   pools; queued jobs may claim them immediately.

If verification fails, close new assignments, drain exact workers through normal
cleanup, and restore the reviewed previous full policy and selected templates
with preconditions. Remove the selected entries from `light_ci_scopes` and
return those pools to their former `maxRunners`; an empty list reproduces the
exclusive default. Never relax isolation, move jobs to public runners, or shrink
service reservations merely to make the capacity calculation pass.
