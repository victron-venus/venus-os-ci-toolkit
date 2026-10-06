# Optional self-hosted routing

GitHub-hosted `ubuntu-latest` remains the default. No new runner, bootstrap job,
secret or Actions variable is created by the toolkit. The existing reusable
workflows evaluate routing directly when GitHub dispatches each job, including
the nested change-scope classifier and coverage uploader.

## Configuration

Configure Actions variables in the **calling repository or its organization**:

- `CI_RUNNER_MODE`: empty or `github` keeps hosted runners. Explicit
  `self-hosted` enables the configured pools; `k3s` is a compatibility alias.
  Other values do not enable self-hosted routing.
- `CI_RUNNER_LABELS`: JSON array selecting an isolated ephemeral Linux/x64 CI
  pool. For example `["self-hosted","linux","x64","your-ci-label"]`.
- `CI_RUNNER_AUTOMATION_LABELS`: JSON array selecting a separate trusted pool
  for approval, merging, CodeRabbit metadata requests and Renovate's bot tokens.
- `CI_RUNNER_RELEASE_LABELS`: JSON array selecting a separate trusted pool for
  release orchestration, publication and verified vendor delivery.

The names above are routing configuration, not runner registrations. Use labels
that actually exist and are available to the caller. ARC scale sets use their
single scale-set name, not a guessed array of generic labels. Missing labels in
an enabled profile produce an empty `runs-on` selection and fail admission;
malformed JSON also fails. Neither case guesses a private pool or silently sends
the job back to GitHub. Existing explicit `runner`/`runner-labels` inputs retain
their behavior for private callers and override shared routing when provided.

Set organization variables for the selected projects to switch centrally;
repository variables can override them. Organization variables do not configure
personal-account repositories such as `4alvit/*`. Runner visibility and group
permissions still apply in the caller's context. Sharing a workflow across owners
does not share the toolkit organization's machines. GitHub documents self-hosted
reuse for workflows owned by the same user/organization as the caller; cross-owner
callers such as `4alvit/*` calling this organization require separate live
qualification and must not be advertised as ready from configuration alone.

## Event and privilege boundaries

Shared CI routing accepts same-repository `pull_request` and default-branch
`push`, `schedule` and `workflow_dispatch`. Fork PRs, merge queues (which can
include fork changes), non-default branch dispatches and unknown events stay on
GitHub-hosted runners. Explicit inputs cannot bypass that boundary for public
callers. The metadata-only profile additionally permits same-repository
`pull_request_target` and `pull_request_review`; these workflows never check out
or execute PR code. Release routing accepts only default-branch events.

Use ephemeral jobs with fresh workspaces, no host/container runtime sockets, no
production network access and no shared signing/automation credentials in the CI
pool. Labels are selectors, not authorization: enforce selected-repository and,
where appropriate, pinned-workflow access through GitHub runner groups. Do not
reuse a runner attached to a private infrastructure repository for public code.
Promotion still enforces the existing source, toolchain and artifact checks; a
runner change cannot waive release evidence or replace RC bytes.

## One-time adoption

Consumers pin reusable workflows to reviewed commit SHAs. Merge the toolkit
change and adopt the new pins through the existing grouped Renovate updates.
For existing generated gates and repository-owned Linux/x64 validation/build
adapters, use the same installer without upgrading the release engine:

```bash
python3 -m pip install --require-hashes --only-binary=:all: -r .github/requirements-generator.txt
python3 scripts/install_release.py /path/to/consumer --runners-only
python3 scripts/install_release.py /path/to/consumer --runners-only --check
```

This mode follows local workflow calls from the policy's validators, Quality gate
and release workflows. It changes only literal `ubuntu-latest` and previously
managed toolkit runner expressions, preserving test commands, permissions,
thresholds, versions, locks and release scripts. It refuses recursive calls,
ambiguous aliases and helpers shared across conflicting CI/release profiles.
Its check covers runner adaptation only, not full release-toolkit freshness.

Other Ubuntu versions, custom/matrix selectors, native ARM64, Windows and macOS
retain their current routing. Review these explicitly before claiming that a
whole project can run during a hosted-runner outage. This switch does not move a
job already queued; after configuration changes start a new run and verify its
actual runner identity. Roll back with `CI_RUNNER_MODE=github`.

Before activating, verify runner/group access and a real trusted smoke run for
each required profile, its toolchains (Python/Node/Go/Java, Docker where used),
artifact access and network isolation. Linux alone is insufficient: for example
the Python adapter requires its existing apt/sudo and DBus build dependencies.
An offline or missing pool will leave jobs queued. Keep GitHub mode until this
admission is complete; do not change group visibility as an automatic fallback.

Self-hosted runners still depend on GitHub Actions orchestration, authentication,
checkout and artifact services. They cover hosted compute outages, not a complete
GitHub outage. No automatic failover is enabled.

## Toolkit checks

`scripts/runner_selection.py --check` verifies the shared declarations.
`tools/runner-expressions/test.mjs` evaluates dispatch using the pinned official
`@actions/expressions` parser/evaluator, including defaults, profile separation,
fork exclusion, malformed configuration and private-input compatibility.
The existing CI contract job installs its locked test dependency and runs it.

References: [reusable workflow runners and variables](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations),
[runner group access](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access),
[self-hosted runner security](https://docs.github.com/en/actions/reference/security/secure-use).

## Public reserve preparation

The public reserve inventory is `deploy/runner-reserve/repositories.json`.
It is separate from private runner groups and the Renovate repository inventory.
Organization variables must have selected-repository visibility matching that
inventory; personal-account repositories need their own variables. Prepare all
four variables with `CI_RUNNER_MODE=github`. The label values alone do not prove
that a corresponding pool exists or has passed qualification.

For the one-time adoption across existing consumer workflows, including projects
without a single-entry release policy:

```bash
python3 scripts/prepare_runner_reserve.py /path/to/consumer --toolkit-ref REVIEWED_40_CHARACTER_SHA
python3 scripts/prepare_runner_reserve.py /path/to/consumer --toolkit-ref REVIEWED_40_CHARACTER_SHA --check
```

This uses the existing runner-expression generator, updates shared workflow pins
and keeps a coverage policy's pin consistent. It preserves the release engine
revision, platform matrices, fixed Ubuntu images, hardware/private selections,
commands, permissions and event filters. Review platform exceptions and qualify
the actual workflow graph before activation. No mode or runner registration is
changed by this command.

OpenSSF Scorecard publication also stays on its required literal `ubuntu-latest`
runner. Its publishing service validates the workflow's hosted-runner identity;
the reserve must not weaken that contract. Local scan/completeness jobs can use
the CI pool, but public Scorecard publication remains a hosted-service dependency.

The GitHub App specifications in `deploy/runner-reserve/github-apps.json` keep
organization runner administration separate from the repository administration
permission GitHub requires for personal repository runners. Install only for the
listed public projects. Do not copy a developer PAT or `BOT_PAT` into the runner
controller, worker image, workspace or source repository. Private keys belong in
controller-only secrets; workers receive ephemeral registration material.

Prepared variable values, a merged adoption PR, or downloaded ARC/gVisor versions
are not a readiness certificate. Readiness requires registered pools, admission
and network isolation, toolchain/service-container tests, same-owner and
cross-owner reusable calls, and a successful job followed by worker destruction.
Keep normal routing on `github` until all applicable checks are evidenced.

### Linux/x64 sandbox deployment

The files under `deploy/runner-reserve/` prepare the public reserve independently
of normal GitHub routing. `render_pool.py` reads the same public inventory and
renders three organization scale sets and three per personal repository (51 for
the current inventory). Each set has `minRunners: 0`, `maxRunners: 1`: standby can
accept selected jobs without another Helm change. Namespace quotas bound the
combined worker population to two jobs per profile, including gVisor overhead.
This is reduced emergency capacity, not a replacement for GitHub's fleet size.

The node runtime uses the pinned gVisor release with `systrap`, which works in a
nested VM without KVM. `containerd-config.toml.tmpl` extends K3s's base template;
never replace an existing custom template without integrating its settings.
`runsc.toml`, the shim and `RuntimeClass` must agree on their installed paths and
handler. Runtime configuration needs an approved node restart; it must not be
applied blindly to a control-plane server. Private runners retain their runtime.

`node_guard.py` is a node service, not code supplied to a job. Install the pinned
CRI client at `/opt/victron-ci-reserve/crictl`; `versions.json` records its checksum.
Its root-owned configuration supplies `node_ip`, `dns_ip` and `extra_denied`
(including any public address of local infrastructure). The CNI cache contract is
specific to this K3s/flannel deployment. It binds sandbox identity to the actual
host veth and MAC before atomically installing an nftables bridge fence. Only
reserved worker namespaces/labels are affected. Private, metadata, node and
cluster destinations are blocked; public TCP 80/443 and cluster DNS are allowed.
A bounded read-only HTTPS endpoint admits only the source pod's exact UID.
The worker verifies its certificate against the public CA bundled in its image;
no insecure TLS bypass is permitted. A focused regression test rejects plaintext,
untrusted certificates and hostname mismatches. The single S5332 suppression at
`serve_forever` accounts for older Sonar analyzers that classify the inherited
server loop as plaintext without following its mandatory TLS wrapper (see the
[upstream check and its documented limitation](https://github.com/SonarSource/sonar-python/blob/master/python-checks/src/main/java/org/sonar/python/checks/hotspots/ClearTextProtocolsCheck.java)).
If discovery or the firewall transaction fails, new workers cannot register.
NetworkPolicy provides another boundary; it does not replace the node fence.

Generate the node guard TLS key and certificate on the node, with the node IP in
the certificate SAN. Keep the private key root-readable on that node; configure
`tls_private_key` and `tls_certificate` in the node service configuration. Copy
only the public certificate to the build context as `guard-ca.crt` (Git-ignored).
Rebuild/requalify the worker image before certificate expiry or trust rotation.

Build `Dockerfile` with BuildKit from this directory, preserving every base-image
digest. Preload the resulting image into K3s containerd on the selected node and
use its immutable **manifest digest**, not the Docker configuration/image ID.
Workers use `imagePullPolicy: Never` so a standby launch does not need a registry
credential or a new image download during an outage. Keep a recoverable image
archive and requalify after node replacement or cache eviction.

```bash
python3 deploy/runner-reserve/render_pool.py \
  --worker-image 'localhost/victron-reserve-worker@sha256:QUALIFIED_MANIFEST_DIGEST' \
  --node-ip NODE_ADDRESS_COVERED_BY_GUARD_CERTIFICATE \
  --output /path/to/new-reserve-plan
```

The renderer validates the inventory and digest format, creates a new output
directory, and never installs resources or changes Actions variables. Its
`foundations.json` contains namespaces, no-permission worker service accounts,
quotas, network policies and a fail-closed Kubernetes admission policy. Admission
requires the pinned worker image, gVisor, the fixed bootstrap, no host namespaces,
no host/persistent/credential volumes, no API token and no privileged host
container. The image defaults to UID 1001; only the enforced gVisor pod specification
requests root for bootstrap. Root and Docker capabilities are emulated **inside Sentry**. Each job
gets a fresh container filesystem, including Docker storage and the tool cache.
The bootstrap refuses an ordinary Linux runtime and waits for node admission
before starting Docker or the JIT-configured runner. It never mounts a host socket.

Validate foundations on the server, then install them before any scale set. Put
the separately scoped GitHub App secrets in each worker namespace using the
names emitted in the values files; only the ARC control plane may read them.
Workers have no Kubernetes API credentials or network route to the API. Render
and validate the pinned scale-set Helm chart with the corresponding values file
from `index.json`, and install each release in the namespace specified there.
Keep `CI_RUNNER_MODE=github` throughout preparation and manual qualification.

`RUNNER_RESERVE_QUALIFICATION=1` selects the fixed manual probe after the same
TLS admission and Docker bootstrap; it never registers a GitHub runner. It
checks sandbox/toolchain prerequisites, public HTTPS, a nested BuildKit build
and a published PostgreSQL service. Never set it in normal scale-set values.

A local Docker or networking probe is insufficient to certify a pool. Record a
real GitHub JIT worker for each profile/owner, a cross-owner reusable call,
Python/Node/Go setup, a Docker build, a published PostgreSQL service, artifact
upload/download, blocked private endpoints and destruction of the worker after
completion. No GitHub registration or successful smoke job means **not ready**.
ARM64, fixed Ubuntu/platform matrices, macOS, Windows, hardware jobs and Scorecard
publication remain explicit exceptions from the one-time adoption described
above. A hosted-compute outage and a GitHub control-plane outage are different;
ARC still requires GitHub's orchestration and artifact services.
