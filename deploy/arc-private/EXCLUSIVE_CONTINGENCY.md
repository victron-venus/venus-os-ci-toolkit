# Private exclusive-bind contingency

This is an **unqualified, opt-in contingency**, not the default deployment.
Keep the existing runtime if its qualification meets the workload budgets.
Neither rendering nor the template composer installs anything. Using this
contingency requires a separate review of the maintenance window and the runtime
results below; source tests alone do not qualify it or promise faster CI.

## Exact change and boundary

`render.py --sandbox-runtime victron-private-exclusive` selects a new handler for
private `ci`, `automation` and `release` workers. The default remains
`victron-reserve`; native LAN/ARM values, routing labels, quotas, worker images,
CPU/memory limits and public resources stay unchanged. The existing node guard
selects the worker label and exact namespaces, not the RuntimeClass name, so its
network fence still applies without a new exception.

The new handler points to `/etc/victron-ci-private-exclusive/runsc.toml`. It uses
the same pinned runsc and existing flags, adding only
`file-access-mounts = "exclusive"`. The shared/public handler and its ConfigPath
must remain byte-for-byte unchanged. This flag changes **all non-root bind
mounts**, not only `/var/lib/docker`; it does not change root `file-access`, turn
off sandboxing or introduce a nested overlay. No pod annotations, mount hints,
`allow-flag-override`, binary wrappers or temporary shared-config swaps are used.
[Pinned flag definition](https://github.com/google/gvisor/blob/release-20260928.0/runsc/config/flags.go#L137)
and [mount selection](https://github.com/google/gvisor/blob/release-20260928.0/runsc/boot/vfs.go#L559-L564).

The rendered global `private-exclusive-runtime-use` policy has no namespace
selector: it rejects this RuntimeClass outside exactly `arc-private-ci`,
`arc-private-automation` and `arc-private-release`, including unlabeled namespaces.
Inside them it applies the entire private worker contract, even if namespace
labels are removed. The namespace-selected private policy also requires the new
class when opted in. Both require one `runner` container, the pinned image and
fixed bootstrap, no init/ephemeral containers, no lifecycle hooks, only the
bounded disk-backed Docker emptyDir, no host namespaces/ports/credentials, and no
`dev.gvisor.*` annotations. The additional conditions require `restartPolicy:
Never` and the default termination-message path with `File` policy.

Pod CREATE and UPDATE, including `pods/ephemeralcontainers`, are covered. The
policy does not authorize or restrict CONNECT operations such as `pods/exec` or
`attach`, and does not protect against a cluster/host administrator changing
files or policy. Recheck existing RBAC before qualification: the worker service
account must have no pod, exec, attach, ephemeral-container, secret or admission
write permission; controllers/operators retain only their existing required
access. Only the trusted runtime operator may create or edit RuntimeClasses:
the name-based guard cannot detect an administrator-created alias pointing to
the same handler. No RBAC grant is added here. The absent worker API token and host mounts
remain mandatory. The permitted same-name ARC JIT Secret reference is a
structural check, not an attestation of Secret ownership.

## Mount ownership assumptions that must be verified

These are the six bind destinations observed in the existing single-container
CRI worker. Capture a fresh, names-and-paths-only OCI mount inventory for the
candidate. Unexpected binds or independently cached writers are a stop condition.

- `/var/lib/docker` is a unique Pod-UID disk-backed emptyDir. Guest Docker,
  containerd and tests write it; kubelet reads host-side allocated bytes/inodes.
  No PVC, hostPath, subPath, mount propagation, second workload container or host
  writer may share it. Kubelet removes it after the pod lifetime.
- `/etc/hosts` is the kubelet's **per-pod** hosts file, not node `/etc/hosts`.
  Kubelet writes/truncates it while preparing container mounts. Another container
  or restart could repeat that write, which is why this mode requires one
  container lifetime and rejects init/ephemeral containers. No steady-state host
  refresh was found in the pinned path; this is a lifecycle assumption, not a
  filesystem lock. [Kubelet source](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/kubelet/kubelet_pods.go#L432-L473).
- `/etc/hostname` and `/etc/resolv.conf` are **per-sandbox** files prepared by
  containerd before sandbox start. The latter is generated from CRI DNSConfig or
  copied initially; it is not a live bind to the node resolver. Treat both as
  immutable from the host while the sandbox runs. Dynamic node DNS changes are
  not promised to propagate. [Containerd source](https://github.com/containerd/containerd/blob/v1.7.23/pkg/cri/server/sandbox_run_linux.go#L250-L307).
- `/dev/shm` is sandbox-private tmpfs, not the node's `/dev/shm`. Keep hostIPC
  false. The runtime sandbox may share the backing mount, but no independent
  application writer may use it.
- `/dev/termination-log` is a unique per-container kubelet file. Kubelet creates
  it before start, the guest writes it, and kubelet reads it after exit. `File`
  policy prevents fallback to stdout from hiding broken readback.
  [Kubelet readback](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/kubelet/kuberuntime/kuberuntime_container.go#L519-L545).

Virtual `/proc`, `/dev`, `/dev/pts`, `/dev/mqueue`, `/sys` and cgroup filesystems
are not switched by the bind-access flag. Guest writes to its own hosts/resolver
files are possible; qualification should not introduce external concurrent
writers. Trusted diagnostic exec, host root and cluster-admin remain outside
this ownership guarantee. A sandbox recreation gets fresh caches; do not infer
cross-sandbox cache coherence.

Exclusive mode permits arbitrary metadata/data caching and asynchronous
propagation. Some actual file writes use a donated host FD directly, so neither
correct host readback nor data loss can be inferred solely from the flag.
[Interop contract](https://github.com/google/gvisor/blob/release-20260928.0/pkg/sentry/fsimpl/gofer/gofer.go#L384-L437).
The host-reader tests below are therefore required in addition to the absence
of external writers.

## Offline preparation

Use a protected operator directory. Save fresh read-only copies and hashes of
the full active K3s containerd template, generated configuration, shared runsc
configuration, unit/drop-ins, renderer inputs and prior Helm values. Do not save
credential values in the report. Confirm K3s 1.31/containerd 1.7 schema and the
actual `config.toml.tmpl` path; this fragment is not a containerd 2.x template.

The existing node uses a full Go template with dynamic runtime blocks. Preserve
the entire template; do not replace it with the small fragment or a
`{{ template "base" . }}` template. Compose a separate local file from the
reviewed snapshot:

```sh
python3 deploy/arc-private/compose_exclusive_template.py \
  --input /private/operator/config.toml.tmpl.snapshot \
  --expected-sha256 REVIEWED_SNAPSHOT_SHA256
```

The composer always writes `config.toml.tmpl.candidate` beside the snapshot;
there is no arbitrary output-path option. Its directory must be owned by the
invoking user with mode `0700`. It anchors both file operations to the opened
directory descriptor, refuses a symlink directory or input, and requires a
regular input file. It checks the input hash, rejects an existing private
handler, and creates output exclusively with mode `0600`; an existing file or
symlink is never overwritten. These are local operator CLI safeguards, not an
HTTP input boundary. Review that the sole appended block is
`containerd-exclusive-runtime.toml.fragment`, and that the named handler is not
also emitted by a dynamic runtime discovery block. The composer does not render
or validate the Go template. Save the candidate hash and preserve source file
ownership/mode for installation and rollback.

Before host installation, separately concatenate the captured **generated TOML**
and the same fragment in memory and parse them with `tomllib.loads`. Reject
duplicate tables, invalid syntax or any change to the original parsed runtime
definitions. This checks the static fragment against the current generated
configuration; it does not render the full Go template or prove that a later
K3s render will not emit a conflicting dynamic block. The actual post-restart
generated configuration still needs the checks below.

Render to a **new** protected directory using the same reviewed image digests,
node address, listener placement and inventory as the active plan, with only
`--sandbox-runtime victron-private-exclusive` added. Compare the complete output:
only private sandbox RuntimeClass references and the described admission/class
resources may differ. Do not overwrite another operator's current render. Do
not apply the whole foundations list without reviewing every object.

## Bounded maintenance and qualification sequence

1. Proceed only after the existing qualification ends and a maintenance decision
   explicitly selects this contingency. Set a maximum maintenance deadline
   before touching the host: one normal restart, at most five minutes to regain
   Ready/CRI health, then the rollback path below. Each observation must have a
   bounded timeout; do not loop restarts or issue `killall`.
2. Record active jobs, ARC workers/listeners, node readiness/taints, pod UIDs and
   restart counts, service endpoints, PVCs and stateful workloads, hostNetwork
   and hostPort consumers, DNS/ingress and management/WireGuard reachability.
   Coordinate a hold on new worker creation for the affected node's pools:
   preserve each pool's values and set both `minRunners` and `maxRunners` to zero,
   the documented [ARC queue-draining configuration](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets#jobs-queue-draining).
   Confirm the controller has observed it; `minRunners: 0` alone is not a hold.
   Existing non-CI applications stay running. Wait for current CI workers to
   drain through their normal cleanup; do not cancel unrelated jobs.
3. **Before loading the handler or creating any RuntimeClass**, install only the
   global policy/binding. Verify its observed generation, zero CEL type-check
   warnings and the absence of this handler from the target node's loaded CRI
   map. Take a fresh cluster-wide Pod and RuntimeClass inventory:
   require no Pod already naming `victron-private-exclusive`, no existing class
   with that name and no class alias whose handler is `victron-private-exclusive`.
   Refuse unexpected objects and retain their identities for review; do not
   delete or adopt them. This is a conservative check for stale references and
   aliases, not a claim that normal admission permits unknown RuntimeClasses:
   Kubernetes 1.31.4's mutating RuntimeClass admission rejects an unknown class
   **before** validating admission policies run. Such a refusal cannot prove
   that this global guard works.
   [Admission order and lookup](https://github.com/kubernetes/kubernetes/blob/v1.31.4/plugin/pkg/admission/runtimeclass/admission.go#L95-L104),
   [unknown-class rejection](https://github.com/kubernetes/kubernetes/blob/v1.31.4/plugin/pkg/admission/runtimeclass/admission.go#L150-L158).
   After these checks, create the reviewed RuntimeClass while its handler is
   **still unloaded**, then install the matching private policy update. The
   latter is needed for a positive private fixture; the old private policy
   requires the shared class. Keep worker creation held and verify both policies
   have been observed without CEL warnings. Run Pod CREATE fixtures with
   `dryRun=All`: a correct private pod must be accepted, and use of this class in
   native and unlabeled namespaces must fail with the intended
   `private-exclusive-runtime-use` policy name and namespace-guard message.
   In the public reserve namespace, the existing `victron-reserve-workers`
   policy may reject first with `gVisor is mandatory`. Record that exact result
   as combined boundary protection, not independent proof of the new global
   policy. Public pods using the shared class must still pass positive controls.
   Exercise the other applicable CREATE negatives from the existing matrix.
   Unknown-class, quota, authorization or unrelated policy failures do not count
   as proof of the intended validation; record the exact rejecting policy and
   message. No fixture Pod is persisted. Keep the trusted RuntimeClass-writer
   and worker-creation holds throughout this transition.
4. Record the prior cordon state, then cordon the target node during the approved
   maintenance window. Confirm no direct `nodeName`/DaemonSet/bound-pod path is
   creating unexpected work. Cordon is scheduling protection, not eviction or a
   freeze of node writers. Do not drain, stop neighboring applications, restart
   WireGuard or run `k3s-killall.sh`. Verify the actual service uses
   `KillMode=process` and has no custom destructive stop hook. K3s service stop
   normally preserves running containers; this still interrupts kubelet/CRI
   management and can affect readiness/endpoints. There is **no zero-downtime
   guarantee**. [K3s lifecycle](https://docs.k3s.io/upgrades/killall).
5. Install the new private runsc file and hash-verified appended template using
   atomic writes only after a fresh cluster-wide scan confirms no Pods reference
   the new class, the sole expected class has its recorded UID/spec, and there
   are no handler aliases. Refuse unexpected objects rather than removing them.
   Preserve the originals and file metadata. Confirm the
   shared ConfigPath file is unchanged. Perform one normal `k3s-agent` restart.
   A RuntimeClass alone cannot register a handler: containerd's loaded runtime
   map must contain it. [CRI lookup](https://github.com/containerd/containerd/blob/v1.7.23/pkg/cri/server/sandbox_run.go#L675-L704)
   and [K3s template configuration](https://docs.k3s.io/advanced#configuring-containerd).
6. Within the maintenance deadline, verify generated config parses and contains
   the original handler definitions unchanged plus exactly one new handler;
   query actual CRI configuration, not only the file. Verify node/CRI health,
   existing pod identity/restarts, endpoints, stateful health and management
   connectivity against the baseline. If any check fails, keep the CI hold and
   use rollback; do not create a qualification pod to diagnose basic node health.
7. Reverify that the global guard, recorded class and private policy remain
   unchanged, and repeat the no-Pod/no-alias scan while worker creation remains
   held. Repeat the applicable server-side CREATE fixtures against the loaded
   configuration, requiring the intended policy name/message for each negative
   (including the explicitly classified public-policy result from step 3):
   correct private pod accepted; same class rejected in public, native and
   unlabeled namespaces; extra/init containers rejected;
   host namespace/volume/port and arbitrary gVisor annotation
   attempts rejected; `Always`/`OnFailure` and non-default termination paths or
   policies rejected. The actual `pods/ephemeralcontainers` UPDATE dry-run needs
   an existing Pod: exercise it against the first separately approved isolated
   qualification Pod in the next step, without persisting the added container,
   before allowing any real GitHub job. Do not count an absent-Pod or forbidden
   CREATE response as proof of that subresource validation. Preserve the
   existing private admission fixture matrix.
   This draft's Python tests are structural tests, not live CEL acceptance.
8. Restore scheduling only if the node was schedulable before maintenance and
   health checks pass; keep the ARC worker-creation hold. Run one explicitly
   approved isolated qualification at a time using the same image, candidate
   1900m standard request, 100m RuntimeClass overhead, limits, emptyDir and network
   fence. Use the fixed bootstrap's existing qualification mode. Do not relax admission to
   inject a different command, init container, helper mount or extra secret.
   The canonical Docker/PostgreSQL qualification keeps the same **1800-second
   (30-minute)** overall deadline as V7; do not change the comparison contract.
   Keep its BusyBox build,
   deletion/opaque-layer assertions, PostgreSQL SQL and published-port checks.
9. Complete the readback cases below in separate sequential disposable pods if
   different termination outcomes are required. Use bounded trusted operator
   exec only to run probes in the same admitted container; do not grant worker
   exec/API permissions. Observe normal command exit separately from termination
   of the container's main process. After each case wait up to two minutes for
   ordinary pod/sandbox/scratch cleanup; if still present, preserve the failure
   and use an explicit operator cleanup decision, not a blind force-delete loop.
10. Only after all runtime, readback and isolation gates pass, recheck the fleet
   smoke's actual cold Docker budget (create 180s, container run/inspect 30s,
   whole job 10 minutes). Then select one pool for a real GitHub job and require
   artifact upload, post steps and exact pod/runner/JIT-Secret removal. Expand
   within the existing namespace quotas only after that proof. A passing
   infrastructure probe is not passing application CI.

## Additional runtime acceptance

- Confirm actual runsc command/config and all bind source/destination identities
  against the six-mount contract. Verify the shared/public config and handler
  hash stayed unchanged. Keep guest UID1001, sudo success, explicit no-new-privs
  sudo failure, nested Docker overlay2 and all existing negative network probes.
- Test scratch host visibility while the sandbox remains alive: create a bounded
  64MiB allocated file plus small files, then overwrite, truncate and unlink.
  Record guest write acknowledgements, host backing-directory allocated bytes
  and inodes, and kubelet volume statistics. Wait at most two measured kubelet
  collection intervals plus 30 seconds (cap five minutes). Use an idle scratch
  interval so concurrent Docker writes cannot disguise the result. Validate the
  direction and bounded convergence of usage, not exact block equality between
  filesystem implementations. Guest `df` alone is insufficient. Do not fill
  16GiB merely to test the accounting path. [Host metrics implementation](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/volume/metrics_du.go#L36-L70).
- Write and close a unique marker of less than 1KiB to `/dev/termination-log`,
  then test normal main-process exit, handled TERM and forced termination after
  acknowledgement. Check the kubelet's actual `terminated.message`; neither
  guest `cat` nor stdout proves host readback. Add no compensating `sync`/`fsync`
  absent from the real workload. Where ARC deletes the object quickly, retain
  its observed status before cleanup. Failure or missing observation blocks
  rollout; do not claim durable behavior for an unobserved SIGKILL path.
- Compare hosts/hostname/resolver backing identities and contents before/after
  the lifetime, with no test intentionally modifying them. Confirm no external
  writer or extra mount appeared. Verify final sandbox and emptyDir removal.
  For real ARC jobs additionally observe deregistration and same-name JIT Secret
  absence using names-only lookups, never dumping Secret data.

## Rollback

On pre-qualification failure, keep new worker creation held. If no exclusive
pod ever started, restore the original template and private policy directly. If
any did start, first let it finish or follow a separately approved bounded
cleanup; verify no exclusive pod/sandbox remains before reverting the private
selector/policy and removing its RuntimeClass. Keep the global guard active
until no exclusive pods/sandboxes remain, the class and any reviewed aliases are
absent, and the handler has been removed from the loaded CRI map. Unexpected
aliases or references stop rollback for review; do not delete them blindly.
Restore saved private Helm values only after the
shared policy is ready, so they cannot race incompatible admission.

Restore the exact captured containerd template (bytes, owner and mode), leaving
the existing shared runsc configuration unchanged, then perform one normal
service restart and the same five-minute health checks. Removing the handler
from a file without restarting does not remove it from the loaded CRI map.
Retain the private config file until its handler is no longer loaded and all
exclusive sandboxes are gone; retaining the inert file is safe during recovery.
Remove the newly introduced global policy/binding last, only after the absence
checks above and confirmation that the handler is no longer loaded.
Restore the prior cordon state and worker-creation policy only after health is
verified. If rollback does not restore health within its deadline, stop and
escalate with evidence; do not repeat restarts, drain or kill all containers.

Record source SHA, exact image digest, hashes of both runsc configs and template,
all changed objects, actual runtime/mount evidence, deadlines, outcomes and
cleanup. Rebuilding an image can change its digest; it is not part of this
same-image comparison. Leave this option unused if the existing runtime passes.
