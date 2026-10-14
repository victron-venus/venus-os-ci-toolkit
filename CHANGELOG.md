# Changelog

## [0.1.1] - 2026-10-13

- Add versioned source candidates with deterministic archives, an explicit source
  inventory, version receipts and contract tests against the extracted source.
- Require bounded Linux Atheris campaigns for RCs over the owned OCI metadata
  and version-policy parsers; record evidence for the exact candidate source.
- Reject JSON numbers that overflow finite floating-point representation before
  accepting OCI metadata, preserving canonical JSON round trips.
- Keep source distribution versions independent of consumer workflow commit pins.

### Upgrade

Download the candidate source archive and follow `docs/self-release.md` to install
its locked development dependencies and rerun the extracted-source tests. Existing
consumers keep their immutable workflow commits until explicitly updated; source
candidate versions are independent of those workflow references.

### Security

The OCI metadata reader now rejects overflowing JSON numbers. RCs additionally
record bounded, isolated Linux campaigns over the metadata and version-policy
parsers. Review the evidence attached to the exact candidate tag before adopting
it. These software checks do not certify hardware integrations or deployed systems.

## 0.1.0 - 2026-09-12

- First tagged source release of the reusable GitHub Actions workflows.
- Document the additional native Venus OS checks required for service installers:
  BusyBox compatibility, persistent paths, repeat installs, offline dependencies,
  bounded logging and target Python/architecture compatibility.

Passing host CI does not establish device compatibility. This repository provides
CI workflows, not a GX runtime package. Consumers pinned to an immutable commit
remain on that commit until explicitly updated.
