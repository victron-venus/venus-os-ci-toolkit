# Private ephemeral runners

Private repositories select their execution environment explicitly. There is no
billing probe, automatic paid fallback or hosted bootstrap job. The default fleet
setting is `CI_RUNNER_MODE=self-hosted`; set it to `github` to use GitHub-hosted
minutes. Public repositories keep their independent GitHub-hosted setting.

```mermaid
sequenceDiagram
    participant W as Repository workflow
    participant G as GitHub queue
    participant A as ARC listener/controller
    participant P as Disposable runner pod
    W->>G: Job targets repository/profile scale set
    G->>A: Queued job (outbound long polling)
    A->>P: Create pod and JIT registration
    P->>G: Claim one job
    P->>G: Logs, checks and explicit artifact uploads
    P->>P: Finish action post steps
    A->>P: Delete completed pod
    Note over A,P: minRunners=0; no idle worker remains
```

## Routing contract

`CI_RUNNER_MODE` accepts `github` or `self-hosted` (`k3s` is a compatibility alias
where supported by the shared selector). Each repository gets distinct JSON
label variables: `CI_RUNNER_LABELS`, `CI_RUNNER_AUTOMATION_LABELS`, optional
`CI_RUNNER_ARM64_LABELS`; `CI_RUNNER_RELEASE_LABELS`
selects its reviewed release/deployment profile. Labels are deterministic hashes
of the complete repository name and the profile, preventing accidental sharing
between owners. Reusable workflow definitions are generated into each private
consumer repository to avoid cross-owner self-hosted runner resolution limits;
the toolkit remains the single maintained source.

Switch to GitHub-hosted without changing labels:

```sh
gh variable set CI_RUNNER_MODE --repo OWNER/PRIVATE_REPOSITORY --body github
```

Switch back to the installed, qualified ephemeral pool:

```sh
gh variable set CI_RUNNER_MODE --repo OWNER/PRIVATE_REPOSITORY --body self-hosted
```

This changes subsequent job evaluation; it does not migrate an already-running
job. Cancel/re-run explicitly when necessary. A deployment that requires a
private LAN endpoint cannot run on GitHub-hosted without separate secure network
access. Do not silently map ARM, Windows, macOS, KVM or LAN work to generic x64 CI.

## Profiles and storage

- **ci:** gVisor, Docker inside the sandbox, no private-network access.
- **automation:** separate gVisor workers for trusted metadata/bot work; Docker
  remains available for workflows such as Renovate.
- **release:** separate gVisor workers for privileged release work, with no LAN
  access. Release jobs requiring a reviewed internal endpoint use the LAN profile.
- **lan:** a native unprivileged runner with only reviewed per-repository `/32`
  TCP destinations added to its egress policy. No Kubernetes token is mounted.
- **arm64:** native ARM64 runner with disposable Docker sidecar; privileged Docker
  is confined to this explicitly trusted architecture profile. It has no host
  socket, host path, persistent registration or Kubernetes service account token.
  Its Docker daemon refuses unbounded cgroups and keeps child containers below
  the pod (or a stricter container scope); the retained 1200-byte MTU accommodates
  the private overlay. Qualify image pulls and resource limits on the native node.

One pod runs one GitHub job, including action post steps. Cross-job state travels
through explicit artifacts or caches, not a shared workspace/PVC. Each pool has
`minRunners: 0`, `maxRunners: 1`; namespace quotas bound aggregate demand. Jobs may
queue when node capacity is exhausted. A four-hour pod deadline bounds stranded
workers; workflows should also set suitable job timeouts.

Large build suites can declare `ci_resource_class: "heavy"` in the private
inventory. This reserves 13 GiB and limits the worker to 16 GiB, covering a former
large runner plus Docker sidecar in one sandbox. The shared CI namespace permits
one heavy worker or two standard workers, never a mixture while the heavy worker
is present. Node scheduling must still have the requested memory and CPU free;
the class does not guarantee performance or bypass capacity checks.

## Provisioning

Keep the real private inventory and generated values outside Git. The example
`deploy/arc-private/inventory.example.json` contains fictitious names only. The
renderer requires explicit private visibility and digest-pinned worker images:

Build the private sandbox image from the repository root. The derivative keeps
the installed guard CA and toolchains, adds the document/test utilities, and
bakes the current checked-in bootstrap. Older bases that copied the runner at
startup are normalized at build time. A historical image tag is not evidence
that its baked bootstrap matches the current source.

```sh
docker build --file deploy/arc-private/Dockerfile \
  --build-arg BASE_IMAGE=REGISTRY/BASE@sha256:DIGEST \
  --tag REGISTRY/PRIVATE_SANDBOX:VERSION .
```

Preload the resulting digest on every eligible sandbox node, then run the
sandbox qualification probe before installing pools; sandbox workers use
`imagePullPolicy: Never`.

```sh
python3 deploy/arc-private/render.py \
  --inventory /private/operator/inventory.json \
  --sandbox-image REGISTRY/SANDBOX@sha256:DIGEST \
  --native-image REGISTRY/NATIVE@sha256:DIGEST \
  --node-ip PRIVATE_NODE_IP \
  --output /private/operator/rendered
```

The output includes foundations, one ARC Helm values document per pool, an index,
and the exact repository variable values. Rendering performs no deployment.
Install foundations first, then Helm `gha-runner-scale-set` 0.15.0 with the existing
`runner-reserve-system/victron-reserve-controller` service account. Install the
updated node guard before creating private gVisor jobs: it explicitly recognizes
`arc-private-ci`, `arc-private-automation` and `arc-private-release` while retaining the public reserve
fence and excluding native private profiles.

Create a dedicated GitHub App with repository **Administration: write** and
**Metadata: read**, installed only on the private consumer repositories. In each
used namespace the controller reads `private-runner-app-OWNER`, containing
`github_app_id`, `github_app_installation_id` and `github_app_private_key`. App
credentials are never mounted into worker pods. All pools use repository-scoped
GitHub configuration URLs, including repositories owned by organizations.

Keep App secrets out of command arguments, shell history, rendered values, logs
and Git. Use a secure file/stdin secret provisioning path. Do not inject the
operator's PAT into worker images. App installation and the first real workflow
run are required before claiming the pool is operational.

## Verification and retirement

Before retiring a static runner, verify a real job starts from zero worker pods,
uploads an artifact, runs post steps and returns to zero. Exercise a failing job,
cancellation and timeout as well. Verify ARM architecture and Docker separately;
verify only the approved LAN endpoint is reachable. Confirm the runner pod cannot
read the App secret or a Kubernetes token.

Then remove the old GitHub registration, delete its Deployment/bootstrap secret,
and commit retirement to the owning deployment and self-healing repositories.
An admission guard must reject recreation of retired runner pods/controllers;
scaling to zero alone is insufficient. Preserve unrelated application workloads,
the Codex bridge and recovery data. Never reuse a persistent runner registration
for an ephemeral pool.

ARC controller/listener processes remain running to receive work; only workers
scale to zero. Artifacts must upload before job termination, and unexpected node
failure can lose files that have not yet uploaded. Retain controller/listener
logs externally. Periodic automation schedules still create work and therefore
may wake temporary runners even with no code changes.

References: [ARC concepts](https://docs.github.com/en/actions/concepts/runners/actions-runner-controller),
[scale-set deployment](https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/deploy-runner-scale-sets),
[reusable workflow access](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).
