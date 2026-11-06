# Dependency updates

GitHub Actions and reusable workflows are maintained by the centrally scheduled
`Renovate dependency updates` workflow in this repository. It uses the existing
organization `BOT_PAT`, an explicit repository allowlist, and `requireConfig:
required`. Repositories opt in with `renovate.json` extending
`local>victron-venus/venus-os-ci-toolkit:renovate-fleet`.

Each repository receives one `renovate/ci-workflows` PR containing all available
CI updates. Major, minor, patch and digest updates are grouped together. CodeQL
`init`, `autobuild`, `analyze` and `upload-sarif` must use the same full commit SHA
across all workflows; the existing required CI configuration check rejects a
partial update before invoking the scanners. Renovate does not enable automatic
merging or apply an `automerge` label. The normal review and required CI gate apply.

The generator reads `.github/action-pins.json`. Renovate updates this manifest in
the same group as workflow references, and the contracts reject generated
workflows that disagree with it. Regeneration preserves version comments, so
Renovate can continue resolving pinned actions. The SHA remains the executable
reference; the comment identifies the upstream release or branch to track.

The same update group includes `.release-policy.json` toolkit digests and the
`with.toolkit-ref` input of jobs that call this toolkit. The runtime input manager
parses YAML structurally: it updates literal 40-character digests only for actual
toolkit callers, leaving other workflows and examples inside shell scripts intact.
The contract suite exercises extraction and replacement with the pinned Renovate
release, including multiple callers in one file. This keeps the invoked workflow,
its runtime source and the release policy on the same revision.

Every CodeQL `uses` line needs an inline full release comment such as
`# v4.38.3`, including separate workflow-analysis jobs and SARIF uploads. Bare
SHAs can be skipped by Renovate while annotated steps move to a new release.
The workflow contract now rejects missing or inconsistent CodeQL version
comments as well as mixed SHAs. Consumers receive this guard when their
vendored `scripts/workflow_contracts.py` is refreshed from the toolkit.

The full-fleet preset extends the original `renovate-ci` preset with native
application and container managers. Consumers explicitly list their managers
and preserve project-specific version ceilings, groups and source exclusions.
The `pep621` manager owns `pyproject.toml` and `uv.lock`. Historical browser
generations, vendored sources and copied build overlays require their existing
source-aware update procedures. Runner images, action tool-version inputs and
Python `requires-python` runtime migrations remain explicit project changes.

PEP 621 projects with lockfiles use `rangeStrategy: update-lockfile`: when a
newer dependency fits an existing range such as `>=1.0`, Renovate refreshes the lockfile without
raising that lower bound. Its default `replace` behavior would otherwise skip
these in-range updates. Out-of-range updates keep the normal replacement
behavior and remain subject to each consumer's version bounds and review.
Coupled exports run after lock-only changes as well as manifest changes.

Application updates form a separate group, with major updates separated from
minor/patch updates. Defaults permit two open ordinary PRs and one new ordinary
PR per hour per repository. Renovate reads GitHub vulnerability alerts for
security fixes, with one concurrent security PR and no dashboard approval wait.
CI security fixes remain in the same atomic `renovate/ci-workflows` branch;
application security fixes retain separate per-package branches, unless a
consumer preserves a required coupled dependency group. Both presets omit
`vulnerabilityAlerts.enabled`: alert detection is active by default, and
setting this field to `true` would force-enable dependencies that a consumer
intentionally disables. Source and ABI compatibility rules therefore also
apply to security updates; their alerts remain visible for manual remediation.
Normal review and required CI still apply; automatic merging is disabled.

Remove `.github/dependabot.yml` (or `.yaml`) after the replacement configuration
is reviewed, and disable GitHub's **Dependabot security updates** setting. Keep
the dependency graph and vulnerability alerts enabled, because Renovate reads
those alerts. Terraform must declare `enabled = false` for security-update
resources so a later apply cannot restore the old PR producer. Compatible
review logic for historical Dependabot PRs can remain.

The public runner contains only public repository names. Private consumers and
the existing `ha-homelab/home-assistant` exception belong to the private runner's
explicit inventory and visibility preflight. Each repository belongs to exactly
one runner. Repositories without dependency or workflow inputs need no updater.

## Operations

The sweep runs daily at 09:23 UTC. For a read-only check, dispatch the workflow
with `dry-run=true` (the manual default). Dispatch with `dry-run=false` for an
immediate update sweep. Only the default branch may run the privileged job.
Failures are visible in the workflow run; a missing token fails the job instead
of silently disabling updates. Dependency lifecycle scripts, plugins and shell
execution remain disabled. Post-upgrade tasks can run only the exact reviewed
generator commands enumerated in `renovate-runner.cjs`.

Some Python consumers export requirements from `uv.lock` or compile a platform
lock constrained by that export. Their native source update is followed by a
reviewed `uv export --locked --offline --no-build` or `uv pip compile --no-build`
command. Export rejects a stale source/lock pair; no command may build source
distributions. Tasks pin uv 0.13.4, name the exact files allowed into the commit,
and use `executionMode: update` on the matching source rule. Branch mode can
lose a task when another dependency leads a mixed update group in Renovate
44.215.0. Temporary constraint exports are excluded from committed artifacts.

Adding a generator requires reviewing its complete literal command, source
ownership and output files, testing a changed dependency and stale-input failure,
and extending the runner allowlist and its negative command-policy tests. A
consumer must wait for that allowlist to reach the runner default branch before
enabling its task. Unsupported source builds and local-interpreter contracts
remain explicit manual boundaries. See [post-upgrade task configuration](https://docs.renovatebot.com/configuration-options/#postupgradetasks).

`BOT_PAT` must permit repository contents and workflow writes (`repo` and
`workflow` for a classic PAT). The preflight rejects a classic token missing
`workflow`: Renovate otherwise logs a rejected workflow push while exiting
successfully. A fine-grained token must grant Contents and Workflows read/write,
as well as the other permissions required by Renovate's GitHub platform.
Tokens without an `X-OAuth-Scopes` response header produce an explicit warning:
authentication succeeded, but workflow write permissions remain unverified.
The preflight does not check fine-grained permissions for each target repository;
confirm those permissions separately before enabling write sweeps.

Commits use the dedicated SSH signing key in the repository Actions secret
`RENOVATE_SIGNING_KEY`, registered as a signing-only public key on `4alvit`.
`gitAuthor` matches that account's verified email. This key was generated for
this runner; it is not a personal authentication key. To rotate it, register the
replacement public signing key, update the secret, verify a signed Renovate PR,
then remove the old public key. Keep required-signature branch rules enabled.

Before contacting GitHub, the preflight checks the signing key's public metadata.
It accepts the [Renovate signing formats](https://docs.renovatebot.com/self-hosted-configuration/#gitprivatekey):
SSH private keys and ASCII-armored OpenPGP private keys (including Renovate's
Base64-encoded OpenPGP input). SSH keys must be Ed25519, NIST ECDSA P-256/P-384/P-521,
or RSA with at least 2048 bits. OpenPGP must contain one primary identity; every
private primary key and subkey must use RSA with at least 2048 bits, the same
NIST curves, or Ed25519/Ed448 and their Curve25519/Curve448 encryption counterparts.
At least one key must support signing. DSA, smaller keys, unknown algorithms and
malformed material fail before the dependency sweep starts. These are GnuPG-compatible
OpenPGP packets: GnuPG represents Ed25519/Ed448 with algorithm ID 22 and its curve
field. This does not claim support for RFC 9580 version-6 keys or algorithm IDs
27/28, which the Renovate runner's GnuPG import must support before they can be used. The inspector
accepts GnuPG Ed448 sizes of 448 bits and the
[legacy rounded 456-bit encoding](https://github.com/gpg/gnupg/blob/gnupg-2.4.4/common/openpgp-oid.c#L54) only with the Ed448 curve and algorithm 22; other curve/algorithm
mismatches remain rejected. Signing tests explicitly select GnuPG compatibility
when generating synthetic Ed448 fixtures on distributions that require it.

The runner needs OpenSSH, GnuPG and OpenSSL. Inspection uses temporary storage,
does not import into the operator's keyring, and removes temporary material on
success or failure. Each inspection command has a 15-second timeout and receives
no GitHub token. Key material and passphrases are never printed by the preflight.
The scheduled workflow currently supplies an unencrypted dedicated SSH key.
When invoking the preflight separately for a passphrase-protected SSH key, pass
`RENOVATE_GIT_PRIVATE_KEY_PASSPHRASE` to both the preflight and Renovate, as described
by [Renovate's passphrase option](https://docs.renovatebot.com/self-hosted-configuration/#gitprivatekeypassphrase).
OpenPGP passphrases remain unsupported by Renovate. A strength check is not a
claim of FIPS certification or proof that the remote account registered the key.

Run the offline regression suite with
`python3 -m unittest discover -s tests -p 'test_renovate_signing.py' -v`.
It generates disposable keys locally and never needs a repository secret.

To onboard another full-fleet repository:

1. Audit the default branch and choose the public or private runner. Verify bot
   access and add the repository to exactly one explicit inventory.
2. Create `renovate.json` extending `renovate-fleet`, preserving version holds,
   source exclusions and dependency groups. Explicitly enable required native
   managers alongside `github-actions`, `custom.regex` and `custom.jsonata`.
3. Review SHA-pinned workflow references and add missing version comments without
   changing their executable values. Use the repository's verified action-pin
   manifest, or verify an upstream tag resolves to that exact commit (including
   annotated-tag dereferencing). Only use `# main` for a toolkit reference after
   verifying its branch identity. Preserve existing prose after the version
   comment. Stop for unknown pins; never guess a version from neighboring steps.
4. Confirm the workflow-contract guard checks coupled CodeQL and toolkit pins.
   Validate the configuration with the pinned Renovate validator and run
   workflow contracts and actionlint for any changed workflow metadata.
5. Merge the reviewed configuration and inventory through normal repository
   gates. Run a dry sweep and verify the repository and expected managers.
6. Remove the old Dependabot YAML, disable Dependabot security updates and run
   a live sweep. Keep vulnerability alerts and project-specific source policies.

`scripts/migrate_ci_updates.py` is a historical CI-only migration helper. It
rejects full-fleet configurations and recreates the old Dependabot stub. Do not
run it during full-fleet onboarding or use it as an annotation-only command.

Do not merge old standalone Dependabot action PRs after migration. Close them
when their changes are superseded by the verified Renovate path. Preserve any
unique fixes until the replacement covers them.

References: [GitHub Actions manager](https://docs.renovatebot.com/modules/manager/github-actions/)
and [grouping options](https://docs.renovatebot.com/configuration-options/#groupname).

### Security branch compatibility

The two fleet runners use the official Renovate 44.215.0 image with a narrowly
scoped, in-memory compatibility fix. Its branch planner otherwise takes all
branch settings from the first alphabetically sorted dependency. A mixed group
can therefore lose its security priority when the vulnerable dependency sorts
later. The fix selects a vulnerable member as the branch settings source while
preserving the entire ordered upgrade list and the existing aggregate review,
automerge and minimum-group-size rules. Ordinary-only groups are unchanged.

The loader verifies the package version and SHA-256 of the one affected upstream
planner module before the CLI can access credentials. Unknown source fails closed; it
never edits the installed package, loads repository code, or exports Node loader
flags to package-manager subprocesses. Contract tests exercise native branch
planning, both directions of the npm/Cargo pair, scheduling and independent
security limits, plus an isolated official-image smoke test without credentials.
The runtime version itself requires a reviewed change while this compatibility
fix is present. To upgrade, reproduce the native tests against the new upstream
version and either retire the fix or explicitly requalify its one-module hash;
do not simply advance the runtime pin.

### Python package build boundary

The same loader also verifies four Python manager and post-upgrade modules and appends
`--no-build` to UV compile and lock commands. The UV compile header parser accepts
that flag for subsequent runs. Native generation and allowed post-upgrade UV
commands use the reviewed UV 0.13.4, preserving other tool constraints. Legacy
pip-tools compilation is not qualified for this runner. `ignoreScripts` and `UV_NO_BUILD` alone do not prevent all metadata
backend execution in the reviewed Renovate/UV combination.

UV lock resolution can still run a first-party project's metadata backend even
with `--no-build`. Before invoking it, the runner checks the raw project metadata
and local file context. Dynamic metadata, workspaces, local editable/directory
sources and unqualified configuration stop the update with an artifact error.
Static projects and confined prebuilt wheel sources remain supported. This check
is separate from third-party source builds, which the CLI flag prohibits.

Existing source-only compiler environments are held as complete source/output
units in their repository policies; security alerts remain enabled for manual
remediation. Do not accept resolver downgrades or change package origins to make
such an environment pass. An existing immutable source archive may use reviewed,
version-specific static dependency metadata without running its build backend.
The offline official-image contract test exercises an inert metadata backend,
requires the unprotected control to execute it, and requires the guarded path
to reject it while still generating a hashed wheel lockfile. Requalify all five
module hashes and these behavior tests before changing the runtime version.
