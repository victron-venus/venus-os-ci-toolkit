# Browser prerequisites in the reserve image

Self-hosted compatibility jobs cannot install branded browser packages: those
installers require an OS package manager. The public reserve image therefore
provides Chrome stable/beta and Edge stable/beta on amd64. The private image
inherits them. The four packages are installed at Playwright's default paths.
Both architectures have the 79 Ubuntu24 native dependencies shared by the pinned
Playwright 1.62.1, 1.63.0 and 1.64.0 generations, including Firefox/WebKit libraries.
Managed Chromium, Firefox and WebKit binaries remain per-job downloads under
`RUNNER_TEMP`, using the existing version and integrity locks.

The official Edge repository's arm64 index is empty. The arm64 image remains
buildable with native libraries, but explicitly advertises no complete branded
browser capability. It must not qualify jobs requiring the four-channel set.
Google's separate arm64 packages do not establish full Edge coverage.

`runner-browsers.json` pins package identity, version, architecture, URL path,
SHA256 and size. Its adjacent repository snapshots are public signing keys,
detached Release signatures and complete Packages indexes, not browser binaries.
The installer verifies the expected primary signing fingerprint, the signature,
the Release index digest and the exact package row before downloading a pinned
archive. Snapshots preserve reproducibility when a vendor refreshes its current
index. Package downloads still require the vendor to retain the pinned versions;
unavailability fails the build and requires a reviewed lock refresh.

The Ubuntu dependency list is pinned by name to reviewed upstream `nativeDeps.ts`
source hashes; resolved distro versions are recorded and receive the full image
scan. Browser apt operations precede the exact patched-libbpf installation, so
dependency resolution cannot replace that backport. Vendor post-install defaults
disable repository re-enablement; only specifically named new vendor cron/source
entries are removed. Unrelated Ubuntu configuration and dpkg metadata remain.

The amd64 build runs a sequential four-channel local-page probe as UID1001 using
a temporary integrity-pinned Playwright1.64.0 npm toolchain. It installs with
`npm ci --ignore-scripts`, downloads no managed browsers and removes its own
toolchain/cache in the same layer. The probe uses the shared workflow's launch
defaults, checks exact versions, page content and JavaScript evaluation, and
closes every browser. It adds no ad-hoc Chromium flags. Records under
`/usr/local/share/victron-browsers` distinguish package installation from actual
browser probe success. This build probe does not qualify gVisor: the full Linux
compatibility matrix and native standard-profile memory/isolation acceptance
remain required.

The retained probe can be run in a qualified image with an existing trusted
toolchain: `node /opt/victron-ci-reserve/qualify-browsers.cjs "$CI_BROWSER_TOOLCHAIN"
/usr/local/share/victron-browsers/installed.json` (one command, as UID1001).

The four packages total 661.335 MiB compressed and declare 2.193 GiB installed,
excluding additional distro libraries. Their temporary archives and apt cache
are removed in the installation layer. Existing builder, scan and disk limits
are unchanged; actual resource failures remain failures. Installed size does
not estimate peak browser RAM.

To update, capture the official signed repository metadata, independently verify
the public signing fingerprint and selected package rows, and update the lock,
snapshots and source inventory together. Recheck the native dependency union
when changing supported Playwright versions. Freeze a new canonical context and
run normal source CI, image builds, full scans and actual browser qualification.
