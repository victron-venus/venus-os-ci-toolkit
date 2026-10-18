# Reviewed gVisor VFS candidate build

This manual workflow builds a quarantined candidate from public source. It does
not install a runtime, deploy software, create a release, or qualify a runtime
for production. Its output must receive a separate regression and deployment
review before use.

## Frozen inputs

- Upstream source: `google/gvisor`, commit
  `60faa657ca99f3398e04c8dbc580a848df20f2f2` (`release-20261012.0`).
- Patch: `patches/gvisor/vfs-final-symlink.patch`, SHA256
  `97e0de464f0675e1820f81798d614aeb297f8ea88d3289ac6349120813a8ac75`.
  This is the three-file public fix associated with
  [upstream PR 14789](https://github.com/google/gvisor/pull/14789), including the
  `/tmp` companion fix and two syscall regression cases.
- Official `default_x86_64` builder: digest
  `sha256:667ce7798657a8fd80d737119b478977dc60708e140b41768099784f6d58f945`.
- Bazel: `8.8.1`; source-requested Go SDK: `1.27.2` with the source's Go patch.

The script creates a local deterministic candidate commit, with fixed generic
identity/date, and tags it `vfs-candidate-20261012-97e0de464f06`. This tag exists
only inside the disposable build checkout; nothing is pushed to any repository.
The candidate is not labelled as an unchanged upstream release.

## Run and interpretation

After code review and an explicit execution decision, open Actions, select
**Build reviewed gVisor VFS candidate**, and run it on the default branch. There
are no free-form source, shell command, image, runner, or secret inputs. The
workflow only admits a public repository and a native GitHub-hosted x64 runner.
It deliberately uses literal `ubuntu-24.04`, independent of runner-mode variables.

The initial stage builds these exact targets:

- `//debian:gvisor-release-tar-bz2` and `//:release`: all seven runtime bundle files.
- `//test/syscalls/linux:open_test`: compilation only.

It executes only `//pkg/sentry/vfs:vfs_test` and
`//runsc/prewarmer:prewarmer_single_page_test`, with uncached results and no skipped
tests. It does not execute `open_test`, `runsc`, Sentry, sidecars, syscall wrappers,
`boot_test`, or any KVM/systrap canary. `boot_test` includes runtime startup with
the root filesystem exposed and seccomp disabled, so it is deliberately outside
this stage. Do not expand the target list to a wildcard.

Successful status is **artifact and allowed unit tests passed, not runtime
qualified**. Startup with ordinary and symlink `/tmp`, syscall regressions,
unchanged consumer tests, and platform/lifecycle acceptance remain separate gates.

## Resource and isolation contract

The [public standard runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
is advertised as 4 CPU, 16 GB RAM and 14 GB SSD. These are not guaranteed free
bytes. The pinned builder has 823,866,778 compressed layer bytes; its expanded
size and this cold build's high-water mark have not been benchmarked.

Before pull, the script requires at least 12 GB free disk, 13 GB MemAvailable and
four CPUs. It checks both the owned filesystem and Docker's filesystem. After
image/source staging, it requires 8 GB free. It stops work below 2 GB free, records
the minimum observed bytes, and fails rather than deleting host tools or caches.
The watchdog checks every second during long commands. Disk checks are not a
hard quota and may overshoot between samples.

The container is capped at three CPUs and 12 GB RAM with no additional swap.
Bazel uses two jobs, an 8,000 MB scheduling estimate and a 2 GiB JVM heap ceiling.
The container limit covers child processes; the Bazel settings alone do not.
The complete work phase has a 90-minute alarm. The job limit is 95 minutes,
reserving time for cleanup and upload. A failed attempt is not retried or enlarged
automatically. Use its resource receipt to review any later attempt.

The exact builder runs as a nonroot user with all capabilities dropped,
no-new-privileges, read-only container root and default seccomp. Only owned source
and output/cache directories plus the read-only build script are mounted. The
owned source is writable because Bazel generates a module lockfile; tracked
source differences and the candidate tree are checked again after the build.
There are no host sockets, credentials, home/root mounts, devices, host namespaces,
or privileged fallback. `processwrapper-sandbox` is selected explicitly for
actions and genrules: it supplies declared-input isolation, while the outer
container/VM supplies the security boundary. It is not equivalent to per-action
Linux namespace isolation.

The host controls its own Docker daemon to launch and remove the container; that
socket is never passed to build code. Cleanup requires the exact name, immutable
container ID and per-run ownership label. It removes only that container and
verifies no container with its ownership label remains. Owned temporary source
and cache files are left for normal disposal with the hosted VM. No host directory
deletion, image pruning, service reset, or unrelated job cancellation is used.

## Output and privacy

The build receives no inherited Actions token, secret environment, private
repository, or host configuration. Actions are pinned; checkout does not persist
credentials. The inherited runner hardening action audits outbound connections.
The build's public dependencies are fetched from their source-defined origins;
this is not an enforced outbound-network allowlist.

The artifact contains the seven-file archive, compiled `open_test`, two unit-test
XML reports, the generated module lockfile and stable-status file, build/host
receipts and checksum manifest. It also includes the exact upstream Apache-2.0
`LICENSE`, the exact applied patch, and a separate `MODIFICATIONS.txt` identifying
the source and changes. The pinned upstream source has no root `NOTICE`; the
added modification notice is not represented as an upstream file. License and
patch digests are checked both before building and before uploading.

The archive is checked for exact names, regular files, 0755 modes,
x86-64 ELF identity, no dynamic interpreter or required shared library, and the
prewarmer's one-page size limit. No output executable is invoked to identify it.
The generated Bazel `STABLE_VERSION` must equal the expected candidate marker
exactly, without a dirty suffix or duplicate value. The same unique candidate
marker must be present in the archived `runsc` bytes. This checks generated stamp
content and byte-substring presence, not decoded symbol semantics or actual
`runsc --version` output. Both checks are recorded in the receipt and repeated
before upload; they do not qualify runtime behavior.
The receipt lists available build tools separately from claims about which tool
each action used. It records resolved Go SDK executable identities.

The allowlisted artifact is capped at 500 MB and retained seven days. No caches,
Git metadata, whole workspaces, environment dumps, private plans or private test
data are uploaded. Treat all logs and artifacts as public. Upload failure means
delivery is incomplete even if compilation succeeded. Failure receipts distinguish
build failure from cleanup failure; they never convert either into success.

## Local validation

`python3 -m unittest discover -s tests -p test_build_gvisor_vfs_candidate.py`
checks event admission, environment filtering, container restrictions, resource
failure gates, ownership-checked cleanup, exact scope, and archive/ELF/XML
validation. These tests use synthetic files and mocks; they do not launch Docker,
Bazel, a VM, or the candidate runtime. `actionlint` validates the workflow syntax.
Additional cases reject missing/wrong stamps, missing markers, altered license or
patch files, and verify owned-process termination when the deadline expires.

Changing an input pin, target, runner contract or resource limit requires another
review. This workflow has no automatic promotion or consumer deployment path.
