#!/usr/bin/env python3
"""Describe ARC routing and observed capacity from sanitized snapshots; never query or mutate services."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat

LIMIT = 8_000_000
PREFIX = 'actions.github.com/'
NAME = re.compile(r'[A-Za-z0-9_.-]{1,100}')
REPO = re.compile(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}')


def load(path):
    """Read an operator-selected regular file, never a terminal link or device.

    CLI paths are intentional local input, not remote/LLM requests. The operator
    controls their parent directories; this utility does not grant file access
    to another principal or return raw input contents.
    """
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('Snapshot must be a regular file')
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('Snapshot exceeds size limit')
    return json.loads(raw, object_pairs_hook=unique)


def timestamp(value):
    """Require an explicit timezone rather than assuming a local clock."""
    if not isinstance(value, str):
        raise ValueError('Snapshot timestamp is missing')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('Snapshot timestamp lacks timezone')
    return result


def collection(snapshot, name):
    """An absent/partial collection is unknown, never proof of absence."""
    value = snapshot.get('collections', {}).get(name, {})
    if not isinstance(value, dict) or not isinstance(value.get('items', []), list):
        raise ValueError('Invalid snapshot collection')
    items = value.get('items', [])
    if len(items) > 10000 or not all(isinstance(item, dict) for item in items):
        raise ValueError('Invalid snapshot items')
    return value


def covers(value, namespace=None):
    """Only machine-readable collection scope can prove missing objects."""
    metadata = value.get('source_metadata', {})
    remaining = metadata.get('remainingItemCount', 0)
    if (value.get('complete') is not True or not isinstance(value.get('items'), list)
            or metadata.get('continue') or type(remaining) is not int or remaining != 0):
        return False
    if value.get('scope') == 'cluster-all':
        return True
    prefixes = value.get('namespace_prefixes', [])
    return (namespace is not None and isinstance(prefixes, list)
            and any(isinstance(prefix, str) and prefix and namespace.startswith(prefix) for prefix in prefixes))


def linked(item, pool):
    """Names alone collide across namespaces; require both ARC linking labels."""
    labels = item.get('metadata', {}).get('labels', {})
    meta = pool.get('metadata', {})
    return (labels.get(PREFIX + 'scale-set-name') == meta.get('name')
            and labels.get(PREFIX + 'scale-set-namespace') == meta.get('namespace'))


def pool_labels(pool):
    """Custom labels are authoritative; absent labels use the scale-set name."""
    spec = pool.get('spec', {})
    labels = spec.get('runnerScaleSetLabels', [spec.get('runnerScaleSetName', pool.get('metadata', {}).get('name'))])
    if not isinstance(labels, list) or not labels or not all(isinstance(label, str) and NAME.fullmatch(label) for label in labels):
        return None
    return set(labels)


def scope(pool, repository):
    """Organization scope is eligible but does not establish runner-group access."""
    url = pool.get('spec', {}).get('githubConfigUrl')
    if not isinstance(url, str):
        return 'unknown'
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?)/?', url)
    if not match:
        return 'unknown'
    target = match[1].casefold()
    if target == repository.casefold():
        return 'repository'
    if target == repository.split('/')[0].casefold():
        return 'organization-access-unverified'
    return 'different'


def ready(pod):
    """A running phase alone does not establish Pod readiness."""
    status = pod.get('status', {})
    states = status.get('containerStatuses', [])
    return (not pod.get('metadata', {}).get('deletionTimestamp')
            and status.get('phase') == 'Running'
            and any(c.get('type') == 'Ready' and c.get('status') == 'True' for c in status.get('conditions', []))
            and bool(states) and all(c.get('ready') is True for c in states))


def admission_state(spec):
    """Describe configured capacity without defaulting omitted fields to zero."""
    maximum = spec.get('maxRunners')
    if 'maxRunners' not in spec:
        return 'unspecified'
    if type(maximum) is not int or maximum < 0:
        return 'unknown'
    if maximum == 0:
        return 'held'
    return 'enabled'


def listener_capacity(spec, listener_spec):
    """An omitted ARC listener minimum represents zero, never an arbitrary minimum."""
    maximum, minimum = spec.get('maxRunners'), spec.get('minRunners')
    listener_maximum = listener_spec.get('maxRunners')
    if type(listener_maximum) is not int or type(maximum) is not int:
        return 'unknown'
    if type(minimum) is not int or minimum < 0:
        return 'pending'
    if 'minRunners' not in listener_spec:
        minimum_matches = minimum == 0
    else:
        listener_minimum = listener_spec['minRunners']
        minimum_matches = type(listener_minimum) is int and listener_minimum == minimum
    if listener_maximum == maximum and minimum_matches:
        return 'matched'
    return 'pending'


def owned_by(item, kind, uid=None):
    """Bind a controller-owned object to its exact owner when known."""
    return any(owner.get('kind') == kind and owner.get('controller') is True
               and (uid is None or owner.get('uid') == uid)
               for owner in item.get('metadata', {}).get('ownerReferences', []))


def listener_pod_state(listener, pool, listeners, pods):
    """Keep observed ready Pods separate from proven unique ready listeners."""
    meta = listener['metadata']
    owned = [pod for pod in pods.get('items', [])
             if pod.get('metadata', {}).get('namespace') == meta['namespace']
             and linked(pod, pool) and owned_by(pod, 'AutoscalingListener', meta['uid'])]
    if meta.get('deletionTimestamp'):
        return 'terminating'
    if len(owned) > 1:
        return 'ambiguous'
    if not owned:
        return 'pod-absent' if covers(pods, meta['namespace']) else 'unknown'
    if not ready(owned[0]):
        return 'not-ready'
    if covers(listeners) and covers(pods, meta['namespace']):
        return 'ready'
    return 'observed-ready'


def describe_listener(pool, listeners, pods):
    """Require exact scale-set links and scope before using listener evidence."""
    result = {'listener': 'unknown', 'registration': 'unknown', 'listener_limit_reconciliation': 'unknown'}
    candidates = [item for item in listeners.get('items', []) if linked(item, pool)]
    if not candidates:
        if covers(listeners):
            result['listener'] = 'absent'
        return result
    if len(candidates) > 1:
        result['listener'] = 'ambiguous'
        return result
    listener = candidates[0]
    meta, spec = listener.get('metadata', {}), listener.get('spec', {})
    if not all(isinstance(meta.get(key), str) and meta[key] for key in ('name', 'namespace', 'uid')):
        return result
    if spec.get('githubConfigUrl') != pool.get('spec', {}).get('githubConfigUrl'):
        result['listener'] = 'scope-mismatch'
        return result
    registered = spec.get('runnerScaleSetId')
    if type(registered) is int and registered > 0:
        result['registration'] = 'observed-scale-set-id'
    result['listener_limit_reconciliation'] = listener_capacity(pool.get('spec', {}), spec)
    result['listener'] = listener_pod_state(listener, pool, listeners, pods)
    return result


def pool_state(meta, current, result):
    """Only a complete, reconciled observation can describe idle scale-to-zero."""
    if meta.get('deletionTimestamp'):
        return 'terminating'
    if result['admission'] == 'held':
        return 'held'
    operational = (result['admission'] == 'enabled' and current
                   and result['listener'] == 'ready' and result['registration'] != 'unknown'
                   and result['listener_limit_reconciliation'] == 'matched')
    if not operational:
        return 'unknown'
    count, minimum = result['worker_pods'], result['min_runners']
    if count == 0 and type(minimum) is int and minimum == 0:
        return 'idle-scale-to-zero'
    if count is not None and count > 0:
        return 'workers-observed'
    return 'unknown'


def describe_pool(pool, repository, listeners, pods):
    """Keep configuration, registration evidence, admission and workers separate."""
    meta, spec = pool.get('metadata', {}), pool.get('spec', {})
    namespace, name = meta.get('namespace'), meta.get('name')
    if not all(isinstance(value, str) and NAME.fullmatch(value) for value in (namespace, name)):
        raise ValueError('Invalid scale-set identity')
    workers = [pod for pod in pods.get('items', [])
               if pod.get('metadata', {}).get('namespace') == namespace
               and linked(pod, pool) and owned_by(pod, 'EphemeralRunner')]
    generation = meta.get('generation')
    observed = pool.get('status', {}).get('observedGeneration')
    current = type(generation) is int and generation > 0 and type(observed) is int and observed == generation
    result = {'namespace': namespace, 'name': name, 'scope': scope(pool, repository),
              'admission': admission_state(spec), 'min_runners': spec.get('minRunners'),
              'max_runners': spec.get('maxRunners'), 'controller_observed_generation': current,
              'worker_pods': len(workers) if covers(pods, namespace) else None,
              'observed_worker_pods': len(workers), 'ready_worker_pods': sum(ready(pod) for pod in workers)}
    result.update(describe_listener(pool, listeners, pods))
    result['state'] = pool_state(meta, current, result)
    return result


def selector_state(labels):
    """Differentiate missing, unknown and malformed label arrays."""
    if labels is None:
        return 'unknown'
    if labels == []:
        return 'missing'
    if not isinstance(labels, list) or not all(isinstance(label, str) and NAME.fullmatch(label) for label in labels):
        return 'invalid'
    if len(labels) != len(set(labels)):
        return 'invalid'
    return 'configured'


def configuration_state(matches, unknown, elsewhere):
    """Known duplicate matches stay ambiguous even in an incomplete census."""
    if len(matches) > 1:
        return 'ambiguous'
    if unknown:
        return 'unknown'
    if matches:
        return 'matched'
    if elsewhere:
        return 'scope-mismatch'
    return 'absent'


def matching_pools(repository, labels, collections):
    """Inspect label and repository scope without assuming organization access."""
    pools, listeners, pods = collections
    matches, elsewhere = [], False
    unknown = not covers(pools)
    for pool in pools.get('items', []):
        available = pool_labels(pool)
        if available is None:
            unknown = True
            continue
        if not set(labels) <= available:
            continue
        binding = scope(pool, repository)
        if binding == 'different':
            elsewhere = True
        elif binding == 'unknown':
            unknown = True
        else:
            matches.append(describe_pool(pool, repository, listeners, pods))
    return matches, configuration_state(matches, unknown, elsewhere)


def profile_result(entry, profile, labels, collections, fresh):
    """Summarize one declared profile without interpreting arbitrary workflow code."""
    if not NAME.fullmatch(profile):
        raise ValueError('Invalid profile')
    repository = entry['repository']
    selector = selector_state(labels)
    matches, configuration = [], 'unknown'
    if selector == 'configured':
        matches, configuration = matching_pools(repository, labels, collections)
    state = matches[0]['state'] if configuration == 'matched' else 'unknown'
    demand = entry.get('queued_jobs', {}).get(profile)
    if demand is not None and (type(demand) is not int or demand < 0):
        raise ValueError('Invalid queued job count')
    if state == 'held' and demand:
        state = 'held-with-queued-demand'
    mode = entry.get('mode')
    return {'repository': repository, 'profile': profile,
            'mode': mode if mode in ('', 'github', 'self-hosted', 'k3s', None) else 'invalid',
            'self_hosted_selected': mode in ('self-hosted', 'k3s'),
            'selector': selector, 'configuration': configuration, 'pools': matches,
            'queued_jobs': demand, 'observed_state': state, 'evidence_fresh': fresh,
            'qualification': 'not-assessed'}


def repository_entries(repositories):
    """Validate explicit repository/profile inventory before emitting results."""
    entries = repositories.get('repositories')
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError('Invalid repository inventory')
    seen = set()
    for entry in entries:
        repository = entry.get('repository')
        if not isinstance(repository, str) or not REPO.fullmatch(repository) or repository.casefold() in seen:
            raise ValueError('Invalid or repeated repository')
        seen.add(repository.casefold())
        if not isinstance(entry.get('profiles'), dict):
            raise ValueError('Explicit expected profiles are required')
    return entries


def evaluate(repositories, cluster, *, now=None, max_age_seconds=900):
    """Evaluate supplied evidence only; no result certifies runtime qualification."""
    if not all(isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1
               for value in (repositories, cluster)):
        raise ValueError('Unsupported snapshot schema')
    now = now or datetime.now(timezone.utc)
    ages = [(now - timestamp(value.get('observed_at'))).total_seconds() for value in (repositories, cluster)]
    fresh = all(0 <= age <= max_age_seconds for age in ages)
    collections = tuple(collection(cluster, key) for key in ('asrs', 'listeners', 'pods'))
    entries = repository_entries(repositories)
    rows = [profile_result(entry, profile, labels, collections, fresh)
            for entry in entries for profile, labels in entry['profiles'].items()]
    return {'schema': 1, 'read_only': True, 'evidence_fresh': fresh,
            'repository_inventory_complete': repositories.get('complete') is True,
            'profiles_not_declared': [entry['repository'] for entry in entries if not entry['profiles']],
            'observed_at': {'repositories': repositories.get('observed_at'), 'cluster': cluster.get('observed_at')},
            'qualification': 'not-assessed', 'results': rows,
            'counts': dict(Counter(row['observed_state'] for row in rows))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repositories', type=Path, required=True)
    parser.add_argument('--cluster', type=Path, required=True)
    parser.add_argument('--max-age-seconds', type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.max_age_seconds <= 86400:
        parser.error('Freshness bound must be 1..86400 seconds')
    try:
        result = evaluate(load(args.repositories), load(args.cluster), max_age_seconds=args.max_age_seconds)
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as exc:
        parser.exit(2, f'Invalid snapshot: {type(exc).__name__}\n')
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
