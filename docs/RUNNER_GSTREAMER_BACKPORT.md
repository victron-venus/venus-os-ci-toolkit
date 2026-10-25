# Runner GStreamer backport

The browser image retains `gstreamer1.0-plugins-bad` and
`libgstreamer-plugins-bad1.0-0`, both required by the pinned Playwright dependency
set. Ubuntu's public Noble version `1.24.2-1ubuntu4` is affected by
[CVE-2025-3887](https://ubuntu.com/security/CVE-2025-3887). Ubuntu lists its fixed
Noble package under ESM Apps. This build uses public source and a local backport;
it needs no ESM credentials.

[The GStreamer advisory](https://gstreamer.freedesktop.org/security/sa-2025-0001.html)
recommends patching older branches. The patch applies the code changes from
[5463f0e](https://github.com/GStreamer/gstreamer/commit/5463f0e09768ca90aa8c58357c1f4c645db580db)
and [bcaab36](https://github.com/GStreamer/gstreamer/commit/bcaab3609805ea10fb3d9ac0c9d947b4c3563948)
to `gst-libs/gst/codecparsers/gsth265parser.c`. The adaptation changes only the
monorepo prefix and unchanged surrounding context: Noble retains its two system
includes and lacks a later explanatory comment. It preserves the upstream code
changes. Original and patched source hashes, patch hashes, source archive hashes,
and official Ubuntu binary package hashes are in `runner-gstreamer.json`.
The `.dsc` is hash-pinned and its archive checksums are checked; the helper does
not claim an independent OpenPGP signature verification.

The `patched-gstreamer` Docker target uses a non-root native builder. It applies
the original three distro patches, then the backport with zero fuzz. It runs the
original Meson build definition for only the 20-file `codecparsers` library,
with two compiler jobs, distro hardening flags and no subproject downloads.
The existing early preflight builds this target and `patched-libbpf` on the same
bounded builder before the full browser image, reusing the same cache. The two
preflights share a finite 900-second budget.

The helper compares the resulting ELF architecture, SONAME, all defined dynamic
symbol names/types and dynamic library dependencies with the pinned Ubuntu
library. An ordinary allocation/free smoke checks the new H.265 parser API.
No malformed media, exploit test or full GStreamer test-suite claim is involved.
Unrelated optional plugins are not recompiled.

Two derived `.deb` files use version `1.24.2-1ubuntu4+victron1`:

* The library package replaces only `libgstcodecparsers-1.0.so.0.2402.0`.
* The plugin package retains its entire payload. Its exact library dependency
  changes to the same downstream version.

Both retain their other dependencies, ABI links, copyright files, maintainer
scripts and plugin metadata. The controls identify the downstream maintainer
and source version. The library package's checksum and installed-size metadata
are updated. The helper re-extracts both generated packages and compares their
complete file/link/mode inventories. Extraction does not execute maintainer
scripts. The final image installs both together after browser apt work and
checks every regular payload file's SHA256.

`/usr/local/share/victron-gstreamer/` retains source and patch notices plus
`build-provenance.json`: source identity, builder hash, actual build dependency
versions, commands, before/after library hashes, ABI observations, and original
and installed package payload inventories. Original changelog and copyright
remain available; the derivative procedure is described here and by its
manifest/provenance rather than attributed to Ubuntu.

These build checks do not accept the final image. The unfiltered final scan and
the separate source-bound applicability gate must still inspect the effective
installed files. A package version or supplied success boolean alone is not
sufficient to accept a backport, and raw findings are not rewritten or hidden.

The final-image reader collects both installed dpkg records, file lists and
checksums, the retained source/provenance records, and the selected library's
actual SHA256 and bounded ELF64 metadata. The gate checks its architecture,
SONAME and dynamic dependencies against the original library, verifies both
complete payload inventories against independently pinned official-package
baselines, and permits only the selected library to differ. The plugin payload
and unrelated dependency fields must remain unchanged; the paired dependency
must identify the same downstream version. This prerequisite applies even if
the scanner reports no blocking GStreamer finding.

Only `CVE-2025-3887` on the two exact amd64 Ubuntu package identities at
`1.24.2-1ubuntu4+victron1`, with the reviewed advisory content, can be resolved by
this backport proof. Other versions, packages, advisories or changed payloads
remain unresolved. The raw report, severity counts and raw scan gate retain
their original values. The separate acceptance result records the verified
backport rule and binds the canonical effective-file evidence.

Local source/package tests (no downloads or image execution):

```sh
python3 -B -m unittest discover -s tests -p test_runner_gstreamer.py -v
ruff check scripts/build_runner_gstreamer.py tests/test_runner_gstreamer.py
```

`tests/test_runner_image_artifacts.py` also exercises the real OCI/package/ELF
reader with a tiny synthetic GStreamer payload and a distinct fixture policy.
It proves the component boundary and rejection of changed plugin bytes even
when their dpkg checksum is updated. Its other mandatory image components are
deliberately absent; that fixture is not a successful full-image qualification.

The ordinary synthetic `.deb` roundtrip requires `dpkg-deb`; other tests are
portable. Actual C compilation, ABI equality and final browser behavior are
confirmed only by a subsequent successful bounded image build and its receipts.
