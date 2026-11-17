# Documentation checks

`scripts/check_docs.py` supplies generic documentation checks for the
organization. Seven repositories grew their own copy, between 37 and 179 lines,
and none of them enforced the same rules. Repository-specific rules remain in
their local checkers, as described below.

## Running it

With Python 3.11 or newer, from the toolkit against a checkout:

```sh
python3 scripts/check_docs.py --root /path/to/repository
```

Or from a consuming repository through a five-line wrapper:

```sh
#!/usr/bin/env bash
set -euo pipefail
exec python3 "$(git rev-parse --show-toplevel)/scripts/_toolkit/check_docs.py" --root . "$@"
```

## Checks

| Check | What it enforces |
| --- | --- |
| `links` | Inline Markdown links/images and double-quoted HTML `href` targets resolve within the repository. Fenced code is ignored. Root-relative paths start at the repository root. |
| `anchors` | Fragments in those links to Markdown files match an ATX heading slug or explicit anchor. Duplicate headings receive numeric suffixes; headings inside fenced examples do not count. |
| `svg` | Every tracked SVG parses as XML. |
| `index` | Every tracked `docs/**.md` is reachable through navigation links from `docs/README.md`, including nested indexes and excluding shared boilerplate. |
| `fences` | Backtick and tilde fences close with the same marker and at least the opening length. |
| `mermaid` | Every mermaid block declares a diagram type, so it renders instead of showing as a code block. |
| `version` | A `version` file, `[project].version` in `pyproject.toml` and an integration `manifest.json` agree, and the newest tag matches, including its prerelease number. |

Default set: `links,anchors,svg,index,fences,mermaid`. The `version` check is
opt-in because most repositories do not declare a version in more than one
place.

These are lightweight checks, not a complete Markdown renderer. Reference-style
links, indented code blocks and Setext headings are not parsed. Non-Markdown
fragment formats (for example SVG element IDs) are left to a format-specific
checker. External URLs are not fetched.

## Why the `index` check matters

Across the organization, 97 `docs/` trees existed and only eight had an index.
About forty percent of documents were reachable from nowhere: no README link, no
navigation entry, no parent page. The check makes an orphan a CI failure rather
than a document nobody finds.

## Templates are exempt

A file under `templates/` is rendered into a consuming repository, so a relative
link in it resolves against that repository, not against the toolkit. Those
targets are checked where they land.

## Tests

`tests/test_check_docs.py` exercises every check against a temporary repository,
including the false-positive cases that motivated them: a link inside a fenced
block, a heading slug, a beta tag against a normalised version.

## What this checker is not

Eight repositories had grown a local `check_docs.py`, and they are **not**
replaced by this one. Each enforces a rule that only makes sense there:

| Repository | Local rule this checker does not implement |
| --- | --- |
| `ha-entity-migrations`, `ha-scenario-tests`, `assist-scenario-lab`, `ha-meter-contracts` | A 24-language catalog and guide manifest whose source digest must match the English text |
| `ha-echo-show-5`, `ha-echomuse` | SVG accessibility (title, `role`, `aria-label`) and a shipped-file inventory that must hold outside a Git checkout |
| `ha-techo5-dot` | Source pins recorded next to the pages they describe |
| `zac93-esp32` | Complete 24-language wiki families with generated language navigation and an exact step count per page |

Replacing those with a wrapper would delete a real check, and vendoring this
module into eight repositories would recreate the drift it exists to end. Use
this checker where the rules are generic — a `docs/README.md` index, resolvable
relative links, mermaid that renders — and keep a local script for a local
invariant.

## Running it from CI

`.github/workflows/docs-check.yml` is a reusable workflow. A repository calls it
with an immutable toolkit commit, so the checker is not vendored:

```yaml
jobs:
  docs:
    uses: victron-venus/venus-os-ci-toolkit/.github/workflows/docs-check.yml@<40-char-sha>
    with:
      toolkit-ref: <40-char-sha>
      checks: links,anchors,index,fences
      python-version: '3.12'
```

The workflow is callable only, requests `contents: read`, and pins every action
to a full commit SHA. It rejects a branch, tag or malformed `toolkit-ref` before
checkout. Use the same immutable revision in both positions in the example.
`tests/test_docs_workflow.py` exercises the guard and checks action pins against
the toolkit's canonical action inventory.
A repository that also needs a local invariant keeps its own script for that
invariant and calls this workflow for the generic rules.
