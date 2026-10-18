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
`victron-private-nuc-kvm`, CPU request `1250m` / limit `2`, memory request `3Gi` /
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
  1350m and 3 GiB + 128 MiB including overhead. It fits the existing CI namespace
  request ceiling of 2 CPU; two do not. Account separately for a new listener
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
