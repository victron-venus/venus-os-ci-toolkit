# Isolated runner templates on k3s

These are **unapplied reference templates**, not evidence of a working pool.
The templates describe isolated CI, emulator and release roles for trusted
consumers. Keep concrete consumer identities, registration credentials, host
inventories and deployment evidence in access-controlled operator records.
Never reuse an unrelated runner registration, namespace or credential.

The public-project reserve is documented separately in [RUNNERS.md](RUNNERS.md).
Preparing either pool does not switch consumers away from GitHub-hosted runners.

## Pools and access

- `ottplay-k3s-linux-x64`, namespace `arc-ottplay-ci`: one runner maximum,
  4 CPU / 8 GiB limit; group `ottplay-private-ci`.
- `ottplay-k3s-kvm-x64`, namespace `arc-ottplay-kvm`: disabled (`maxRunners: 0`)
  until KVM admission; intended maximum one, 4 CPU / 10 GiB limit; same CI group.
- `ottplay-k3s-release-x64`, namespace `arc-ottplay-release`: disabled until
  signing-group admission; intended maximum one, 4 CPU / 6 GiB limit; group
  `ottplay-private-release`.

All scale sets have `minRunners: 0`. At approved maxima the runner limits total
12 CPU / 24 GiB, plus bounded ARC overhead. Each pod has a 48 GiB disk limit;
actual existing workload reservations must be checked before enabling all pools.
No workspace, toolcache, registration or SDK PVC is shared between jobs.

Review the selected-repository and workflow policies in a private operator copy
of [`github-groups.json`](../deploy/arc-ottplay/github-groups.json). Release runners
must accept only reviewed release and smoke workflows at their exact default-
branch references. Verify GitHub's returned policy; a group name is not an access
boundary. If a restriction is unsupported or references a workflow not yet present
on the default branch, leave the affected pool disabled. Do not broaden access to
make activation succeed. Review branch protection and an exact source SHA before
activation.

Create a dedicated organization-owned GitHub App: organization **Self-hosted
runners: read/write**, repository **Metadata: read-only**. Organization scope
does not need repository Administration permission. Install it only for the
selected consumer repositories. No PAT, existing runner token or signing secret
belongs in these values. The App secret `ottplay-runner-app` has keys `github_app_id`,
`github_app_installation_id` and `github_app_private_key`. Provision it through
the approved secret store into **each scale-set namespace**, not the controller
namespace; the charts create the controller's required namespaced bindings.
Never mount it in a job pod. [ARC authentication](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/authenticate-to-the-api).

## Image and installation

Build `deploy/arc-ottplay/Dockerfile` for `linux/amd64` on an approved builder.
The upstream runner, Node and Ubuntu 24.04 indexes are pinned; inspect updates
and rebuild regularly. Apt packages are resolved at build time, so retain the
build provenance/SBOM and scan the **resulting image**. Publish it to the chosen
registry and replace `REPLACE_WITH_BUILT_DIGEST` in private copies of all three
values files. Configure an image-pull Secret reference if required; a build tag
is not the delivery pin. Publish and pin the delivery image before installation.

The image includes Python/pip/venv, JDK 17, Node 22, git, gh, curl, jq, ffmpeg,
compiler tools, and Playwright 1.63.0 Chromium OS dependencies. The dependency
installer uses the committed buildtools npm lockfile with lifecycle scripts
disabled; its installed CLI is invoked directly and removed after the build.
Browser binaries follow the consuming lockfile. Use `playwright install` without
`--with-deps`; do not run apt/sudo in jobs. Python installs use a job venv or
`setup-python` (Ubuntu system Python follows PEP 668). `setup-node` and
`setup-java` have writable `/opt/hostedtoolcache`. Android workflows install
their pinned command-line tools and SDK into a fresh writable directory under
`RUNNER_TEMP`; `/opt/android-sdk` is also a disposable writable mount.

After cluster/API access and namespace policies are reviewed:

1. Check existing ARC CRDs/controllers, node reservations and image compatibility.
   Do not install over another ARC controller without reviewing its watch scope.
2. Apply `namespaces.yaml` and provision the App secrets privately. Runner network
   policies allow cluster DNS and public HTTPS, denying ingress and private-network
   egress. Verify CNI enforcement and registry/cache needs. Controller/listener
   pods need Kubernetes API and GitHub access and are not job pods.
   Kubernetes NetworkPolicy does not replace the worker node's host firewall and
   routed-overlay rules: standard policy semantics exempt traffic to/from the
   pod's hosting node. RFC1918 exclusions therefore do not prove isolation from
   services on the worker node itself. Require separate host-firewall admission and
   a live node-service denial test. Test DNS/HTTPS from the actual runner pod, not only
   the host, and verify denied private/API paths after CNI translation. Do not
   open the host firewall broadly to resolve a failed download.
3. Install controller and scale-set charts at **0.14.2**, whose reviewed OCI
   digests are recorded in `versions.json`. Use the corresponding values files.
   Controller release `ottplay-arc` belongs in `arc-ottplay-system`; runner release
   names equal the pool names above. The controller ServiceAccount is explicitly
   `ottplay-arc-controller`. Keep KVM and release at zero until their gates pass.
4. Render the exact pinned charts with `helm template` first and review RBAC,
   pod security and image references. These templates use ordinary shell jobs:
   `containerMode` is deliberately absent. Do not enable container jobs, service
   containers or Docker actions without a separate executor design.

ARC requires the container name `runner`; our command seeds its writable HOME
from the immutable runner distribution and executes `run.sh` with ARC's JIT
configuration. It must not call `config.sh` or maintain registration itself.
Each ARC runner handles one job and its pod/emptyDirs are discarded. Collect
controller/listener/runner logs outside these pods with normal secret redaction.

Custom images/PodSpecs are the operator's responsibility. ARC's Kubernetes
container hooks create additional pods and have different RBAC requirements;
allowing shell jobs there can expose that API authority. Hook extensions cannot
override a job container's image/name via `$job`. Docker-in-Docker, including
the documented rootless variant, requires privileged mode. Neither mode is
configured here. [ARC configuration and restrictions](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets).

## KVM admission

KVM stays disabled until the worker host actually exposes hardware virtualization.
Loading a guest module alone cannot supply a missing capability. Enable and verify
nested virtualization, if required, through a separate reviewed infrastructure
change. Require a real character device, then a device plugin that advertises
`devic.es/kvm` and injects `/dev/kvm` with device-cgroup permission. A hostPath
alone is insufficient. Do not mount the host Docker/containerd socket, all of
`/dev`, or make a runner privileged.

One compatible plugin interface is the [generic device plugin](https://github.com/squat/generic-device-plugin),
configured for only `{"name":"kvm","groups":[{"paths":[{"path":"/dev/kvm"}]}]}`
with the default `devic.es` domain and one allocation. Its pinned image,
kubelet-socket access and node installation require their own review; no plugin
DaemonSet or privileged bootstrap is included here. Verify the injected device's
GID and set `supplementalGroups` to the actual device group. A template value
is not evidence that the device exists or is accessible.

After allocation, verify `KVM_GET_API_VERSION` and the installed Android
emulator's `-accel-check` under UID 1001 and RuntimeDefault seccomp. Only then
label the verified node `ottplay.dev/kvm-admitted=true`, enable maximum one
runner, and run the phone/TV emulator smoke. Never substitute software emulation
or skip emulator assertions to make admission green.

## Smoke, activation and rollback

Run the local configuration tests with the pinned chart archives in
`ARC_CHART_DIR`. The optional Docker regression reproduces a root-owned,
fsGroup-writable mount under UID 1001 and verifies runner startup, executable
bits and symlinks without registration or cluster access:

```sh
ARC_CHART_DIR=/tmp \
ARC_TEST_DOCKER_IMAGE=python@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 \
uv run --with pyyaml python -m unittest tests/test_arc_ottplay.py -v
```

This tests the startup permission contract; it does not qualify the built worker
image. Record the result against the exact image, chart and source revision.

`deploy/arc-ottplay/smoke.sh linux|kvm|release` checks tools, non-root execution,
writable job directories, absence of host sockets/API tokens and public egress;
KVM also performs the ioctl and emulator acceleration checks. Run it in the
`runner-smoke.yml` workflow at the operator-reviewed current main SHA with no signing secrets.
The workflow additionally exercises setup actions, Chromium rendering and
emulator acceleration as appropriate. It does not replace the phone/TV boot
and instrumentation matrix required to qualify a release. Confirm the actual pool/job identity and fresh
pod UID; cancel/re-run a scratch job and verify its workspace and processes do
not survive. Check that job pods cannot reach the cluster API/private services.

A scale-to-zero pool may have no online listener runner in GitHub's runner list.
Activation therefore requires a successful current-main smoke for each required
pool, no older than 24 hours, plus the group policy checks. Its head must match
both current main and the operator's explicit `--expected-sha` (full 40-character
reviewed commit SHA); the CLI rechecks these before applying. The operator CLI may
then set `CI_RUNNER_MODE=k3s` after explicit activation approval. A consumer that
requires emulator and signing jobs must qualify all three pools. KVM absence
blocks any consumer whose required jobs need hardware acceleration.

Keep concrete CLI repository selections in the operator's access-controlled
runbook. Inspect the plan without `--apply`, verify the current reviewed SHA and
fresh smoke results, then add `--apply` only for an explicitly approved switch.
The target must be present in the CLI's reviewed consumer configuration; a public
example must not imply that an arbitrary repository name is already supported.

The operator's separate credential needs repository Metadata/Actions read,
Variables write, and organization Self-hosted runners read to verify the groups.
Registration App credentials must not be used for the operator CLI. The CLI
does not read billing, dispatch builds or change budgets. Use Python 3.10+ and
an authenticated `gh` client; API requests explicitly target github.com.

The CLI uses best-effort rechecks, not an atomic GitHub compare-and-swap. An
unset repository variable can inherit an organization value; inspect the
reported state and do not equate an unknown value with `github`. Existing queued
or running jobs are not migrated: cancel an obsolete queued run if appropriate
and dispatch a **new** run on the intended ref after switching. A rerun has no
documented runner override and is not the recovery mechanism.

Automatic quota routing is deliberately absent: billing usage is delayed and
is not an atomic remaining-minutes signal. Any future quota monitor must run
outside GitHub-hosted Actions and must preserve the same admission checks.

To stop routing, restore `CI_RUNNER_MODE=github`, let running jobs finish, then
drain the affected scale set with minimum/maximum zero. Do not delete active
pods. macOS/Windows jobs and public FOSS are outside this configuration. Shared
kernel isolation is not a VM security boundary; keep these pools private and
do not add deployment/cloud credentials or signing material to normal CI.
