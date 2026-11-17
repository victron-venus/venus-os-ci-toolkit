#!/usr/bin/env python3
"""Generic documentation checks shared across repositories.

Eight repositories each grew their own `check_docs.py`, differing between 37 and
179 lines and none of them covering the same set of rules. This module is the
shared implementation. Keep repository-specific invariants in local checkers;
call this module directly or through the reusable documentation workflow.

Checks, each independently selectable:

  links      inline Markdown links/images and HTML href targets resolve
  anchors    Markdown fragments match an ATX heading or explicit anchor
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
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit
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

# Checkouts of third-party code: a broken link there is upstream's, not ours, and
# rewriting vendor sources would make the next update fight the diff.
VENDOR_DIRS = {'vendor', 'node_modules'}

# rustdoc intra-doc links (``[`Variant`](struct@Variant)``) are resolvable only by
# rustdoc against the crate; they are not repository paths.
RUSTDOC_LINK = re.compile(r'^(struct|enum|trait|fn|mod|type|const|static|macro|union|'
                          r'prim|keyword)@')

LINK = re.compile(r'!?\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+"[^"]*")?\)')
HTML_HREF = re.compile(r'href="(?P<target>[^"]+)"')
HEADING = re.compile(r'^#{1,6}\s+(?P<title>.+)$', re.M)
EXPLICIT_ANCHOR = re.compile(r'<a\s+[^>]*(?:id|name)="(?P<id>[^"]+)"')
FENCE = re.compile(r'^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>.*)$')
MERMAID_TYPES = re.compile(
    r'^\s*(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram(-v2)?|'
    r'erDiagram|journey|gantt|pie|quadrantChart|requirementDiagram|gitGraph|'
    r'C4Context|C4Container|C4Component|C4Dynamic|C4Deployment|mindmap|timeline|'
    r'sankey-beta|xychart-beta|block-beta|packet-beta|architecture-beta|kanban)(?=\s|$)',
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


def markdown_parts(text: str) -> tuple[str, list[tuple[str, str]], bool]:
    """Separate prose and fenced blocks, respecting marker type and length."""
    plain: list[str] = []
    blocks: list[tuple[str, str]] = []
    marker = ''
    info = ''
    body: list[str] = []
    for line in text.splitlines(keepends=True):
        if marker:
            if re.fullmatch(r' {0,3}' + re.escape(marker[0])
                            + '{' + str(len(marker)) + r',}[ \t]*\r?\n?', line):
                blocks.append((info, ''.join(body)))
                marker = ''
            else:
                body.append(line)
            plain.append('\n')
            continue
        match = FENCE.fullmatch(line.rstrip('\r\n'))
        if match and not (match['marker'][0] == '`' and '`' in match['info']):
            marker = match['marker']
            info = match['info'].strip()
            body = []
            plain.append('\n')
        else:
            plain.append(line)
    return ''.join(plain), blocks, bool(marker)


def prose(text: str) -> str:
    """Markdown prose, excluding examples inside backtick or tilde fences."""
    return markdown_parts(text)[0]


# Inline code spans hold examples, not links: ``<a href="...">`` in a sentence
# about HTML is a code sample. They are stripped for link discovery only; anchors
# still read them because a heading such as ``### `python-minimal/` `` slugs to
# the text inside the backticks.
INLINE_CODE = re.compile(r'(`+)(?!`).*?(?<!`)\1(?!`)')


def linkable(text: str) -> str:
    """Prose with inline code spans removed, for link discovery."""
    return INLINE_CODE.sub('', prose(text))


def anchors(text: str) -> set[str]:
    text = prose(text)
    found: set[str] = set()
    for match in HEADING.finditer(text):
        title = re.sub(r'\s+#+\s*$', '', match.group('title'))
        base = slugify(title)
        candidate = base
        number = 0
        while candidate in found:
            number += 1
            candidate = f'{base}-{number}'
        found.add(candidate)
    found |= set(EXPLICIT_ANCHOR.findall(text))
    return found


# A published site links its pages by route, not by filesystem path. Recognise
# the page directories of the common static-site layouts; a route is accepted
# only when the page source is really there.
PAGE_DIRS = {'pages', 'app'}
PAGE_SUFFIXES = ('.md', '.mdx', '.tsx', '.jsx', '.js', '.ts', '.html')
SKIP_WALK = {'node_modules', 'vendor', 'dist', 'build', 'out', 'target'}


def page_roots(root: Path) -> list[Path]:
    found = []
    for path in root.rglob('*'):
        depth = len(path.relative_to(root).parts)
        if depth > 4 or not path.is_dir() or path.name in SKIP_WALK:
            continue
        if path.name in PAGE_DIRS:
            found.append(path)
    return found


def route_target(root: Path, destination: str) -> Path | None:
    """Resolve a site route such as ``/getting-started`` to its page source.

    A published site links pages by route, not by filesystem path. Only consult
    this after the path has failed to resolve as a repository path, and only for
    route-shaped links, so it cannot mask a genuine broken link.
    """
    segments = [part for part in destination.strip('/').split('/') if part]
    if not segments:
        return None
    for pages in page_roots(root):
        base = pages.joinpath(*segments)
        for candidate in (base.with_suffix(suffix) for suffix in PAGE_SUFFIXES):
            if candidate.is_file():
                return candidate
        for suffix in PAGE_SUFFIXES:
            entry = base / f'index{suffix}'
            if entry.is_file():
                return entry
    return None


def local_target(root: Path, source: Path, link: str) -> Path | None:
    """Resolve repository links; schemes and protocol-relative URLs are external."""
    url = urlsplit(link.strip('<>'))
    if url.scheme or url.netloc:
        return None
    destination = unquote(url.path)
    target = (root / destination.lstrip('/') if destination.startswith('/')
              else source.parent / destination) if destination else source
    if not target.exists() and destination.startswith('/'):
        route = route_target(root, destination)
        if route is not None:
            return route
    # A link to a directory is valid on GitHub whether or not the directory has a
    # README. Resolve to the README only when one exists, so anchor and index
    # checks still see a document; otherwise keep the directory itself, which
    # ``check_links`` accepts because it exists.
    if target.is_dir() and (target / 'README.md').is_file():
        target = target / 'README.md'
    return target


def inside(root: Path, path: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def regular_document(root: Path, path: Path) -> bool:
    if not inside(root, path) or not path.is_file():
        return False
    current = root
    for part in path.relative_to(root).parts:
        current /= part
        if current.is_symlink():
            return False
    return True


def immutable_self_links(root: Path, documents: dict[Path, str]) -> dict[str, Path]:
    """Prove equivalent local pages behind immutable self-permalinks, offline.

    Batch metadata/content reads keep the Git process count independent of the
    number of links. Unknown commits, replacements and lazy fetches cannot supply
    evidence. Unsupported Git versions simply do not prove a permalink.
    """
    candidates: dict[str, tuple[str, str]] = {}
    for text in documents.values():
        for link in LINK.findall(text) + HTML_HREF.findall(text):
            url = urlsplit(link.strip('<>'))
            parts = unquote(url.path).lstrip('/').split('/', 4)
            if (url.scheme != 'https' or url.netloc != 'github.com' or len(parts) != 5
                    or parts[2] != 'blob' or not re.fullmatch(r'[0-9a-fA-F]{40}', parts[3])):
                continue
            path = root / parts[4]
            if (any(p in {'', '.', '..'} for p in parts[4].split('/'))
                    or any(ord(c) < 32 or c in '\\\x7f' for c in parts[4])
                    or path.resolve() not in documents or not regular_document(root, path)):
                continue
            candidates[link] = ('/'.join(parts[:2]), parts[3].lower() + ':' + parts[4])
    if not candidates:
        return {}
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    environment.update(GIT_NO_REPLACE_OBJECTS='1', GIT_OPTIONAL_LOCKS='0',
                       GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                       GIT_TERMINAL_PROMPT='0')

    def read(*args: str, data: bytes | None = None) -> bytes:
        return subprocess.check_output(
            ['git', '--no-optional-locks', '--no-lazy-fetch', *args], cwd=root,
            input=data, stderr=subprocess.DEVNULL, env=environment, timeout=10,
        )

    try:
        origin = read('config', '--local', '--get', 'remote.origin.url').decode().strip()
        if origin.startswith('git@github.com:'):
            repository = origin.removeprefix('git@github.com:').removesuffix('.git')
        elif origin.startswith('https://github.com/'):
            repository = origin.removeprefix('https://github.com/').removesuffix('.git')
        else:
            return {}
        matched = {link: spec for link, (repo, spec) in candidates.items()
                   if repo.casefold() == repository.casefold()}
        if not matched:
            return {}
        tracked_paths = set()
        for row in read('ls-files', '--stage', '-z').split(b'\0'):
            if row:
                metadata, path = row.split(b'\t', 1)
                mode, _, stage = metadata.split()
                if mode in {b'100644', b'100755'} and stage == b'0':
                    tracked_paths.add(path.decode())
        specs = sorted({spec for spec in matched.values()
                        if spec.split(':', 1)[1] in tracked_paths})
        revisions = sorted({spec.split(':', 1)[0] for spec in specs})
        requests = revisions + specs
        metadata = read('cat-file', '--batch-check=%(objectname)\t%(objecttype)\t%(objectmode)\t%(objectsize)',
                        data=''.join(x + '\n' for x in requests).encode()).splitlines()
        if len(metadata) != len(requests):
            return {}
        rows = {request: line.split(b'\t') for request, line in zip(requests, metadata)}
        valid_commits = {rev for rev in revisions if len(rows[rev]) == 4
                         and rows[rev][0].decode() == rev and rows[rev][1] == b'commit'}
        local: dict[str, tuple[bytes, bytes]] = {}
        for spec in specs:
            fields = rows[spec]
            if (spec.split(':', 1)[0] not in valid_commits or len(fields) != 4
                    or fields[1] != b'blob' or fields[2] not in {b'100644', b'100755'}):
                continue
            current = (root / spec.split(':', 1)[1]).read_bytes()
            if int(fields[3]) == len(current):
                local[spec] = (fields[0], current)
        objects = sorted({oid for oid, _ in local.values()})
        payload = read('cat-file', '--batch', data=b''.join(oid + b'\n' for oid in objects))
        contents = {}
        offset = 0
        for oid in objects:
            end = payload.index(b'\n', offset)
            actual, kind, size = payload[offset:end].split()
            size = int(size)
            offset = end + 1
            if (actual != oid or kind != b'blob' or len(payload) < offset + size + 1
                    or payload[offset + size:offset + size + 1] != b'\n'):
                return {}
            contents[oid] = payload[offset:offset + size]
            offset += size + 1
        if offset != len(payload):
            return {}
        return {link: (root / spec.split(':', 1)[1]).resolve()
                for link, spec in matched.items() if spec in local
                and contents[local[spec][0]] == local[spec][1]}
    except (OSError, ValueError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {}


def check_links(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        if (TEMPLATE_DIRS | VENDOR_DIRS) & set(path.relative_to(root).parts[:-1]):
            continue
        body = linkable(path.read_text(encoding='utf-8'))
        targets = [link for link in LINK.findall(body) + HTML_HREF.findall(body)
                   if not RUSTDOC_LINK.match(link)]
        for link in targets:
            target = local_target(root, path, link)
            if target is not None and (not inside(root, target) or not target.exists()):
                errors.append(f'{path.relative_to(root)}: broken link {link}')
    return errors


def check_anchors(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        if TEMPLATE_DIRS & set(path.relative_to(root).parts[:-1]):
            continue
        body = prose(path.read_text(encoding='utf-8'))
        for link in LINK.findall(body) + HTML_HREF.findall(body):
            if '#' not in link:
                continue
            fragment = urlsplit(link.strip('<>')).fragment
            target = local_target(root, path, link)
            if (target is None or not inside(root, target) or not target.is_file()
                    or target.suffix != '.md' or not fragment):
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
    if not regular_document(root, index):
        return ['docs/README.md: navigation index must be a regular repository file']
    documents = {p.resolve(): prose(p.read_text(encoding='utf-8'))
                 for p in files if p.suffix == '.md' and docs in p.parents
                 and regular_document(root, p)}
    permalinks = immutable_self_links(root, documents)
    reached: set[Path] = set()
    index_body = prose(index.read_text(encoding='utf-8'))
    pending = [index.resolve()]
    while pending:
        page = pending.pop()
        if page in reached:
            continue
        reached.add(page)
        body = documents.get(page, index_body)
        for link in LINK.findall(body) + HTML_HREF.findall(body):
            target = permalinks.get(link) or local_target(root, page, link)
            if target is not None and inside(root, target):
                target = target.resolve()
                if target in documents and target.is_file() and target not in reached:
                    pending.append(target)
    errors: list[str] = []
    for path in files:
        if docs not in path.parents or path.suffix != '.md':
            continue
        if path == index or path.name in SHARED_BOILERPLATE:
            continue
        if path.resolve() not in reached:
            errors.append(f'{path.relative_to(root)}: not linked from docs/README.md')
    return errors


def check_fences(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        if markdown_parts(path.read_text(encoding='utf-8'))[2]:
            errors.append(f'{path.relative_to(root)}: unbalanced code fence')
    return errors


def check_mermaid(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        if path.suffix != '.md' or not path.is_file():
            continue
        for info, body in markdown_parts(path.read_text(encoding='utf-8'))[1]:
            if info == 'mermaid' and not MERMAID_TYPES.search(body):
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
        try:
            project = tomllib.loads(pyproject.read_text(encoding='utf-8')).get('project', {})
        except tomllib.TOMLDecodeError as error:
            return [f'pyproject.toml: invalid TOML ({error})']
        version = project.get('version')
        if isinstance(version, str):
            declared.append(('pyproject.toml', version))
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
        if tag_key != newest:
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
