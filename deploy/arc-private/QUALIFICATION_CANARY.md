# Qualify a site runner image without enabling a runner pool

The private ARC renderer can produce one temporary, credential-free qualification
Pod and one exact alternative in the existing NUC admission policy. This is an
operator-controlled image acceptance step, not a new ARC pool. Omitting
`--qualification-canary-plan` preserves the existing rendered configuration.
Pool values, repository Actions variables, JIT rules and public runner defaults
do not change when the option is present either.

The candidate must already have passed the source-built image scans and the site
derivation checks described in [Runner image artifacts](../../docs/RUNNER_IMAGE_ARTIFACTS.md).
An image digest alone is not evidence that its site CA, bootstrap, network fence
and storage work together. Conversely, a successful Pod does not waive image
scan findings or authorize deployment of that image to production pools.

## Private operator plan

Keep the plan and renderer output outside Git with mode `0600`; they identify a
private repository scope and the operator's Kubernetes username. Use the existing
operator inventory and complete NUC plan. Select an existing **standard CI** scope
with `tmpfs-2gi`, not a heavy or automation profile. Obtain `creator_username`
from the authenticated operator's `kubectl auth whoami` result; do not use a
service-account, node or anonymous identity.

This example uses placeholder data and cannot be deployed as written:

```json
{
  "schema": 1,
  "name": "qualification-private",
  "namespace": "arc-private-ci",
  "service_account": "reserve-worker",
  "scope": "example/standard",
  "profile": "ci",
  "node": "nuc",
  "runtime_class": "victron-private-nuc-kvm",
  "image": "registry.example/qualified@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "bootstrap_profile": "private",
  "creator_username": "cluster-operator@example.invalid"
}
```

The optional `cpu_profile` accepts only `standard` (the default) or `low-share`.
Existing plan files remain valid and retain the original resource budget. Set
`"cpu_profile": "low-share"` explicitly to qualify the same complete probe with
a smaller CPU reservation. Arbitrary CPU values are not accepted. This choice
applies only to the temporary canary; production pool resource profiles stay
unchanged.

Add `--qualification-canary-plan /private/path/qualification.json` to the existing
reviewed renderer command. The new `qualification-canary.json` contains the Pod.
The only changed existing resource is `victron-private-nuc-kvm-workers` in
`foundations.json`. Compare with output from the same command without the option:
all pool definitions, network policy, node guard and routing must be identical.
Do not apply a whole rendered directory as a shortcut for this qualification.

Only one exact canary is authorized at a time. Qualify public and private site
images **sequentially**, using a distinct name and digest for each plan:

- `public` uses the public image's `overlay2` bootstrap and contains no
  `RUNNER_PRIVATE_*` inputs.
- `private` uses `ci-tmpfs-2gi`, `overlayfs` and the existing exact downward API
  memory/profile references. The 2 GiB tmpfs lives inside the sandbox, not in a
  Kubernetes memory-backed volume.

Both modes use the fixed `start-runner.sh` entry point with
`RUNNER_RESERVE_QUALIFICATION=1`. No JIT configuration, GitHub secret, Kubernetes
token, arbitrary command, host volume or extra container is accepted. CREATE
requires the exact operator identity. UPDATE requires an already matching
`oldObject` identity, allowing kubelet status updates without letting another
Pod become a canary. DELETE remains outside these admission rules for cleanup;
normal Kubernetes authorization still applies.

## Acceptance and rollback

Before applying anything, review the exact emitted policy and Pod; validate them
against the cluster's actual Kubernetes API with server-side dry runs and inspect
the policy's type-checking status. The CEL unit tests below do not replace that
integration check. A Pod dry run against an unchanged production-only policy is
expected to reject the qualification environment: the exact temporary policy
must first be reviewed and installed in the authorized maintenance step.

Ensure the candidate image is already present by digest on the selected node,
the existing node guard has healthy TLS and the node has sufficient free disk,
CPU and memory. The Pod keeps `imagePullPolicy: Never`, the gVisor RuntimeClass,
existing scoped egress and cross-namespace exclusive NUC anti-affinity. It cannot
preempt existing jobs and waits for active workers to finish. No service or runner
is restarted by rendering this plan.

The `standard` budget requests 500m CPU; `low-share` requests 100m. Both retain
the 2 CPU limit, 3/6 GiB memory and 1/20 GiB ephemeral storage (request/limit),
16 GiB disk scratch, and the existing RuntimeClass overhead. Including that
100m CPU overhead, the scheduler must have 600m or 200m available respectively.
The CPU reservation controls scheduling and the relative share during contention;
it is not a measured minimum or a guarantee that the probe will finish. The
unchanged 2 CPU ceiling allows bounded bursts for nested Docker, image unpacking,
BuildKit and PostgreSQL startup. The probe performs no full project compilation.

Use `low-share` only after checking current reservations, queued CI and node
capacity. It runs the same TLS, isolation, toolchain, offline npm, BuildKit,
Compose and PostgreSQL checks. No test, per-step deadline or admission condition
is skipped. `activeDeadlineSeconds` remains 600 and termination grace 15 seconds.
Capture the selected CPU profile, image digest, rendered inputs, Pod status,
elapsed time, CPU usage/throttling and qualification log before cleanup; do not
record credentials. A failed or timed-out qualification is not acceptance. A
successful low-share probe does not establish general CI capacity or worst-case
performance while other services are busy.

After each attempt, delete only that exact Pod and restore the reviewed existing
policy rendered **without** the optional plan. Confirm the candidate name/image
and qualification environment are rejected again. Leave production pool images,
Actions routing and JIT issuance unchanged. Promotion, if approved later, is a
separate reviewed configuration change.

## Repeatable software checks

```sh
python3 tests/test_runner_qualification.py -v
cd tests/cel
go test -count=1 -mod=readonly ./...
```

The Python checks verify output isolation, all 43 fixture pools and node-guard
discovery. The separate locked Go module evaluates the **emitted expressions**
with the CEL interpreter against positive production and all combinations of
both bootstrap modes and CPU profiles,
malformed inputs, identity changes, JIT mixing and UPDATE escapes. Compile errors
fail the test; they cannot count as successful negative cases. These are dynamic
CEL fixtures, not a Kubernetes API server or a claim of hardware acceptance.
