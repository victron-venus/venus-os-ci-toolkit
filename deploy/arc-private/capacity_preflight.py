#!/usr/bin/env python3
"""Read-only resource headroom diagnostics, not scheduler/runtime qualification."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import sys
import time

RESOURCES = ('cpu', 'memory', 'ephemeral-storage')
MAX_BYTES = 32 * 1024 * 1024
COVERAGE = 'all-pods-and-runtimeclasses;all-target-namespace-quotas-and-limitranges'
PATTERN = re.compile(r'([+]?(?:\d+(?:\.\d*)?|\.\d+))([eE][+-]?\d+|[numkKMGTPE]|[KMGTPE]i)?\Z')
UNITS = {'': Decimal(1), 'n': Decimal('1e-9'), 'u': Decimal('1e-6'),
         'm': Decimal('.001'), 'k': Decimal(1000), 'K': Decimal(1000)}
UNITS.update({s: Decimal(1000) ** i for i, s in enumerate('MGTPE', 2)})
UNITS.update({s + 'i': Decimal(1024) ** i for i, s in enumerate('KMGTPE', 1)})
BOUNDARIES = [
    'Resource headroom only; not scheduler, admission, runtime, or application qualification.',
    'Affinity, taints, topology, volume binding, images, admission webhooks, other ARC-created '
    'objects, and performance are not qualified.',
    'GETs are not atomic. Capacity can change immediately; this report does not reserve '
    'resources or authorize enabling a pool.',
]


class Unable(ValueError):
    """Incomplete or unsupported inputs must never produce a passing report."""


def require(condition, message):
    if not condition:
        raise Unable(message)


def quantity(value):
    require(isinstance(value, (str, int)) and not isinstance(value, bool), 'Invalid quantity type')
    require(len(str(value)) <= 64, 'Excessive quantity')
    match = PATTERN.fullmatch(str(value))
    require(match is not None, 'Unsupported quantity')
    number, suffix = match.groups()
    suffix = suffix or ''
    try:
        if suffix.startswith(('e', 'E')) and len(suffix) > 1:
            exponent = int(suffix[1:])
            require(abs(exponent) <= 30, 'Quantity exponent out of bounds')
            factor = Decimal(10) ** exponent
        else:
            factor = UNITS[suffix]
        with localcontext() as context:
            context.prec = 128
            result = Decimal(number) * factor
    except (InvalidOperation, KeyError) as exc:
        raise Unable('Unsupported quantity') from exc
    require(result.is_finite() and 0 <= result <= 2 ** 63 - 1, 'Quantity out of bounds')
    return result


def amount(resource, value):
    with localcontext() as context:
        context.prec = 128
        result = quantity(value) * (1000 if resource == 'cpu' else 1)
    require(result == result.to_integral_value(), 'Unsupported sub-unit quantity for ' + resource)
    return int(result)


def resource_map(raw):
    require(isinstance(raw, dict), 'Resource map must be an object')
    require(set(raw) <= set(RESOURCES), 'Unsupported resource dimension')
    return {key: amount(key, value) for key, value in raw.items()}


def plus(left, right):
    return {k: left.get(k, 0) + right.get(k, 0) for k in set(left) | set(right)}


def maximum(left, right):
    return {k: max(left.get(k, 0), right.get(k, 0)) for k in set(left) | set(right)}


def object_meta(obj, kind, namespace=None):
    require(isinstance(obj, dict) and obj.get('kind') == kind, 'Expected ' + kind)
    meta = obj.get('metadata', {})
    require(all(isinstance(meta.get(k), str) and meta[k] for k in ('name', 'uid', 'resourceVersion')),
            kind + ' lacks persisted identity/resourceVersion')
    if namespace is not None:
        require(meta.get('namespace') == namespace, kind + ' namespace mismatch')
    return meta


def listed(snapshot, key, kind, namespace=None):
    obj = snapshot.get(key, {})
    require(isinstance(obj, dict) and obj.get('kind') in (kind + 'List', 'List'), 'Missing ' + key + ' list')
    meta = obj.get('metadata', {})
    require(isinstance(meta.get('resourceVersion'), str) and meta['resourceVersion'], key + ' lacks list resourceVersion')
    require(not meta.get('continue') and not meta.get('remainingItemCount'), key + ' is paginated/incomplete')
    items = obj.get('items')
    require(isinstance(items, list) and len(items) <= 20000, key + ' items missing or excessive')
    seen = set()
    for item in items:
        identity = object_meta(item, kind, namespace)
        require(identity['uid'] not in seen, key + ' contains duplicate object')
        seen.add(identity['uid'])
    return items


def container_resources(container):
    require(isinstance(container, dict) and container.get('name'), 'Container lacks name')
    require(not container.get('resizePolicy'), 'In-place resize policy unsupported')
    raw = container.get('resources', {})
    require(isinstance(raw, dict) and set(raw) <= {'requests', 'limits'}, 'Container resource claims unsupported')
    req, lim = resource_map(raw.get('requests', {})), resource_map(raw.get('limits', {}))
    require(not (set(lim) - set(req)), 'Missing request with limit: admission defaulting not qualified')
    require(all(req[k] <= lim[k] for k in set(req) & set(lim)), 'Request exceeds limit')
    return req, lim


def pod_resources(pod, runtimes, *, candidate=False):
    spec, status = pod.get('spec', {}), pod.get('status', {})
    require(isinstance(spec, dict) and isinstance(status, dict), 'Malformed Pod')
    require(not spec.get('resources') and not spec.get('resourceClaims'), 'Pod-level resources/claims unsupported')
    require(not status.get('resize'), 'In-place resize unsupported')
    for entry in status.get('containerStatuses', []) + status.get('initContainerStatuses', []):
        require(not entry.get('allocatedResources') and not entry.get('resources'), 'Allocated/actual resize resources unsupported')
    containers, init = spec.get('containers'), spec.get('initContainers', [])
    require(isinstance(containers, list) and containers and isinstance(init, list), 'Missing containers')
    require(len(containers) + len(init) <= 100, 'Too many containers')
    req, lim, sides_req, sides_lim, init_req, init_lim = {}, {}, {}, {}, {}, {}
    names = set()
    for container in containers + init:
        require(isinstance(container, dict) and container.get('name') not in names, 'Invalid/duplicate container')
        names.add(container.get('name'))
    for container in containers:
        require(not container.get('restartPolicy'), 'Unexpected regular-container restartPolicy')
        cr, cl = container_resources(container)
        req, lim = plus(req, cr), plus(lim, cl)
    # Kubernetes PodRequests/PodLimits: each init overlaps all earlier restartable inits.
    for container in init:
        cr, cl = container_resources(container)
        restart = container.get('restartPolicy')
        require(restart in (None, 'Always'), 'Unsupported init restartPolicy')
        if restart == 'Always':
            sides_req, sides_lim = plus(sides_req, cr), plus(sides_lim, cl)
            req, lim = plus(req, cr), plus(lim, cl)
            stage_req, stage_lim = sides_req, sides_lim
        else:
            stage_req, stage_lim = plus(sides_req, cr), plus(sides_lim, cl)
        init_req, init_lim = maximum(init_req, stage_req), maximum(init_lim, stage_lim)
    req, lim = maximum(req, init_req), maximum(lim, init_lim)
    overhead = resource_map(spec.get('overhead', {}))
    # An admitted Pod's stored overhead is authoritative, even if its class changed.
    if candidate:
        runtime = spec.get('runtimeClassName')
        if runtime:
            require(runtime in runtimes, 'RuntimeClass missing from snapshot')
            expected = resource_map(runtimes[runtime].get('overhead', {}).get('podFixed', {}))
            require('overhead' not in spec or overhead == expected, 'Candidate overhead differs from RuntimeClass')
            overhead = expected
        else:
            require(not overhead, 'Candidate overhead without RuntimeClass')
    req = plus(req, overhead)
    lim = {k: v + (overhead.get(k, 0) if v else 0) for k, v in lim.items()}
    return req, lim


def quota_usage(req, lim, *, terminal=False):
    result = {'count/pods': 1}
    if not terminal:
        result['pods'] = 1
        for resource in RESOURCES:
            result[resource] = req.get(resource, 0)
            result['requests.' + resource] = req.get(resource, 0)
            result['limits.' + resource] = lim.get(resource, 0)
    return result


def quota_amount(key, value):
    resource = key.removeprefix('requests.').removeprefix('limits.')
    require(resource in RESOURCES or key in ('pods', 'count/pods'), 'Unsupported quota dimension: ' + key)
    return amount(resource, value)


def limit_checks(candidate, ranges, blockers):
    for item in ranges:
        name = item['metadata']['name']
        limits = item.get('spec', {}).get('limits')
        require(isinstance(limits, list) and limits, 'LimitRange has no limits')
        for rule in limits:
            require(rule.get('type') == 'Container', 'Unsupported LimitRange type: ' + str(rule.get('type')))
            require(set(rule) <= {'type', 'min', 'max', 'default', 'defaultRequest', 'maxLimitRequestRatio'}, 'Unsupported LimitRange fields')
            mins, maxes = resource_map(rule.get('min', {})), resource_map(rule.get('max', {}))
            defaults = [resource_map(rule.get('defaultRequest', {})), resource_map(rule.get('default', {}))]
            ratios = rule.get('maxLimitRequestRatio', {})
            require(isinstance(ratios, dict) and set(ratios) <= set(RESOURCES), 'Unsupported LimitRange ratio')
            for container in candidate['spec']['containers'] + candidate['spec'].get('initContainers', []):
                req, lim = container_resources(container)
                require(not (set(defaults[0]) - set(req) or set(defaults[1]) - set(lim)), 'LimitRange would default candidate resources')
                for key, value in mins.items():
                    if key not in req or req[key] < value or (key in lim and lim[key] < value):
                        blockers.append(f'LimitRange {name}: {container["name"]} below minimum {key}')
                for key, value in maxes.items():
                    if key not in lim or lim[key] > value or req.get(key, 0) > value:
                        blockers.append(f'LimitRange {name}: {container["name"]} exceeds/misses maximum {key}')
                for key, raw in ratios.items():
                    ratio = quantity(raw)
                    with localcontext() as context:
                        context.prec = 128
                        exceeds = key in lim and Decimal(lim[key]) > ratio * req.get(key, 0)
                    if key not in lim or req.get(key, 0) == 0 or exceeds:
                        blockers.append(f'LimitRange {name}: {container["name"]} violates {key} limit/request ratio')


def timestamp(value):
    require(isinstance(value, str), 'Missing capture time')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise Unable('Invalid capture time') from exc
    require(parsed.tzinfo is not None, 'Capture time lacks timezone')
    return parsed


def analyze(snapshot, candidate, *, now=None):
    now = now or datetime.now(timezone.utc)
    report = {'schema': 1, 'verdict': 'unable_to_qualify', 'blockers': [], 'unknowns': [],
              'node_resources': {}, 'quotas': [], 'pending_reservations': [], 'boundaries': BOUNDARIES}
    try:
        require(isinstance(snapshot, dict) and type(snapshot.get('schema')) is int and snapshot['schema'] == 1
                and snapshot.get('coverage') == COVERAGE, 'Unsupported/incomplete snapshot coverage')
        start, end = timestamp(snapshot.get('started_at')), timestamp(snapshot.get('finished_at'))
        require(0 <= (end - start).total_seconds() <= 30, 'Capture duration exceeds 30 seconds')
        require(0 <= (now - end).total_seconds() <= 60, 'Snapshot stale or from the future')
        version = snapshot.get('server_version', {}).get('gitVersion')
        require(isinstance(version, str) and re.fullmatch(r'v1\.\d+\.\d+[-+\w.]*', version), 'Missing/unsupported server version')
        report['server_version'] = version
        namespace, node = snapshot.get('namespace'), snapshot.get('node')
        require(isinstance(namespace, str) and namespace, 'Missing namespace')
        node_meta = object_meta(node, 'Node')
        pods = listed(snapshot, 'pods', 'Pod')
        quotas = listed(snapshot, 'quotas', 'ResourceQuota', namespace)
        ranges = listed(snapshot, 'limitranges', 'LimitRange', namespace)
        runtime_list = listed(snapshot, 'runtimeclasses', 'RuntimeClass')
        runtimes = {x['metadata']['name']: x for x in runtime_list}
        require(len(runtimes) == len(runtime_list), 'Duplicate RuntimeClass name')
        require(candidate.get('kind') == 'Pod' and candidate.get('apiVersion') == 'v1', 'Candidate must be a v1 Pod')
        require(candidate.get('metadata', {}).get('namespace') == namespace, 'Candidate namespace mismatch')
        require(not candidate.get('metadata', {}).get('uid') and not candidate.get('status'), 'Candidate is already admitted')
        require(not candidate.get('spec', {}).get('nodeName'), 'Candidate must not bypass scheduler with nodeName')
        creq, clim = pod_resources(candidate, runtimes, candidate=True)
        report['candidate_requests'], report['candidate_limits'] = creq, clim
        report['units'] = {'cpu': 'millicores', 'memory': 'bytes', 'ephemeral-storage': 'bytes', 'pods': 'count'}
        limit_checks(candidate, ranges, report['blockers'])
        allocatable = node.get('status', {}).get('allocatable', {})
        require(all(k in allocatable for k in (*RESOURCES, 'pods')), 'Node allocatable dimensions incomplete')
        allocated = {k: amount(k, allocatable[k]) for k in (*RESOURCES, 'pods')}
        used, observed_quota = {'pods': 0}, {}
        for pod in pods:
            phase = pod.get('status', {}).get('phase')
            require(phase in ('Pending', 'Running', 'Unknown', 'Succeeded', 'Failed'), 'Pod phase missing or unsupported')
            terminal = phase in ('Succeeded', 'Failed')
            pns = pod['metadata'].get('namespace')
            require(isinstance(pns, str) and pns, 'Pod namespace missing')
            assigned_here = pod.get('spec', {}).get('nodeName') == node_meta['name']
            req, lim = ({}, {}) if terminal or not (assigned_here or pns == namespace) else pod_resources(pod, runtimes)
            if assigned_here and not terminal:
                used = plus(used, plus(req, {'pods': 1}))
            if pns == namespace:
                observed_quota = plus(observed_quota, quota_usage(req, lim, terminal=terminal))
                if phase == 'Pending':
                    report['pending_reservations'].append({'pod': pod['metadata']['name'],
                        'assigned_node': pod.get('spec', {}).get('nodeName'), 'requests': req, 'limits': lim})
        for key, capacity in allocated.items():
            requested = 1 if key == 'pods' else creq.get(key, 0)
            free = capacity - used.get(key, 0)
            report['node_resources'][key] = {'allocatable': capacity, 'assigned': used.get(key, 0),
                                            'available': free, 'candidate': requested}
            if requested > free:
                report['blockers'].append(f'Node {node_meta["name"]}: {key} needs {requested}, only {free} available')
        candidate_quota = quota_usage(creq, clim)
        for quota in quotas:
            name = quota['metadata']['name']
            try:
                spec, status = quota.get('spec', {}), quota.get('status', {})
                require(not spec.get('scopes') and not spec.get('scopeSelector'), f'Quota {name}: scoped quotas unsupported')
                hard, declared, consumed = status.get('hard'), spec.get('hard'), status.get('used')
                require(all(isinstance(x, dict) for x in (hard, declared, consumed)) and hard, f'Quota {name}: status incomplete')
                hard = {k: quota_amount(k, v) for k, v in hard.items()}
                require(hard == {k: quota_amount(k, v) for k, v in declared.items()}, f'Quota {name}: status.hard has not converged to spec.hard')
                require(set(consumed) == set(hard), f'Quota {name}: status.used incomplete')
                for key, value in hard.items():
                    current = quota_amount(key, consumed[key])
                    require(current >= observed_quota.get(key, 0), f'Quota {name}: status.used undercounts observed Pods for {key}')
                    dimension = key.removeprefix('requests.').removeprefix('limits.')
                    if dimension in ('cpu', 'memory'):
                        group = 'limits' if key.startswith('limits.') else 'requests'
                        for container in candidate['spec']['containers'] + candidate['spec'].get('initContainers', []):
                            require(dimension in container.get('resources', {}).get(group, {}), f'Quota {name}: candidate lacks explicit {group}.{dimension}')
                    incoming = candidate_quota.get(key, 0)
                    report['quotas'].append({'quota': name, 'resource': key, 'hard': value, 'used': current,
                        'observed_pods': observed_quota.get(key, 0), 'candidate': incoming, 'available': value - current})
                    if current + incoming > value:
                        report['blockers'].append(f'Quota {name}: {key} needs {incoming}, only {value - current} available')
            except Unable as exc:
                report['unknowns'].append(str(exc))
        report['verdict'] = 'unable_to_qualify' if report['unknowns'] else ('blocked' if report['blockers'] else 'resource_headroom_passes')
    except (Unable, TypeError, KeyError, AttributeError) as exc:
        report['unknowns'].append(str(exc))
    return report


def parse_json(raw):
    require(len(raw) <= MAX_BYTES, 'JSON exceeds 32 MiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'Duplicate JSON key')
            result[key] = value
        return result
    def invalid_constant(_value):
        raise Unable('Non-finite JSON number')
    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def load_json(path):
    # O_NONBLOCK prevents opening a FIFO from hanging before fstat can reject it.
    # Bound the actual read, not only a racy pre-open size observation.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        require(stat.S_ISREG(os.fstat(stream.fileno()).st_mode), 'Input must be a regular file')
        return parse_json(stream.read(MAX_BYTES + 1))


def bounded_get(command, deadline):
    """Bound both pipes and wall time, including a shared capture deadline."""
    require(time.monotonic() < deadline, 'Capture deadline exceeded')
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    buffers = {'out': bytearray(), 'err': bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ, 'out')
            selector.register(proc.stderr, selectors.EVENT_READ, 'err')
            while selector.get_map():
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'Capture deadline exceeded')
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(chunk)
                    require(len(buffers[key.data]) <= (MAX_BYTES if key.data == 'out' else 1024 * 1024),
                            'kubectl output limit exceeded')
        require(proc.wait(timeout=max(.001, deadline - time.monotonic())) == 0, 'Read-only kubectl GET failed')
        require(time.monotonic() < deadline, 'Capture deadline exceeded')
        return parse_json(buffers['out'])
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def project_object(obj):
    """Persist accounting fields only; no environment, command, image, or annotations."""
    if 'items' in obj:
        return {**{k: obj[k] for k in ('kind', 'apiVersion', 'metadata') if k in obj},
                'items': [project_object(x) for x in obj['items']]}
    meta = obj.get('metadata', {})
    result = {k: obj[k] for k in ('kind', 'apiVersion') if k in obj}
    result['metadata'] = {k: meta[k] for k in ('name', 'namespace', 'uid', 'resourceVersion', 'deletionTimestamp') if k in meta}
    kind = obj.get('kind')
    if kind == 'Pod':
        spec, status = obj.get('spec', {}), obj.get('status', {})
        result['spec'] = {k: spec[k] for k in ('nodeName', 'runtimeClassName', 'overhead', 'resources', 'resourceClaims') if k in spec}
        for group in ('containers', 'initContainers'):
            if group in spec:
                result['spec'][group] = [{k: c[k] for k in ('name', 'resources', 'restartPolicy', 'resizePolicy') if k in c} for c in spec[group]]
        result['status'] = {k: status[k] for k in ('phase', 'resize') if k in status}
        for group in ('containerStatuses', 'initContainerStatuses'):
            if group in status:
                result['status'][group] = [{k: c[k] for k in ('allocatedResources', 'resources') if k in c} for c in status[group]]
    elif kind == 'Node':
        result['status'] = {k: obj.get('status', {})[k] for k in ('allocatable', 'nodeInfo') if k in obj.get('status', {})}
    else:
        result.update({k: obj[k] for k in ('spec', 'status', 'overhead') if k in obj})
    return result


def capture(context, node, namespace):
    for value in (context, node, namespace):
        require(isinstance(value, str) and value and not value.startswith('-'), 'Invalid capture target')
    queries = {'server_version': ['--raw', '/version'], 'node': ['node', node],
               'pods': ['pods', '--all-namespaces'], 'quotas': ['resourcequotas', '--namespace', namespace],
               'limitranges': ['limitranges', '--namespace', namespace], 'runtimeclasses': ['runtimeclasses']}
    result = {'schema': 1, 'coverage': COVERAGE, 'namespace': namespace,
              'started_at': datetime.now(timezone.utc).isoformat()}
    deadline = time.monotonic() + 30
    for key, args in queries.items():
        command = ['kubectl', '--context', context, '--request-timeout=15s', 'get', *args]
        if key != 'server_version':
            command += ['--chunk-size=0', '--output=json']
        try:
            data = bounded_get(command, deadline)
        except (Unable, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise Unable('Capture ' + key + ': ' + str(exc)) from exc
        result[key] = data if key == 'server_version' else project_object(data)
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    require(time.monotonic() < deadline, 'Capture deadline exceeded')
    object_meta(result['node'], 'Node')
    require(result['node']['metadata']['name'] == node, 'Captured node identity mismatch')
    require(isinstance(result['server_version'].get('gitVersion'), str), 'Server version missing')
    for key, kind, ns in [('pods', 'Pod', None), ('quotas', 'ResourceQuota', namespace),
                          ('limitranges', 'LimitRange', namespace), ('runtimeclasses', 'RuntimeClass', None)]:
        listed(result, key, kind, ns)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    collect = subs.add_parser('capture', help='GET only; create a new private snapshot file')
    for key in ('context', 'node', 'namespace'):
        collect.add_argument('--' + key, required=True)
    collect.add_argument('--output', type=Path, required=True)
    check = subs.add_parser('check', help='Offline bounded snapshot analysis')
    check.add_argument('--snapshot', type=Path, required=True)
    check.add_argument('--candidate', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            result = capture(args.context, args.node, args.namespace)
            raw = (json.dumps(result, indent=2) + '\n').encode()
            require(len(raw) <= MAX_BYTES, 'Combined snapshot exceeds 32 MiB')
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            print(json.dumps({'snapshot_written': True, 'mode': '0600', 'cluster_mutations': False}))
            return 0
        result = analyze(load_json(args.snapshot), load_json(args.candidate))
    except (Unable, OSError, ValueError, RecursionError, subprocess.TimeoutExpired) as exc:
        result = {'verdict': 'unable_to_qualify', 'unknowns': [str(exc)], 'boundaries': BOUNDARIES}
    print(json.dumps(result, indent=2))
    return {'resource_headroom_passes': 0, 'blocked': 1, 'unable_to_qualify': 2}[result['verdict']]


if __name__ == '__main__':
    sys.exit(main())
