# Explicit full-memory reservation for standard CI

`NucKvmPlan.full_memory_ci_scopes` is an optional list of already selected CI repository scopes. A listed scope renders the existing standard `tmpfs-2gi` worker with memory request 6Gi and limit 6Gi. It changes only the memory request and the corresponding exact NUC admission tuple. CPU reservation/limit, image, storage mode, tmpfs bound, serial scheduling, isolation, worker maximum and runtime overhead are unchanged.

The field is opt-in. Omission or an empty list preserves the existing standard 3Gi/6Gi profile, including experimental qualification. Heavy 13Gi/16Gi and bounded-heavy 9Gi/12Gi remain unchanged. A scope must be an explicitly selected private standard CI target using `tmpfs-2gi`; unknown, duplicate, malformed, light-concurrent or heavy selections are rejected. `low_share_ci_scopes` can separately select the existing exclusive CPU reservation for the same scope. There is no arbitrary memory override.

For a strict complete NUC plan, add this optional field alongside the existing targets, immutable image and node address:

```json
{"full_memory_ci_scopes": ["example/project"]}
```

This fragment is not a complete plan or deployment instruction. The renderer never activates a pool. Choose the final reservation only after the required workload qualification and fresh scheduler/quota review; rendering this option is not evidence of successful qualification. With the current 128Mi RuntimeClass overhead, one 6Gi worker charges 6272Mi of requested memory. CPU request/limit need not be equal, so this option does not claim Kubernetes Guaranteed QoS.

The existing standard private image storage gate accepts memory requests between 3Gi and 6Gi inclusive with a 6Gi limit. No image payload or build-context file changes are needed for this option. The NUC admission policy derives its exact allowed resource map from the rendered worker; the shared-runtime 3Gi/6Gi admission branch is intentionally unchanged.
