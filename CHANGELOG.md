# Changelog

## 0.1.1 - 2026-10-13

- Add versioned source candidates with deterministic archives, an explicit source
  inventory, version receipts and contract tests against the extracted source.
- Require bounded Linux Atheris campaigns for RCs over the owned OCI metadata
  and version-policy parsers; record evidence for the exact candidate source.
- Reject JSON numbers that overflow finite floating-point representation before
  accepting OCI metadata, preserving canonical JSON round trips.
- Keep source distribution versions independent of consumer workflow commit pins.

## 0.1.0 - 2026-09-12

- First tagged source release of the reusable GitHub Actions workflows.
- Document the additional native Venus OS checks required for service installers:
  BusyBox compatibility, persistent paths, repeat installs, offline dependencies,
  bounded logging and target Python/architecture compatibility.

Passing host CI does not establish device compatibility. This repository provides
CI workflows, not a GX runtime package. Consumers pinned to an immutable commit
remain on that commit until explicitly updated.
