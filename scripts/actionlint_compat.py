#!/usr/bin/env python3
"""Validate GitHub concurrency queues before linting with older actionlint.

Only a validated queue field is blanked in the in-memory lint copy. Original
files, line/column positions, diagnostics and every other actionlint check remain
unchanged. Remove the compatibility path when the pinned linter supports queue.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import yaml
from yaml.nodes import MappingNode, ScalarNode


def field(node, key):
    if isinstance(node, MappingNode):
        return next((value for name, value in node.value if name.value == key), None)
    return None


def queue_span(concurrency: MappingNode, source: str) -> tuple[int, int]:
    """Validate the unsupported field before identifying its precise source span."""
    keys = [key.value for key, _ in concurrency.value]
    queue = field(concurrency, "queue")
    cancel = field(concurrency, "cancel-in-progress")
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate concurrency key")
    if not isinstance(queue, ScalarNode) or queue.value not in {"single", "max"} or queue.tag != "tag:yaml.org,2002:str":
        raise ValueError("concurrency queue must be literal single or max")
    if queue.value == "max" and cancel is not None and (
        not isinstance(cancel, ScalarNode) or cancel.value != "false"
    ):
        raise ValueError("concurrency queue max requires cancel-in-progress false")
    index = keys.index("queue")
    key, value = concurrency.value[index]
    if not (key.end_mark.index <= value.start_mark.index < value.end_mark.index <= concurrency.end_mark.index):
        raise ValueError("concurrency queue must not use a YAML alias")
    start, end = key.start_mark.index, value.end_mark.index
    if concurrency.flow_style and index + 1 < len(keys):
        following = concurrency.value[index + 1][0].start_mark.index
        end += source[end:following].index(",") + 1
    elif concurrency.flow_style and index:
        previous = concurrency.value[index - 1][1].end_mark.index
        start = previous + source[previous:start].rindex(",")
    return start, end


def lint_copy(source: str) -> str:
    """Blank only supported concurrency queue entries, preserving all offsets."""
    try:
        document = yaml.compose(source, Loader=yaml.BaseLoader)
    except yaml.YAMLError:
        return source  # The linter reports malformed YAML at its original path.
    scopes = [document]
    jobs = field(document, "jobs")
    if isinstance(jobs, MappingNode):
        scopes.extend(value for _, value in jobs.value)
    spans = []
    for scope in scopes:
        concurrency = field(scope, "concurrency")
        if isinstance(concurrency, MappingNode) and field(concurrency, "queue") is not None:
            spans.append(queue_span(concurrency, source))
    characters = list(source)
    for start, end in spans:
        characters[start:end] = [char if char in "\r\n" else " " for char in source[start:end]]
    return "".join(characters)


def workflow_paths(requested: list[str]) -> list[tuple[Path, str]]:
    """Select only discovered regular workflows from this checkout's directory."""
    directory = Path(".github/workflows")
    available = {}
    paths = []
    for path in sorted(directory.glob("*.y*ml")):
        if path.suffix not in {".yml", ".yaml"} or not path.is_file() or path.is_symlink():
            continue
        if directory.is_symlink() or directory.parent.is_symlink():
            raise ValueError("Workflow directory must not be a symbolic link")
        paths.append((path, str(path)))
        for label in (str(path), "./" + str(path), str(path.absolute())):
            available[label] = (path, label)
    if not paths or any(name not in available for name in requested):
        raise ValueError("Select existing regular files from .github/workflows")
    return [available[name] for name in requested] if requested else paths


def main(paths: list[str] | None = None) -> int:
    try:
        selected = workflow_paths(sys.argv[1:] if paths is None else paths)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    result = 0
    for path, name in selected:
        try:
            source = path.read_text()
            checked = lint_copy(source)
        except (OSError, ValueError) as error:
            print(f"{name}: {error}", file=sys.stderr)
            result = 1
            continue
        run = subprocess.run(
            ["actionlint", "-shellcheck=", "-pyflakes=", "-stdin-filename", name, "-"],
            input=checked, text=True, check=False,
        )
        result = max(result, run.returncode)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
