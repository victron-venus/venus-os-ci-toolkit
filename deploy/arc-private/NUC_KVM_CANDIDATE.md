# Opt-in private NUC KVM candidate

For an explicit set of standard CI, heavy CI and automation profiles, see
[Multiple private profiles on one NUC](NUC_KVM_PROFILES.md). That interface uses
one complete target plan and requires scheduler-enforced serialization across
namespaces. It does not qualify or activate additional workloads automatically.

This renderer option prepares one repository's standard CI pool. It does not
install a runtime, start a runner, change GitHub routing, qualify an image, or
declare earlier failed capabilities supported. Keep the candidate unqualified
until the actual node, image, network and unchanged application jobs pass review.

The fixed worker contract is `nuc`/`amd64`, RuntimeClass and handler
`victron-private-nuc-kvm`, CPU request `600m` / limit `2`, memory request `3Gi` /
limit `6Gi`, and RuntimeClass overhead `100m` / `128Mi`. The existing private
`ci-tmpfs-2gi` bootstrap uses a guest-only 2 GiB / 262144-inode tmpfs with the
containerd `overlayfs` image store. There is no new Docker wrapper or qualifier.
The pool remains `minRunners=0`, `maxRunners=1`; original job timeouts stay in the
consumer workflow. Heavy CI, automation, release, LAN, ARM and public workers
are outside this option.

## Render, without deployment

Use the existing private inventory schema with an explicitly private selected
row, `profiles` including `ci`, `ci_resource_class=standard` (or its default),
and `ci_storage=tmpfs-2gi`. The operator supplies the one exact repository name,
verified image digest and actual NUC guard address; no image or CA is guessed.

```sh
python3 deploy/arc-private/render.py \
  --inventory "$PRIVATE_INVENTORY" \
  --sandbox-image "$EXISTING_MP_IMAGE" --native-image "$EXISTING_NATIVE_IMAGE" \
  --node-ip "$EXISTING_MP_GUARD_IP" --listener-node "$LISTENER_NODE" \
  --nuc-kvm-scope "$SELECTED_PRIVATE_REPOSITORY" \
  --nuc-kvm-image "$VERIFIED_NUC_IMAGE" --nuc-kvm-node-ip "$NUC_GUARD_IP" \
  --output "$NEW_PRIVATE_OUTPUT"
```

All three NUC arguments are required together. A one-row CI-only inventory is
supported. With the full inventory, only the selected CI values change; the
other pool values, labels and repository variables remain unchanged. The normal
private foundations gain explicit NUC delegation and scoped network isolation.
Do not apply the whole index just to test this one pool, and do not reuse an old
temporary policy that allowed a different image or CPU tuple.

The generated `foundations.json` describes the current source contracts; it is
not a safe wholesale migration of an older live policy. In particular, a live
baseline may still allow only the earlier image/backend, while current source
also describes unqualified profiles. For that baseline, prepare an addon
transition bound to its actual policy UID, resource version and spec hash:
install the new global worker/class guards and their bindings, then add only
the exact NUC-runtime delegation and selected-scope fallback rejection to the
existing private policy. Preserve every existing validation and public policy
byte for byte. Separately review the selected-scope NetworkPolicy exclusion
and new NUC policy; preserve the live policies' other selectors, egress and
deny ranges. Observe the guards and test their positive/negative cases before
exposing the class/handler or installing the one pool. Do not apply generated
namespaces, quotas, other pools or the full common private policy as a pilot
shortcut. Keep snapshots and an exact rollback for each staged change.

## Host and network prerequisites

- Establish fresh scheduler fit and no node pressure. One worker requests
  700m and 3 GiB + 128 MiB including overhead. It fits the existing CI namespace
  request ceiling of 2 CPU; CPU quota alone does not serialize two workers.
  Required cross-namespace anti-affinity still permits only one running worker.
  Account separately for a new listener
  and host guard. Do not change neighbours' requests or assume historical fit.
- Verify actual KVM access on the host. The runtime uses KVM; the job gets no
  host `/dev/kvm`, host socket, host namespace or privileged native container.
- Install the complete official `release-20261012.0` amd64 bundle into a new
  immutable directory, preserving existing bundles. The archive at
  [the pinned release](https://github.com/google/gvisor/releases/tag/release-20261012.0)
  is `gvisor-x86_64.tar.bz2`, SHA256
  `180dd277f657c1478f09490e8a9a595f7378257510b43d1d3694a769aef9ed02`.
  Verify the actual shim, frontend and adjacent Sentry, not a frontend path alone.
- [runsc-nuc-kvm.toml](runsc-nuc-kvm.toml) selects KVM while retaining the existing
  networking and guest sudo flags. It enables no arbitrary annotation override
  or exclusive bind access. The existing shared mp handler/config is unchanged.
- [The containerd fragment](containerd-nuc-kvm.toml.fragment) is an additive
  config-v2 stanza, not a complete template. Its absolute `runtime_path` selects
  the same-release shim without changing the default PATH resolver or another
  handler. This option is supported by [containerd 1.7.23](https://github.com/containerd/containerd/blob/v1.7.23/pkg/cri/config/config.go#L59-L67).
  Inspect the current full K3s
  template/generated config and preserve its other handlers. Registration may
  require the separately approved NUC-agent maintenance window and rollback;
  this renderer performs neither. See [K3s containerd configuration](https://docs.k3s.io/advanced#configuring-containerd)
  and [gVisor platforms](https://gvisor.dev/docs/architecture_guide/platforms/).
- Reuse unchanged `../runner-reserve/node_guard.py`, `network-policy.json` and
  the verified `crictl` at their canonical host paths. Install
  [nuc-node-guard.service](nuc-node-guard.service) as
  `victron-private-nuc-guard.service` only on NUC after checking port/table/unit
  conflicts. Its root-owned `node-guard.json` supplies actual `node_ip`, `dns_ip`,
  full infrastructure `extra_denied`, `tls_certificate` and `tls_private_key`.
  The local CRI/CNI/veth/MAC/UID checks, bridge anti-spoof rules and fail-closed
  HTTPS readiness are the same as the shared guard; LAN/ARM remain excluded.
- The server certificate SAN must cover the actual NUC address. The selected
  image must trust that CA at `/opt/victron-ci-reserve/guard-ca.crt`. A new public
  CA in an image requires a new verified digest; never place the server private
  key in an image. Retain the unchanged bootstrap and its TLS/Pod-UID check.
- NetworkPolicies are additive: the old CI policy excludes this exact scope,
  and its dedicated policy admits only DNS, public HTTP(S) with the existing
  denied networks, and NUC `/32`:19999. It does not add access to the mp guard.

## Guard lifecycle: qualification and persistent standby

Inert installation and durable standby are separate, reviewed states. The inert
installer leaves the guard disabled and does not start services. Qualification
may explicitly start that installed unit once; a successful one-shot start does
not make it start on a future boot. Keep that behavior in the installer and
renderer. Do not add automatic enablement, another watchdog, or a restart timer.

After the exact source, configuration, TLS identity, reconciliation, network
fence and worker profile have passed acceptance, the owner may designate that
unchanged guard as **persistent standby**. Record this decision and confirm that
no maintenance or rollback hold is active. Check the approved file hashes,
ownership, modes and absence of unexpected symlink changes; the loaded unit's
`FragmentPath` and `DropInPaths` must match that record. Inspect its current state:

```sh
unit=victron-private-nuc-guard.service
systemctl show "$unit" -p FragmentPath -p DropInPaths -p UnitFileState \
  -p ActiveState -p SubState -p MainPID -p Result -p NRestarts -p ExecMainStatus
```

For an already active, unchanged, accepted guard, this explicit transition only
enables its existing boot dependency. Run it as one checked sequence after the
review above; an inactive, failed, masked or changed unit requires separate
diagnosis and is not an invitation to start or repair it automatically.

```sh
set -eu
unit=victron-private-nuc-guard.service
systemctl is-active --quiet "$unit"
before_pid=$(systemctl show "$unit" -p MainPID --value)
before_fragment=$(systemctl show "$unit" -p FragmentPath --value)
test "$before_pid" -gt 0
sudo systemctl enable "$unit"
test "$(systemctl is-enabled "$unit")" = enabled
test "$(readlink -f "/etc/systemd/system/multi-user.target.wants/$unit")" = "$before_fragment"
systemctl is-active --quiet "$unit"
test "$(systemctl show "$unit" -p MainPID --value)" = "$before_pid"
```

Do not add `--now` or restart the healthy guard. As documented by
[systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html#enable%20UNIT%E2%80%A6),
enablement creates the unit's boot links; it does not start it. The existing
[`Restart=always`](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Restart=)
handles ordinary process exits subject to rate limits, but does not undo a
deliberate stop. Do not add stop propagation from the runtime service: the guard
already revokes admission when runtime discovery fails. Test boot and deliberate
stop semantics in a disposable systemd fixture; confirm a real boot only during
a separately approved maintenance window, never by rebooting an active host for
this check.

An enabled/active unit and a verified-TLS denial for a bogus Pod UID establish
listener identity and a negative authorization result. They do **not** prove a
fresh CRI reconciliation, current firewall ownership, or successful worker
admission. Verify those separately with the accepted source/configuration and
owned fence, then retain the exact worker identity and application evidence.
Keep bounded journal diagnostics private; do not publish addresses, Pod UIDs,
credentials or raw response bodies.

If readiness fails, hold new assignments and investigate. Do not automatically
reset failures, unmask a unit, clear a fence, change routes, or restart a healthy
guard. Existing workers do not continuously poll the startup endpoint. Preserve
their evidence and drain them under the reviewed rollback plan before changing
host state.

The common worker bootstrap makes at most 60 admission requests, retaining its
two-second connect deadline, three-second request deadline and two-second pause
between failed attempts. Only a successful verified-TLS response containing the
literal JSON boolean `"admitted": true` permits startup. Transport failures skip
JSON parsing; invalid responses and denials remain failures. The response is
limited to 64 KiB by curl and independently by the parser's bounded read. The
worker image must retain curl 8.4 or newer so the download limit also applies
when the server omits the response length; see the
[curl size-limit contract](https://curl.se/docs/manpage.html#--max-filesize).
Diagnostics report only a bounded attempt count, sanitized failure category and
numeric curl exit status. A bootstrap source fix takes effect only after
rebuilding, qualifying and pinning its worker image; merging this source does
not update already installed images.

## Admission, review and rollout boundary

Install and observe the two global Fail/Deny guards before exposing the new
RuntimeClass/handler. The worker guard covers CREATE and UPDATE, including
`pods/ephemeralcontainers` and UPDATE on `pods/status`, across all namespaces.
It matches either old or new NUC runtime, and either old or new selected scope
in the CI namespace, so a label change cannot evade the guard. It requires the selected
private CI scope, exact image/node/resource/storage/guard inputs and inherited
no-host/no-credentials constraints. The class guard rejects another class name
using this handler and changes to its node/overhead. Review pre-existing class
aliases and Pods; an admission policy does not retroactively remove them.

The original private policy delegates only the exact new RuntimeClass to the
global guard and refuses an mp fallback for the selected CI scope. Other private
and all public workers retain their original contracts. ARC's same-name JIT
Secret reference remains the only permitted environment Secret; this structural
check is not a Secret-ownership lookup or a replacement for controller RBAC.

Updates preserve the scope, profile and `runner-reserve.github.io/role` labels
and an already bound node. Normal kubelet status changes and unrelated ARC label
additions remain allowed. This also protects NetworkPolicy selection and the
host guard's worker discovery: Kubernetes 1.31.4's
[PodStatusStrategy](https://github.com/kubernetes/kubernetes/blob/v1.31.4/pkg/registry/core/pod/strategy.go#L203-L227)
restores the old spec and owner references on status writes, but does not restore
labels. The full inherited contract, including the prohibition on `dev.gvisor.*`
annotations, applies to those writes too.

Before a real worker, obtain actual Kubernetes-version CEL type-check results
and server dry-run positive/negative results. A rejection for an unknown
RuntimeClass is not proof of either policy. Test outside namespaces/scopes,
wrong nodes/images/resources, duplicate or substituted guard environment,
runtime aliases, host mounts/namespaces, and init/ephemeral containers. Preserve
the public/shared positive controls. No local renderer test claims live admission.

After host/network qualification and reviewed source/image pins, inspect the
exact repository queue, then install only this one pool with max1. Verify actual
runner/image/runtime/resource identity and the unchanged application jobs, with
Pod/registration/JIT cleanup. Preserve earlier failed PostgreSQL results and do
not extend success to unused profiles. Rollback first stops new assignments and
drains exact workers; restore reviewed prior policies/config only after no NUC
instances remain. Keep the global guards until the class/handler are removed.
Never switch queued work to the old mp route without explicit review.
