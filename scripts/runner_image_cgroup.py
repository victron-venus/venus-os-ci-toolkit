"""Read-only evidence that a BuildKit RUN stays inside its bounded cgroup."""
from pathlib import Path
import re


def require(condition, message):
    if not condition:
        raise ValueError(message)


def group_path(text):
    lines = text.strip().splitlines()
    require(len(lines) == 1 and lines[0].startswith('0::/'), 'Unified cgroup v2 required')
    value = lines[0][3:]
    require('..' not in value.split('/') and '//' not in value, 'Unsafe cgroup path')
    return value


def process_identity(pid, proc=Path('/proc')):
    require(type(pid) is int and pid > 0, 'Invalid process identity')
    path = proc / str(pid)
    before = (path / 'stat').read_text()
    group = group_path((path / 'cgroup').read_text())
    after = (path / 'stat').read_text()
    # stat field 22 is process starttime; comm (field 2) may contain spaces.
    def start(raw):
        fields = raw.rsplit(') ', 1)[1].split()
        require(len(fields) >= 20 and fields[19].isdigit(), 'Invalid process starttime')
        return fields[19]
    require(start(before) == start(after), 'Process identity changed')
    return {'pid': pid, 'starttime_ticks': start(after), 'cgroup': group}


def enforce(parent, cgroups=Path('/sys/fs/cgroup')):
    require(parent['cgroup'] != '/', 'Builder must not use the host root cgroup')
    directory = cgroups / parent['cgroup'].lstrip('/')
    require((cgroups / 'cgroup.controllers').is_file(), 'Unified cgroup v2 mount required')
    limits = {name: (directory / name).read_text().strip()
              for name in ('cpu.max', 'memory.max', 'memory.swap.max')}
    require(limits == {'cpu.max': '200000 100000', 'memory.max': str(4 * 1024**3),
                       'memory.swap.max': '0'}, 'Effective builder cgroup limits differ')
    return limits


def sample(pid, expected=None, proc=Path('/proc'), cgroups=Path('/sys/fs/cgroup')):
    current = process_identity(pid, proc)
    if expected is not None:
        require(current == expected, 'Owned builder process changed')
    return current, enforce(current, cgroups)


def run_processes(marker, parent, proc=Path('/proc')):
    require(re.fullmatch(r'runner-proof-[0-9a-f]{32}', marker), 'Invalid RUN marker')
    found = []
    for directory in proc.iterdir():
        if not directory.name.isdigit():
            continue
        try:
            # An exact argv component prevents matching BuildKit log text or
            # the Python observer. No arbitrary command lines enter evidence.
            if marker.encode() not in (directory / 'cmdline').read_bytes().split(b'\0'):
                continue
            current = process_identity(int(directory.name), proc)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        require(current['cgroup'] == parent['cgroup'] or
                current['cgroup'].startswith(parent['cgroup'] + '/'),
                'BuildKit RUN is outside the bounded builder cgroup')
        require(current['pid'] != parent['pid'], 'RUN marker belongs to builder itself')
        found.append(current)
    require(len(found) <= 1, 'Ambiguous marked RUN process')
    return found
