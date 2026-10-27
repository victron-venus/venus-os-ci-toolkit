# Optional self-hosted routing

For reinstall, backup and another organization, use the [ARC recovery index](ARC_RECOVERY.md).

For pinned branded browsers, Playwright OS dependencies and architecture limits,
see [runner browser prerequisites](RUNNER_BROWSERS.md).

For private consumers spanning multiple owners, use the
[caller-local workflow distribution](WORKFLOW_DISTRIBUTION.md). It preserves
immutable shared sources and the explicit `github` / `self-hosted` switch while
keeping every reusable job definition inside the caller repository.

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

For GitHub-hosted Linux, the Python and browser workflows prepare the
image-provisioned Ubuntu mirror list before installing system packages. When
the list contains the recognized Azure primary and HTTPS Ubuntu fallback,
they remove only the Azure entry and retain the existing fallback. A missing
or unfamiliar list is left unchanged with a diagnostic; self-hosted runners
are not modified. Package requirements, signature verification, test commands
and job timeouts remain unchanged. Consumers pinned to an older workflow SHA
must explicitly adopt a commit containing this preparation step.

Self-hosted runners still depend on GitHub Actions orchestration, authentication,
checkout and artifact services. They cover hosted compute outages, not a complete
GitHub outage. No automatic failover is enabled.

## Toolkit checks

### Inspect ARC snapshots without changing routing

`scripts/arc_readiness.py` evaluates sanitized inventory files with Python's
standard library. It makes no API requests, changes no variables or pools, and
starts no jobs. Keep inventories and reports containing private repository names
outside public source trees.

The two paths are chosen by the local operator; this is not an HTTP endpoint or
an LLM tool with delegated filesystem access. Parent directories must be controlled
by that operator. On Linux/macOS the reader opens each final file without following
symlinks, rejects nonregular files (including FIFOs/devices) without blocking, and
limits reads to 8 MB. It emits selected inventory fields, never raw file contents.

```sh
python3 scripts/arc_readiness.py --repositories repositories.json --cluster cluster.json
```

The repository input declares the expected profiles rather than guessing them
from existing workers. Use effective Actions variables after repository overrides:

```json
{"schema":1,"observed_at":"2026-01-01T00:00:00Z","complete":true,
 "repositories":[{"repository":"example/project","mode":"self-hosted",
   "profiles":{"ci":["example-ci"],"automation":[]},"queued_jobs":{"ci":2}}]}
```

Each profile is an exact label array; an empty array means missing configuration
and `null` means unknown. An empty profile object is listed under
`profiles_not_declared`, without inventing requirements. `queued_jobs` is optional and must describe the same
profile; absent demand remains unknown. A hosted/default mode is reported without
enabling the declared reserve. This checks declared selectors, not every caller's
workflow expression, event eligibility or explicit runner override.

The cluster input has `schema: 1`, a timezone-bearing `observed_at`, and
`collections.asrs`, `.listeners` and `.pods`. Each collection contains sanitized
Kubernetes `items`, `complete`, `source_metadata` (including any unconsumed
`continue` token) and `scope: "cluster-all"`. For filtered Pod collections use
`namespace_prefixes`, for example `["arc-", "runner-reserve"]`, instead of claiming
cluster-wide coverage. Collect every page before marking a collection complete.
Omitted collections, partial pages and namespaces outside the declared coverage
cannot establish absence or zero workers.

Preserve object metadata names, namespaces, UIDs, generations, linking labels,
owner references and deletion timestamps. ASRS objects need `githubConfigUrl`,
`runnerScaleSetName`, optional `runnerScaleSetLabels`, `minRunners`, `maxRunners`
and status `observedGeneration`. Listeners need scope and `runnerScaleSetId`.
Pods need phase, readiness conditions and container readiness. Listener capacity
must also be captured: differing explicit limits mean pending reconciliation;
a missing maximum cannot establish available capacity. Do not include
Secret objects, Pod environment values or JIT credentials. Preserve absent,
explicit `null` and zero fields separately; never normalize them with a default
that fabricates a hold or an empty worker population.

The report separates matching ASRS configuration, observed scale-set registration
IDs, configured admission, listener Pod readiness and worker Pod counts. A listener
is joined by both scale-set name and namespace; its Pod must have the matching
owner UID. An organization scope remains `organization-access-unverified` until
runner-group access is checked separately. Zero GitHub runner registrations alone
are not evidence that an ARC pool is missing. A complete Pod census with no workers,
current controller generation, a uniquely matched ready listener, matching
listener capacity and an enabled zero-minimum pool is
reported as `idle-scale-to-zero`. A configured `maxRunners: 0` is `held`, or
`held-with-queued-demand` when demand is supplied; existing workers may still drain.
Unset limits, missing registration evidence and incomplete snapshots remain unknown.
Positive observations from partial collections are retained, but cannot establish
uniqueness or readiness when additional matching objects may have been omitted.

Freshness defaults to 15 minutes (`--max-age-seconds` accepts 1–86400). Stale or
future snapshots retain their historical observations but set `evidence_fresh`
false. The command's zero exit status means valid input was evaluated, not that
CI is ready. Every report says `qualification: "not-assessed"`: registration,
listener health and idle capacity cannot prove toolchain compatibility, isolation,
job execution or cleanup. Those still require the trusted smoke checks above.

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
The catalog contains 77 repositories: 18 personal repositories and selected
projects in three organizations. It renders 63 scope/profile registrations across
separate CI, automation and release profiles. Catalog membership does not prove
that any corresponding pool is registered or qualified.
The retired `4alvit/default` HACS submission fork is excluded; the DESLOC
integration and card remain covered through the `ha-homelab` organization.
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
and keeps coverage and fuzzing policy pins consistent. Policy JSON retains its
original whitespace, compact arrays, key order and line endings; only the two
owned pin values can change. Duplicate policy keys are rejected, and the complete
decoded result must match the intended policy before any files are written.
A configured fuzz workflow,
its helper checkout input and its policy must already agree; all three are updated
together or adoption refuses the edit set. It preserves the release engine
revision. A `vendor-update.yml` call and its required `toolkit-revision` input
must also agree on an immutable SHA; both advance together so delivery executes
the helper belonging to its reviewed workflow. Missing or inconsistent inputs
stop adoption before any files are written. The command preserves
platform matrices, fixed Ubuntu images, hardware/private selections,
commands, permissions and event filters. Review platform exceptions and qualify
the actual workflow graph before activation. No mode or runner registration is
changed by this command.

Migration only adapts literal `ubuntu-latest` and the generator's exact canonical
expressions. Fixed platform labels and custom expressions retain their selection.
Before moving a local job, the generator requires review when permissions are
unknown or writable, credentials are referenced, or publication/deployment
indicators are present. A job-level permission map replaces the workflow map.
These checks identify reasons for review; they cannot prove arbitrary scripts
safe. Inspect the job's commands and credentials before assigning its role.

Record reviewed exceptions on the existing command, repeating `--job-profile`:

```sh
python3 scripts/prepare_runner_reserve.py /path/to/consumer --toolkit-ref REVIEWED_40_CHARACTER_SHA \
  --job-profile ci.yml:publish=release \
  --job-profile scorecards.yml:analysis=hosted \
  --job-profile auto-approve.yml:approve=preserve
```

Roles are `ci`, `automation`, `release`, `hosted` and `preserve`. `hosted` keeps a
literal hosted selector or restores an exact canonical expression to its known
`ubuntu-latest` fallback. `preserve` acknowledges a reviewed job while keeping its
selector unchanged; it makes no claim about where that selector runs. Unknown
workflows/jobs, repeated overrides and overrides of reusable-call jobs fail.
An override for an existing fixed-platform job may deliberately make no change.
Use the same arguments with `--check` to verify the result.

Review short-lived scan/upload credentials before allowing them in the CI pool.
Long-lived deployment credentials or `BOT_PAT` belong only to their reviewed
release or metadata automation job, never to a candidate-code CI job. Privileged
`workflow_run` jobs require `hosted` or an independently reviewed `preserve`
exception; migration does not expand the shared event trust boundary.

Shared RC fuzzing also uses the CI profile, because it builds and executes the
candidate's fuzz targets. Its public-repository, default-branch RC-dispatch guard
and read-only permissions remain unchanged; it does not use the release pool.

OpenSSF Scorecard publication also stays on its required literal `ubuntu-latest`
runner. Its publishing service validates the workflow's hosted-runner identity;
the reserve must not weaken that contract. Local scan/completeness jobs can use
the CI pool, but public Scorecard publication remains a hosted-service dependency.

The GitHub App specifications in `deploy/runner-reserve/github-apps.json` map
`organizations.victron-venus`, `organizations.ha-homelab` and
`organizations.open-ott-play` to three separately owned private Apps with distinct
organization-prefixed names. Create each under its matching organization, using
only that entry as the App manifest. Each requests only organization self-hosted
runner administration, with no repository permissions, events or webhook. Limit
each organization's runner groups to its listed public projects; an App installation
alone does not impose that runner-group restriction.

The separate `personal` App retains only the repository administration permission
GitHub requires for personal repository runners. Install it under `4alvit` with
**Only select repositories**, selecting the 18 personal public repositories in
`repositories.json`, not all repositories. App names must be globally available
when created and no longer than 34 characters; record any naming adjustment with
the corresponding owner entry. See [GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app).
These source specifications do not create Apps or grant access.

Do not copy a developer PAT or `BOT_PAT` into the runner
controller, worker image, workspace or source repository. Private keys belong in
controller-only secrets; workers receive ephemeral registration material.
The catalog's `organization_secrets` binds each organization to a distinct
namespace-local Secret reference and installation identity. Do not reuse private
runner credentials for these public pools. Keep `CI_RUNNER_MODE=github` during
registration and qualification.

Prepared variable values, a merged adoption PR, or downloaded ARC/gVisor versions
are not a readiness certificate. Readiness requires registered pools, admission
and network isolation, toolchain/service-container tests, same-owner and
cross-owner reusable calls, and a successful job followed by worker destruction.
Keep normal routing on `github` until all applicable checks are evidenced.

### Linux/x64 sandbox deployment

The files under `deploy/runner-reserve/` prepare the public reserve independently
of normal GitHub routing. `render_pool.py` reads the same public inventory and
renders three scale sets per organization and personal repository (63 across
three organizations and 18 personal repositories). Kubernetes scale-set names include the repository scope;
ARC 0.15 custom `scaleSetLabels` preserve the shared Actions-variable labels
without collisions between personal repositories in the same namespace.
Active rendering requires a fresh physical-host capacity plan with an explicit
zero/one allocation for every scope/profile. Each set has `minRunners: 0` and the
allocated `maxRunners`; namespace quotas match the per-profile allocation sums
and include gVisor overhead. There is no implicit six-worker budget.
See [the capacity-plan contract](../deploy/runner-reserve/CAPACITY-PLAN.md) for
measurement freshness, growth allowances, limits, and offline validation.

For registration-only preparation, pass `--held` to `render_pool.py`. Every
generated pool then has `minRunners: 0` and `maxRunners: 0`; this mode requires no
capacity plan and preserves its previous outputs. The flag only creates held
values files and never changes GitHub routing or deploys them. Open admission
only after separate acceptance; do not apply generated foundations blindly over
existing infrastructure. Capacity-plan acceptance is not runtime qualification
or continuous physical-disk admission control.

The node runtime uses the pinned gVisor release with `systrap`, which works in a
nested VM without KVM. `containerd-config.toml.tmpl` extends K3s's base template;
never replace an existing custom template without integrating its settings.
`runsc.toml`, the shim and `RuntimeClass` must agree on their installed paths and
handler. Changing containerd's handler configuration needs an approved node
restart; it must not be applied blindly to a control-plane server. The existing
shim reads `runsc.toml` for newly created sandboxes without a containerd restart.
This dedicated runtime enables guest `allow-suid` for package installers run by
the non-root user. Public reserve and private sandbox pods both use this setting;
guest root remains inside gVisor and the host/network admission boundaries stay
in force. Native private LAN/ARM profiles retain their separate runtime policy.

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
The guard checks local CNI state and the live veth MAC/ifindex every two seconds,
without spawning a process. A disappeared or reused interface revokes admission.
CRI discovery runs every 15 seconds; a new worker may wait that long for its
first admission. The owned nftables table is inspected every 10 seconds and
atomically replaced only when workers or filtering rules change. Rule order,
selectors, verdicts and chain attributes are compared; packet counters and kernel
handles are ignored. The expected snapshot comes from the guard's own atomic
transaction, and a separate read must match before admission. A deleted or
externally modified table is therefore repaired without trusting it as a new
baseline. Existing unrelated tables and private runners are not changed.

The root-owned JSON configuration optionally sets `identity_check_interval_seconds`
(default 2, range 1–10), `discovery_interval_seconds` (default 15, range 1–300) and
`firewall_check_interval_seconds` (default 10, range 1–60). Intervals are seconds
between completed checks, plus scheduler delays on an overloaded node. Readiness
expires after twice the identity interval plus one second if the checking thread
stalls. A healthy but slow reconciliation can also temporarily return 503;
startup retries until fresh checks succeed. This short lease deliberately
prefers delayed registration to stale admission. Already running workers do not
poll this startup endpoint. Discovery, identity, firewall-read or firewall-write
failures clear admission; recovery requires fresh CRI discovery. NetworkPolicy
provides another boundary; it does not replace the node fence.

Generate the node guard TLS key and certificate on the node, with the node IP in
the certificate SAN. Keep the private key root-readable on that node; configure
`tls_private_key` and `tls_certificate` in the node service configuration. Copy
only the public certificate to `deploy/runner-reserve/guard-ca.crt` (Git-ignored).
Rebuild/requalify the worker image before certificate expiry or trust rotation.

Build from the **repository root** with BuildKit, preserving every base-image
digest. `deploy/runner-reserve/Dockerfile.dockerignore` limits this root context
to the exact image sources, the public CA and the shared pinned GitHub CLI
manifest/installer; it excludes Git history, keys and unrelated working files.
Do not substitute a directory-wide allowlist. The image verifies the CLI archive,
binary and version, then requires `gh api --paginate --slurp` support used by
shared automation; distribution-provided `gh` is insufficient.

```bash
docker build --platform linux/amd64 --file deploy/runner-reserve/Dockerfile \
  --tag localhost/victron-reserve-worker:qualification .
```

Building alone does not qualify an image. Preload the resulting image into K3s
containerd on the selected node and
use its immutable **manifest digest**, not the Docker configuration/image ID.
Workers use `imagePullPolicy: Never` so a standby launch does not need a registry
credential or a new image download during an outage. Keep a recoverable image
archive and requalify after node replacement or cache eviction.

```bash
python3 deploy/runner-reserve/render_pool.py --held \
  --node WORKER_NODE \
  --worker-image 'localhost/victron-reserve-worker@sha256:QUALIFIED_MANIFEST_DIGEST' \
  --node-ip NODE_ADDRESS_COVERED_BY_GUARD_CERTIFICATE \
  --output /path/to/new-reserve-plan
```

Before deploying, verify that `--node` matches the installed `victron-reserve`
RuntimeClass's `scheduling.nodeSelector` (the supplied `runtime-class.yaml` names
`mp` and `amd64`). The renderer does not rewrite or inspect that RuntimeClass.
Verify the final manifest is `linux/amd64`, matching that RuntimeClass; do not
use the build host's native architecture implicitly. A different worker node
requires separately reviewed runtime installation,
matching RuntimeClass scheduling, guard certificate/address and preloaded image;
a listener placement change does not move any of these worker prerequisites.
Keep `--held` and `CI_RUNNER_MODE=github` while preparing and qualifying them.

#### Distribute listeners separately from workers

Each scale set needs a listener Pod in `runner-reserve-system`, even while its
workers are held at zero. The 63-set inventory therefore needs 63 listener Pod
slots, with aggregate requests of 630m CPU and 2520Mi memory. Worker namespace
quotas do not include these listeners. Account for both populations and reserve
headroom before selecting nodes; free memory alone does not imply free Pod slots.

By default listeners use the worker's `--node`. To distribute them, provide
`--listener-placement /path/to/listener-placement.json`. This changes only
`listenerTemplate.spec.nodeSelector.kubernetes.io/hostname`. Worker placement,
guard address, image, runtime, security settings and `--held` limits are preserved.
No-map output remains unchanged.

The JSON format is `{"schema": 1, "placements": {"SCOPE": {"PROFILE": "NODE"}}}`.
It must include every current scope and all three profiles (`ci`, `automation`,
`release`); there is no partial-map fallback. A scope is an organization name or
the full name of a personal repository from `repositories.json`. Duplicate,
unknown or missing entries and invalid node names fail before output is created.

From the repository root, generate a complete starting map into a **new file**:

```bash
python3 - <<'PY'
import json
from pathlib import Path
catalog = json.loads(Path('deploy/runner-reserve/repositories.json').read_text())
scopes = [*catalog['organizations'], *[
    repo['name'] for repo in catalog['repositories']
    if repo['name'].split('/')[0] not in catalog['organizations']]]
plan = {'schema': 1, 'placements': {
    scope: {profile: 'choose-listener-node' for profile in catalog['profiles']}
    for scope in scopes}}
with open('listener-placement.json', 'x') as output:
    output.write(json.dumps(plan, indent=2) + '\n')
PY
```

Replace each placeholder with a reviewed node hostname, then pass the file to
the existing render command along with `--held` during registration preparation.
Inspect the resulting values and current node capacity before any installation.
The renderer checks structure; it does not contact Kubernetes, verify that nodes
exist, or certify their capacity or the worker image. A placement map alone does
not make the reserve ready.

The renderer validates the inventory and digest format, creates a new output
directory, and never installs resources or changes Actions variables. Its
`foundations.json` contains namespaces, no-permission worker service accounts,
quotas, network policies and a fail-closed Kubernetes admission policy. Admission
requires the pinned worker image, gVisor, the fixed bootstrap, no host namespaces,
no host/persistent/credential volumes, no API token and no privileged host
container. The image defaults to UID 1001; only the enforced gVisor pod specification
requests root for bootstrap. Root and Docker capabilities are emulated **inside Sentry**. Each job
gets a fresh container filesystem, including Docker storage and the tool cache.
The runner payload is installed with its final ownership in the image, avoiding
a full payload copy at every cold start. Link MTU is read through netlink because
gVisor does not expose Linux sysfs network-interface files.
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
checks sandbox/toolchain prerequisites, public HTTPS, offline npm pack/install/ci,
a nested BuildKit build, Compose config/up/down using that same local image
without pulling another image, and a published PostgreSQL service. The npm
fixture is retained in the image so it runs again as the unprivileged sandbox
user. Never set the qualification flag in normal scale-set values.
After bootstrap, only this probe has a 600-second deadline and a 15-second forced
termination grace. The root timeout supervisor retains a process group across
the probe's UID drop; ordinary JIT runner jobs do not get this probe deadline.
A timeout or nonzero exit is a failed qualification, never a partial pass. This
limit does not include the earlier admission/Docker startup budgets or replace
operator cleanup: remove the owned test Pod and verify its containers, network
fence entry and other owned resources are gone even after forced termination.

A local Docker or networking probe is insufficient to certify a pool. Record a
real GitHub JIT worker for each profile/owner, a cross-owner reusable call,
Python/Node/Go setup, a Docker build, a published PostgreSQL service, artifact
upload/download, blocked private endpoints and destruction of the worker after
completion. No GitHub registration or successful smoke job means **not ready**.
ARM64, fixed Ubuntu/platform matrices, macOS, Windows, hardware jobs and Scorecard
publication remain explicit exceptions from the one-time adoption described
above. A hosted-compute outage and a GitHub control-plane outage are different;
ARC still requires GitHub's orchestration and artifact services.
