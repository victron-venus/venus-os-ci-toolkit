#!/usr/bin/env python3
"""Documentation checks shared by every repository in the organization.

Seven repositories each grew their own `check_docs.py`, differing between 37 and
179 lines and none of them covering the same set of rules. This module is the
single implementation. A repository runs it from a five-line wrapper, or calls
it directly from CI.

Checks, each independently selectable:

  links      every relative link and image target resolves
  anchors    every in-page `#fragment` matches a heading or explicit anchor
  svg        every tracked SVG parses as XML
  index      every `docs/**.md` is reachable from `docs/README.md`
  fences     markdown code fences are balanced
  mermaid    every mermaid block declares a diagram type
  version    a `version` file, `pyproject.toml` and the newest tag agree

Usage:
    python3 check_docs.py --root . --checks links,anchors,svg,index,fences,mermaid
    python3 check_docs.py --root . --checks all
Exit status is 1 when any check fails, and the failures are printed one per line.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

# Shared repository boilerplate. It is intentionally not part of a project's
# navigation: the canonical copy lives in venus-os-ci-toolkit.
SHARED_BOILERPLATE = {
    'release-workflow.md', 'quality.md', 'openssf-evidence.md',
    'runner-preflight.md', 'release-read-safety.md', 'VERSIONING.md',
    'rc-fuzzing.md', 'transport-security.md',
}

# Templates are rendered into a consuming repository, so a relative link there
# is resolved against that repository, not against the toolkit. Their targets
# are checked where they land.
TEMPLATE_DIRS = {'templates'}

LINK = re.compile(r'!?\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+"[^"]*")?\)')
HTML_HREF = re.compile(r'href="(?P<target>[^"]+)"')
HEADING = re.compile(r'^#{1,6}\s+(?P<title>.+)$', re.M)
EXPLICIT_ANCHOR = re.compile(r'<a\s+[^>]*(?:id|name)="(?P<id>[^"]+)"')
FENCE = re.compile(r'^```[^\n]*$', re.M)
FENCED_BLOCK = re.compile(r'^```[^\n]*\n.*?^```[^\n]*$', re.M | re.S)
MERMAID = re.compile(r'^```mermaid\n(?P<body>.*?)^```', re.M | re.S)
MERMAID_TYPES = re.compile(
    r'^\s*(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram(-v2)?|'
    r'erDiagram|journey|gantt|pie|quadrantChart|requirementDiagram|gitGraph|'
    r'C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|mindmap|timeline|'
    r'sankey-beta|xychart-beta|block-beta|packet-beta|architecture-beta|kanban)',
    re.M,
)


def tracked(root: Path) -> list[Path]:
    try:
        out = subprocess.check_output(
            ['git', 'ls-files', '-z'], cwd=root, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob('*.md') if '.git' not in p.parts]
    return [root / name for name in filter(None, out.decode('utf-8', 'replace').split('\0'))]


def slugify(title: str) -> str:
    text = re.sub(r'[`*_\[\]()]', '', title).strip().lower()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[\s]+', '-', text)


def prose(text: str) -> str:
    """Markdown with fenced code removed.

    A fenced block is example code, not documentation: an RFC pattern such as
    ``[a-z0-9]`` inside it is not a link, and an inline ``- [x](y)`` sample in a
    shell snippet is not a target.
    """
    return FENCED_BLOCK.sub('', text)


def anchors(text: str) -> set[str]:
    found = {slugify(m.group('title')) for m in HEADING.finditer(text)}
    found |= set(EXPLICIT_ANCHOR.findall(text))
    return found


def check_links(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        if TEMPLATE_DIRS & set(path.relative_to(root).parts[:-1]):
            continue
        body = prose(path.read_text(encoding='utf-8'))
        targets = LINK.findall(body) + HTML_HREF.findall(body)
        for link in targets:
            if link.startswith(('http:', 'https:', 'mailto:', 'tel:', '#')):
                continue
            destination = unquote(link.split('#')[0]).strip('<>')
            if not destination:
                continue
            target = path.parent / destination
            if target.is_dir():
                target = target / 'README.md'
            if not target.exists():
                errors.append(f'{path.relative_to(root)}: broken link {destination}')
    return errors


def check_anchors(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        body = prose(path.read_text(encoding='utf-8'))
        for link in LINK.findall(body) + HTML_HREF.findall(body):
            if '#' not in link or link.startswith(('http:', 'https:', 'mailto:')):
                continue
            file_part, _, fragment = link.partition('#')
            target = path if not file_part else path.parent / unquote(file_part)
            if not target.is_file() or not fragment:
                continue
            if unquote(fragment) not in anchors(target.read_text(encoding='utf-8')):
                errors.append(f'{path.relative_to(root)}: missing anchor #{fragment}')
    return errors


def check_svg(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.svg' or not path.is_file():
            continue
        try:
            ET.parse(path)
        except ET.ParseError as error:
            errors.append(f'{path.relative_to(root)}: invalid SVG ({error})')
    return errors


def check_index(root: Path, files: list[Path]) -> list[str]:
    docs = root / 'docs'
    index = docs / 'README.md'
    if not docs.is_dir():
        return []
    if not index.is_file():
        return ['docs/README.md: missing navigation index']
    body = prose(index.read_text(encoding='utf-8'))
    linked = {unquote(t.split('#')[0]) for t in LINK.findall(body)}
    errors: list[str] = []
    for path in files:
        if docs not in path.parents or path.suffix != '.md':
            continue
        if path == index or path.name in SHARED_BOILERPLATE:
            continue
        relative = path.relative_to(docs).as_posix()
        if relative not in linked and f'./{relative}' not in linked:
            errors.append(f'{path.relative_to(root)}: not linked from docs/README.md')
    return errors


def check_fences(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        count = len(FENCE.findall(path.read_text(encoding='utf-8')))
        if count % 2:
            errors.append(f'{path.relative_to(root)}: unbalanced code fence')
    return errors


def check_mermaid(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        for block in MERMAID.finditer(path.read_text(encoding='utf-8')):
            if not MERMAID_TYPES.search(block.group('body')):
                errors.append(
                    f'{path.relative_to(root)}: mermaid block without a diagram type'
                )
    return errors


def _newest_tag(root: Path) -> str:
    try:
        tags = subprocess.check_output(
            ['git', 'tag', '--sort=-v:refname'], cwd=root, stderr=subprocess.DEVNULL,
            text=True,
        ).split()
    except (OSError, subprocess.CalledProcessError):
        return ''
    for tag in tags:
        if re.search(r'\d', tag):
            return tag
    return ''


def _normalise(version: str) -> str:
    text = version.strip().lstrip('v')
    text = re.sub(r'^(.*?)-v', r'\1-v', text)
    # 0.4.1-beta.3, 0.4.1b3 and 0.4.1b3 all normalise to the same comparison key.
    text = text.replace('-beta.', 'b').replace('-rc.', 'rc').replace('-alpha.', 'a')
    return text.lower()


def check_version(root: Path, _files: list[Path]) -> list[str]:
    version_file = root / 'version'
    pyproject = root / 'pyproject.toml'
    declared: list[tuple[str, str]] = []
    if version_file.is_file():
        declared.append(('version', version_file.read_text(encoding='utf-8').strip()))
    if pyproject.is_file():
        match = re.search(r'^version\s*=\s*"(?P<v>[^"]+)"', pyproject.read_text(encoding='utf-8'), re.M)
        if match:
            declared.append(('pyproject.toml', match.group('v')))
    manifest = root / 'custom_components'
    for path in manifest.glob('*/manifest.json'):
        match = re.search(r'"version"\s*:\s*"(?P<v>[^"]+)"', path.read_text(encoding='utf-8'))
        if match:
            declared.append((str(path.relative_to(root)), match.group('v')))
    if len(declared) < 2:
        return []
    tag = _newest_tag(root)
    keys = {_normalise(value) for _, value in declared}
    errors: list[str] = []
    if len(keys) > 1:
        errors.append('version disagreement: ' + ', '.join(f'{n}={v}' for n, v in declared))
    if tag:
        tag_key = _normalise(tag)
        newest = _normalise(declared[0][1])
        if tag_key.split('b')[0].split('rc')[0] != newest.split('b')[0].split('rc')[0]:
            errors.append(f'version {newest} does not match newest tag {tag}')
    return errors


CHECKS = {
    'links': check_links,
    'anchors': check_anchors,
    'svg': check_svg,
    'index': check_index,
    'fences': check_fences,
    'mermaid': check_mermaid,
    'version': check_version,
}
DEFAULT = 'links,anchors,svg,index,fences,mermaid'


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', default='.', help='repository root')
    parser.add_argument('--checks', default=DEFAULT,
                        help=f'comma-separated checks or "all" (default: {DEFAULT})')
    args = parser.parse_args()

    root = Path(args.root).resolve()
    files = tracked(root)
    selected = list(CHECKS) if args.checks == 'all' else [
        name.strip() for name in args.checks.split(',') if name.strip()
    ]
    unknown = [name for name in selected if name not in CHECKS]
    if unknown:
        print(f'unknown checks: {", ".join(unknown)}', file=sys.stderr)
        return 2

    errors: list[str] = []
    for name in selected:
        errors.extend(CHECKS[name](root, files))
    if errors:
        print('\n'.join(sorted(set(errors))))
        return 1
    print(f'PASS: {", ".join(selected)} ({len(files)} tracked files)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
