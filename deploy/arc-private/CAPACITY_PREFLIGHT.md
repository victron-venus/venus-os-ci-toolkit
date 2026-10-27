# Private runner resource headroom preflight

`capacity_preflight.py` explains whether **one additional Pod's resource requests
and namespace quota usage** fit a recent snapshot. It performs no Kubernetes
writes, scaling, job cancellation, automatic request reduction, or release.
It does not replace the release-image `scripts/runner_preflight.py`.
Run it on Linux, macOS, or WSL. Its bounded descriptor and pipe handling uses
POSIX APIs; native Windows returns `unable_to_qualify` before those operations.

A worker requesting 600m CPU with 100m RuntimeClass overhead needs 700m of
unallocated requests. It cannot schedule when only 240m remains. Two admitted,
unscheduled Pods can also consume a namespace's entire two-Pod and 40 GiB
storage-limit quota, preventing a smaller worker from being admitted. Pending
Pods consume quota before assignment; they do not yet consume a particular
node's assigned requests.

## Capture and check

Keep the selected scale set disabled until an operator has reviewed the result
and the other scheduling, admission and runtime requirements. Use the complete
rendered values for the proposed worker, not a hand-edited request total.
These files are private operator artifacts: Pod and namespace identities may
be sensitive even though capture discards environment values, commands,
images and annotations.

```sh
umask 077
python3 - "$PRIVATE_VALUES_JSON" "$PRIVATE_NAMESPACE" "$PRIVATE_CANDIDATE" <<'PY'
import json, sys
from pathlib import Path
values = json.loads(Path(sys.argv[1]).read_text())
pod = {"apiVersion": "v1", "kind": "Pod", **values["template"]}
pod.setdefault("metadata", {})["namespace"] = sys.argv[2]
with Path(sys.argv[3]).open("x") as stream:
    json.dump(pod, stream)
PY

python3 deploy/arc-private/capacity_preflight.py capture \
  --context "$PRIVATE_KUBE_CONTEXT" --node "$PRIVATE_NODE" \
  --namespace "$PRIVATE_NAMESPACE" --output "$NEW_PRIVATE_SNAPSHOT"

python3 deploy/arc-private/capacity_preflight.py check \
  --snapshot "$NEW_PRIVATE_SNAPSHOT" --candidate "$PRIVATE_CANDIDATE"
```

Capture uses explicit-context, fixed-path `kubectl get --raw` calls: the server version, selected
Node, **all** namespaces' Pods, selected namespace's ResourceQuotas and
LimitRanges, and all RuntimeClasses. An authorization error, partial list,
missing resource version, oversized response or deadline failure prevents a
qualifying snapshot. Both output pipes are bounded; the entire capture has one
30-second deadline. The snapshot is created with mode `0600` and cannot replace
an existing file. Check accepts snapshots at most 60 seconds old. Retake a
snapshot after any resource or quota change; never edit its timestamps.

The JSON result uses millicores for CPU and bytes for memory and ephemeral
storage. It names each limiting quota and lists Pending reservations.

- Exit 0, `resource_headroom_passes`: supported resource arithmetic passes for
  this snapshot only.
- Exit 1, `blocked`: a known node resource or quota/LimitRange constraint does
  not fit.
- Exit 2, `unable_to_qualify`: an unsupported, stale, inconsistent or incomplete
  input prevents a conclusion.

None grants permission to enable a pool. Affinity and anti-affinity, taints,
topology, volume binding, admission policies, cached images, other objects
created by ARC, runtime startup and actual CI deadlines still need their normal
checks. Node Pod slots are included; other scheduler predicates are not
simulated. Reads are not atomic and cannot reserve capacity against another
controller. A smaller reservation needs workload evidence and an independently
reviewed profile change; this tool never applies a replacement.

## Supported accounting and conservative boundaries

- CPU, memory and ephemeral storage use exact decimal quantities, with CPU in
  whole millicores and storage in whole bytes. Unrecognized, negative,
  excessively large or finer-grained values fail closed.
- Regular containers add together. Ordinary init containers contribute a
  per-resource maximum; restartable init sidecars remain in later init stages
  and the steady-state sum, following Kubernetes `PodRequests`/`PodLimits`.
- Existing Pods use admitted `spec.overhead` exactly once. Today's RuntimeClass
  is **not** retroactively applied to them. A prospective Pod uses the requested
  RuntimeClass's `podFixed`; conflicting explicit overhead is rejected.
  Overhead adds to requests but only nonzero limits.
- Assigned nonterminal Pods are subtracted from node allocatable resources.
  All target-namespace nonterminal Pods, including Pending ones, provide a
  conservative comparison against quota `status.used`. Terminating or expired
  Pods whose usage Kubernetes has already released may therefore produce
  `unable_to_qualify`; refresh and inspect their lifecycle instead of manually
  subtracting them.
- Every unscoped quota applies independently. `status.hard` must converge to
  `spec.hard`; each tracked dimension needs `status.used`. CPU/memory aliases,
  requests, limits, ephemeral storage, `pods`, and `count/pods` are supported.
  `pods` counts nonterminal Pods; `count/pods` counts all persisted Pod objects,
  including Succeeded and Failed objects. Pending usage is not added twice.
- Container LimitRange minimums, maximums and limit/request ratios are checked.
  Defaults that would fill omitted candidate values are not simulated.
  Pod/PVC LimitRanges, scoped quotas and other resource/quota dimensions return
  `unable_to_qualify`, even if an operator believes them irrelevant.
- Pod-level resources, dynamic resource claims, in-place resize policies or
  allocated/actual resize resource fields are unsupported and cannot pass.
  Container requests alone may not describe those newer accounting features.
  The snapshot records the Kubernetes server version.

If quota blocks admission while node requests fit, inspect the named Pending
reservations and coordinate their owners. Do not cancel jobs or delete Pods
solely because they are old. If `status.hard` differs from `spec.hard`, wait for
the controller and retake the snapshot. An unsupported shape requires separate
Kubernetes analysis or an implemented and reviewed extension; removing its
object from the snapshot is not a workaround.
For a supported smaller reservation, follow the existing
[measured low-share qualification and drain-first rollback](NUC_KVM_PROFILES.md#opt-in-lower-cpu-share-for-an-exclusive-ci-worker)
procedure. Keep limits, isolation and application deadlines unchanged.

## Accounting sources and local checks

The implementation was checked against official documentation for
[container resources](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/),
[Pod overhead](https://kubernetes.io/docs/concepts/scheduling-eviction/pod-overhead/),
and [ResourceQuota](https://kubernetes.io/docs/concepts/policy/resource-quotas/).
The reference 1.31 accounting behavior is also described by upstream
[`PodRequests` and `PodLimits`](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/api/v1/resource/helpers.go),
the [Pod quota evaluator](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/quota/v1/evaluator/core/pods.go),
and [LimitRanger](https://github.com/kubernetes/kubernetes/blob/v1.31.4/plugin/pkg/admission/limitranger/admission.go).
Newer unsupported fields are rejected, rather than interpreted through that
older baseline. This is a diagnostic implementation, not complete Kubernetes
API or scheduler equivalence.

```sh
python3 -m unittest discover -s tests -p 'test_arc_capacity.py' -v
```
