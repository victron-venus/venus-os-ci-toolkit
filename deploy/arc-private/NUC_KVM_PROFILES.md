# Multiple private profiles on one NUC

This is a **render-only, unqualified candidate**. It extends the single-repository
[NUC KVM candidate](NUC_KVM_CANDIDATE.md) to an explicit set of CI and automation
profiles. Rendering does not install a runtime, transfer an image, start a
runner, or establish that an application works on this machine.

Use this option when a small dedicated node has enough memory for one large job
but cannot run several private jobs concurrently. Every selected worker keeps
its repository-specific route and disposable sandbox. Kubernetes schedules at
most one of these workers on the node at a time, including workers from different
profile namespaces. Listeners remain independent and lightweight.

## Describe the complete target set

Keep the plan in private operator storage. It contains repository identities;
the public examples below are synthetic. Each target must already exist as a
private repository/profile in the ordinary inventory.

```json
{
  "image": "localhost/private-worker@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "node_ip": "192.0.2.2",
  "targets": [
    {"scope": "example-owner/application", "profile": "ci"},
    {"scope": "example-owner/home", "profile": "ci"},
    {"scope": "example-owner/home", "profile": "automation"}
  ]
}
```

Replace both the example digest and address with verified operator values. The
image must trust the actual node guard CA, and must already be available to
containerd on that node: the worker retains `imagePullPolicy: Never`. Never put a
server private key or GitHub credential in this plan or image.

```sh
python3 deploy/arc-private/render.py \
  --inventory "$PRIVATE_INVENTORY" \
  --sandbox-image "$EXISTING_SANDBOX_IMAGE" \
  --native-image "$EXISTING_NATIVE_IMAGE" \
  --node-ip "$EXISTING_GUARD_IP" \
  --listener-node "$LISTENER_NODE" \
  --nuc-kvm-plan "$PRIVATE_NUC_PLAN" \
  --output "$NEW_PRIVATE_OUTPUT"
```

The plan replaces the entire NUC target set for this render. It is **not an
incremental add command**. The renderer cannot inspect an installed cluster or
detect a target omitted from an older plan. Compare the proposed targets and
full policy diff with the current operator inventory before applying anything.
Do not render one repository at a time and apply each `foundations.json`: the
last invocation would replace the previous shared allowlist.

The original `--nuc-kvm-scope`, `--nuc-kvm-image` and `--nuc-kvm-node-ip` interface
remains available for one standard CI profile. Do not combine it with a plan.
Unselected profiles, repository URLs, GitHub App references and routing labels
retain their existing configuration. Release, LAN and ARM profiles cannot be
selected through this interface.

## Fixed resource contracts

Resources follow the selected profile and its existing inventory storage mode;
the plan does not accept arbitrary CPU, memory or security settings.

- Standard CI requires `ci_storage: tmpfs-2gi` and the standard resource class.
  It requests 1250m CPU and 3 GiB memory, with limits of 2 CPU and 6 GiB memory.
- Heavy CI requires `ci_resource_class: heavy` and
  `ci_storage: tmpfs-6gi-heavy`. It requests 1250m CPU and 13 GiB memory, with
  limits of 2 CPU and 16 GiB memory. Its Docker guest tmpfs remains 6 GiB.
- Automation requires `automation_storage: tmpfs-3gi`. It requests 50m CPU and
  5 GiB memory, with limits of 1 CPU and 6 GiB memory. Its Docker guest tmpfs
  remains 3 GiB.

The RuntimeClass adds 100m CPU and 128 MiB memory per worker. Account for
listeners, the guard, and other workloads separately. A CPU request that fits the
scheduler does not prove sufficient throughput; application deadlines remain
unchanged. Namespace quotas and `maxRunners: 1` alone do not serialize workers
across namespaces.

Before qualification, read all ResourceQuota and LimitRange objects in each
affected namespace, including quota `spec.hard`, `status.hard`, `status.used`,
and any scopes. The admission evaluator uses `status.hard`; wait for it to
converge to the reviewed `spec.hard` before computing admission headroom,
especially after a quota update.
Overlapping quotas all apply; adding another quota does not lift a smaller
existing budget. The rendered sandbox quota is named
`reserve-budget`; verify the actual target name from a fresh live snapshot.
Account for existing requests and limits usage, RuntimeClass overhead, and all
other quota dimensions before assuming that a worker can be admitted. A
`Pending` Pod already consumes quota.

The observed automation `requests.memory` quota of 3 GiB cannot admit a worker
requesting 5 GiB plus 128 MiB overhead (5248 MiB). Its memory limit also consumes
6 GiB plus 128 MiB (6272 MiB) of the namespace limit budget. The renderer emits a
6 GiB automation request quota only when the inventory selects
`automation_storage: tmpfs-3gi`; rendering does not update the live quota.

Treat the necessary quota change as a separate reviewed JSON Patch. From the
fresh snapshot, include `test` operations for `/metadata/uid`,
`/metadata/resourceVersion`, and the complete old `/spec/hard` object, followed
by a single `replace` of `/spec/hard/requests.memory` from `3Gi` to `6Gi`.
Preserve every other budget. If any precondition fails, take a new snapshot and
review it again; do not discard the tests. Never apply all of `foundations.json`
to make this one-field change. Admission still requires sufficient unused
requests and limits capacity after the change.

## Scheduling and isolation

Selected workers share `private-runners.github.io/serial-domain: nuc-kvm` and a
required Pod anti-affinity rule. The rule uses an empty `namespaceSelector` to
cover all namespaces and the `kubernetes.io/hostname` topology key. The admission
contract requires this exact rule, its label, and the default scheduler.

Quota admission happens before scheduling. With the 2 CPU request quota in the
CI namespace, two CI workers would request `2 * (1250m + 100m) = 2700m`. A
second same-namespace CI worker therefore receives an API quota rejection and
must wait for a later creation retry; there is no second Pod to remain Pending.
An admitted second worker in another selected namespace remains Pending under
the required anti-affinity until the first leaves the node. Neither path
assigns a new execution node or a broader runner label.

The global NUC admission policy binds each allowed repository, namespace,
profile, resource tuple and bootstrap environment together. A heavy worker
cannot borrow an automation identity or another repository's resource tuple.
The original sandbox policy denies selected scope/namespace pairs from falling
back to the original node, even with a substituted profile label. Separate
network policies preserve denied networks and permit only the selected node's
guard endpoint, rather than adding it to the old guard exception.

Pod creation cannot preassign `nodeName` to bypass the scheduler. Bound Pod
updates must preserve the existing node and identity, including updates through
`pods/status`. A separate binding policy covers both `pods/binding` and legacy
`bindings` in the affected private namespaces, allowing only the verified
`system:kube-scheduler` user. This is intentionally namespace-wide because a
Binding admission request does not expose the parent Pod's runtime or labels.
Verify that identity on the target cluster and test existing scheduler-based
workers in those namespaces. This feature is unsuitable for a cluster using a
different scheduler identity until that contract is explicitly reviewed; do not
grant impersonation to work around it.

These controls retain the original restrictions on host mounts, host namespaces,
runtime aliases, extra containers and credentials. They do not protect against
an administrator removing the policies, nor can scheduling prove that an
orphaned sandbox has stopped. Never force-delete an old Pod merely to free a
slot; establish that its runtime and temporary storage are gone first.

## Qualify before activation

1. Review the complete desired state, immutable source/image pins, available
   capacity, guard TLS identity, existing RuntimeClass aliases and namespace
   permissions. Drain old NUC workers that lack the new serialization rule.
2. Complete the quota preflight above for every selected namespace, using fresh
   `spec.hard`, `status.hard` and `status.used` values. If the automation quota still allows
   only 3 GiB of memory requests, stop before creating its worker. Review the
   guarded one-field quota patch separately, preserving other budgets; after
   an authorized update, wait for `status.hard` to match the reviewed `spec.hard`,
   read current usage again, and recompute remaining capacity.
   Do not apply the complete foundations bundle to resolve a quota mismatch.
3. Test generated admission rules on the actual Kubernetes version. Positive
   tests must include legitimate pending and bound Pod status updates and
   scheduler bindings. Negative tests must include cross-profile resource
   substitution, label edits through `/status`, preassigned nodes, alternate
   schedulers, both direct-binding endpoints, and weakened anti-affinity.
   Rejection for a missing RuntimeClass is not proof of the intended rule.
4. Prove the node guard, network isolation, nested Docker, and actual application
   workload under each selected resource contract. Include cold downloads and
   image extraction, not just an already warm BusyBox test. A standard CI result
   does not qualify heavy CI or automation.
5. With controlled synthetic workers, verify both scheduling orders across
   profile namespaces, after checking quota headroom for each: the admitted
   second Pod remains Pending because of anti-affinity, then starts after the
   first runtime and Pod are fully removed. Separately verify that a second
   same-namespace CI creation is quota-rejected while the first remains
   nonterminal and its usage is charged, then succeeds on retry after quota is
   released. Keep the synthetic worker active until the rejection is recorded.
   Distinguish API creation errors
   from scheduler Pending events. Record actual Pod identity, node, image and
   runtime for each admitted worker.
6. Activate only the reviewed scale sets. Existing queued jobs can claim a new
   worker immediately; dispatching a smoke first does not reserve capacity.
   Record run/attempt/head, runner and Pod UID, complete unchanged application
   checks, and verify Pod, registration, JIT Secret, sandbox and scratch cleanup.

Keep activation and rollback under one operator while a node is undergoing
maintenance. On failure, stop new assignments, preserve evidence and drain exact
workers before restoring the previously reviewed full policy state. Do not
route private jobs onto public workers or revive retired static runners.

The offline tests check rendering and input contracts. They do not establish
live CEL acceptance, application performance, fairness between queued jobs or
successful cleanup on a particular node.

References: [Kubernetes scheduling and namespace selectors](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/#namespace-selector),
[Kubernetes 1.31 Pod status strategy](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/registry/core/pod/strategy.go),
and [Pod binding implementation](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/registry/core/pod/storage/storage.go).
