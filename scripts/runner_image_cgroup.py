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
    require(value == '/' or all(part and part not in {'.', '..'} for part in value[1:].split('/')),
            'Unsafe cgroup path')
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


def owned_root(container_id, parent, private_path):
    """Correlate one stable host PID with PID 1 inside its private namespace."""
    require(re.fullmatch(r'[0-9a-f]{64}', container_id), 'Invalid Docker container identity')
    host = group_path('0::' + parent['cgroup'])
    private = group_path('0::' + private_path)
    if private == '/':
        root = host
    else:
        require(host.endswith(private), 'Host/private cgroup suffix differs')
        root = host[:-len(private)]
    require(root and root != '/' and root.rsplit('/', 1)[-1] in
            {container_id, 'docker-' + container_id + '.scope'},
            'Cgroup root is not owned by the Docker container')
    group_path('0::' + root)
    return root


def validate_binding(binding):
    root = owned_root(binding['container_id'], binding['builder_process'], binding['private_process_path'])
    require(root == binding['owned_root'], 'Owned cgroup root changed')
    return root


def read_limits(group, cgroups, result):
    require((cgroups / 'cgroup.controllers').is_file(), 'Unified cgroup v2 mount required')
    directory = cgroups / group.lstrip('/')
    for name in ('cpu.max', 'memory.max', 'memory.swap.max'):
        result[name] = (directory / name).read_text().strip()


def sample(pid, binding, proc=Path('/proc'), cgroups=Path('/sys/fs/cgroup'), observe=None):
    # Emit partial observations even when validation fails or a cgroup file is
    # unavailable. The caller keeps this journal alongside failure artifacts.
    record = {'kind': 'limits', 'container_id': binding['container_id'],
              'owned_root': binding['owned_root'], 'leaf_limits': {}, 'root_limits': {}}
    try:
        current = process_identity(pid, proc)
        record['builder_process'] = current
        require(current == binding['builder_process'], 'Owned builder process changed')
        root = validate_binding(binding)
        read_limits(current['cgroup'], cgroups, record['leaf_limits'])
        read_limits(root, cgroups, record['root_limits'])
        require(record['root_limits'] == {'cpu.max': '200000 100000',
                'memory.max': str(4 * 1024**3), 'memory.swap.max': '0'},
                'Effective builder cgroup limits differ')
        record['accepted'] = True
        return current, record['root_limits']
    finally:
        if observe is not None:
            observe(record)


def require_run_inside(child, binding):
    root = validate_binding(binding)
    group = group_path('0::' + child['cgroup'])
    require(group == root or group.startswith(root + '/'),
            'BuildKit RUN is outside the bounded builder cgroup')
    require(child['pid'] != binding['builder_process']['pid'], 'RUN marker belongs to builder itself')


def run_processes(marker, binding, proc=Path('/proc')):
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
        require_run_inside(current, binding)
        found.append(current)
    require(len(found) <= 1, 'Ambiguous marked RUN process')
    return found
