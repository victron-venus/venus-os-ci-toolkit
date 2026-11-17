# Documentation checks

`scripts/check_docs.py` is the single documentation checker for the
organization. Seven repositories grew their own copy, between 37 and 179 lines,
and none of them enforced the same rules. This one covers all of them.

## Running it

From the toolkit, against any checkout:

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
| `links` | Every relative link and image target resolves. Fenced code is ignored, so an example such as `[a-z0-9]` is not read as a link. |
| `anchors` | Every `#fragment` matches a heading slug or an explicit anchor in the target file. |
| `svg` | Every tracked SVG parses as XML. |
| `index` | Every `docs/**.md` is reachable from `docs/README.md`, excluding shared boilerplate. |
| `fences` | Markdown code fences are balanced. |
| `mermaid` | Every mermaid block declares a diagram type, so it renders instead of showing as a code block. |
| `version` | A `version` file, `pyproject.toml` and an integration `manifest.json` agree, and the newest tag matches. |

Default set: `links,anchors,svg,index,fences,mermaid`. The `version` check is
opt-in because most repositories do not declare a version in more than one
place.

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
