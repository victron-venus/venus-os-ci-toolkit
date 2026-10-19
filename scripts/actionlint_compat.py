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
        if not isinstance(concurrency, MappingNode) or field(concurrency, "queue") is None:
            continue
        keys = [key.value for key, _ in concurrency.value]
        queue = field(concurrency, "queue")
        cancel = field(concurrency, "cancel-in-progress")
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate concurrency key")
        if not isinstance(queue, ScalarNode) or queue.value not in {"single", "max"}:
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
        if concurrency.flow_style:
            if index + 1 < len(keys):
                following = concurrency.value[index + 1][0].start_mark.index
                end += source[end:following].index(",") + 1
            elif index:
                previous = concurrency.value[index - 1][1].end_mark.index
                start = previous + source[previous:start].rindex(",")
        spans.append((start, end))
    characters = list(source)
    for start, end in spans:
        characters[start:end] = [char if char in "\r\n" else " " for char in source[start:end]]
    return "".join(characters)


def main(paths: list[str] | None = None) -> int:
    paths = sys.argv[1:] if paths is None else paths
    if not paths:
        paths = sorted(str(path) for path in Path(".github/workflows").glob("*.y*ml"))
    if not paths:
        print("No workflow files found", file=sys.stderr)
        return 1
    result = 0
    for name in paths:
        try:
            source = Path(name).read_text()
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
