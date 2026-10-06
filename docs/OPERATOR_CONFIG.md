# Public source and operator configuration

The checked-in `fleet.json` is a public repository snapshot. Deployment-specific
consumer identities, rollout inventories and runner access policies belong in
operator-controlled storage outside Git. Examples and tests use synthetic
consumers; they are not an inventory or authorization for a live deployment.

## Inventory lookup and compatibility

`fleet.py` and `run_nightly.py` accept `--inventory /absolute/path/fleet.json`.
Without that flag, they use `~/.config/venus-os-ci-toolkit/fleet.json` when it
exists, otherwise the checked-in public inventory. An existing unreadable or
invalid user inventory fails; it does not fall back to a smaller public selection.
An unknown explicitly selected repository fails before any repository operation.

Before updating an installation that operates on non-public consumers, preserve
its reviewed full inventory outside the checkout. Keep the same repository
identities, child directory names, default branches and policy fields. Provision
that inventory for the actual scheduler's operating-system user, or add an
explicit `--inventory` argument to its reviewed caller. Do this on each operator
host; a file prepared on one workstation does not migrate another machine.

Use owner-only directory and file permissions (`0700` and `0600`). Back up the
old inventory and compare the repository selection before changing a caller.
Do not discover or infer a private fleet from filesystem directory names.

To operate on only the checked-in public snapshot regardless of user defaults:

```sh
python3 scripts/fleet.py status --inventory ./fleet.json --root /path/to/checkouts
```

This reads local checkout status; `render`, `check`, and `submit --execute` have
their documented effects. Normal identity, tracked-file and clean-checkout
checks remain required. Inventory selection does not authorize a release or merge.

## Runner switch configuration

`scripts/runner_mode.py` reads `~/.config/venus-os-ci-toolkit/runner-mode.json`,
or the explicit `--config` path. A missing file, symlink, invalid schema or unknown
consumer fails before any GitHub API call. There are no built-in live consumers.

Start from [`runner-mode.example.json`](../deploy/arc-ottplay/runner-mode.example.json)
in an operator directory outside Git. Replace the synthetic organization,
consumer aliases and repository identities with the reviewed inventory. Preserve
the original aliases if existing callers use them. Record the exact pool labels,
group names, selected repositories and enforced workflow restrictions returned by
GitHub. Do not include a PAT, App private key, registration token or signing key.
The separate [`github-groups.json`](../deploy/arc-ottplay/github-groups.json) is an
illustrative group-policy template, not a live group manifest.

The JSON schema requires:

- Schema `1`, one organization and explicitly named consumer aliases.
- A unique repository identity in that organization for each alias.
- A `smoke-linux` job and optional `smoke-kvm` / `smoke-release` bindings, each
  containing exactly a runner label and group name.
- Selected-repository groups that prohibit public repositories and contain only
  configured consumers. Release pools require enforced, exact default-branch
  references for `release.yml` and `runner-smoke.yml`.

Configuration is trusted operator input, never pull-request input. The API fence
is built only after the complete file validates. It admits the exact selected
repository reads and `CI_RUNNER_MODE` writes, plus the required organization group
reads. It cannot access secrets, dispatch or cancel runs, change group policy,
change a different variable or follow arbitrary API paths.

```sh
python3 scripts/runner_mode.py --config /path/to/operator/runner-mode.json --repo console
```

That command reads status only; `console` is a synthetic example alias. Activation
still requires the operator-reviewed full source SHA, a current-main successful
smoke no older than 24 hours, actual runner/group evidence and an explicit
`--apply`. The tool rechecks source, smoke and the variable before writing.
Preparing the JSON files does not change any runner mode, workflow, permission,
registration or running job. See [runner admission](ottplay-k3s-runners.md) and
the separate [public-project reserve](RUNNERS.md).
