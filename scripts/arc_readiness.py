#!/usr/bin/env python3
"""Describe ARC routing and observed capacity from sanitized snapshots; never query or mutate services."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re

LIMIT = 8_000_000
PREFIX = 'actions.github.com/'
NAME = re.compile(r'[A-Za-z0-9_.-]{1,100}')
REPO = re.compile(r'[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}')


def load(path):
    """Bound input size and reject duplicate JSON keys."""
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate JSON key')
            result[key] = value
        return result
    with Path(path).open('rb') as stream:
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


def describe_pool(pool, repository, listeners, pods):
    """Keep configuration, registration evidence, admission and workers separate."""
    meta, spec = pool.get('metadata', {}), pool.get('spec', {})
    namespace, name = meta.get('namespace'), meta.get('name')
    if not all(isinstance(x, str) and NAME.fullmatch(x) for x in (namespace, name)):
        raise ValueError('Invalid scale-set identity')
    maximum, minimum = spec.get('maxRunners'), spec.get('minRunners')
    admission = ('held' if type(maximum) is int and maximum == 0 else
                 'enabled' if type(maximum) is int and maximum > 0 else
                 'unspecified' if 'maxRunners' not in spec else 'unknown')
    candidates = [item for item in listeners.get('items', []) if linked(item, pool)]
    listener_state, registration, reconciliation = 'unknown', 'unknown', 'unknown'
    if not candidates and covers(listeners):
        listener_state = 'absent'
    elif len(candidates) > 1:
        listener_state = 'ambiguous'
    elif len(candidates) == 1:
        listener = candidates[0]
        lmeta, lspec = listener.get('metadata', {}), listener.get('spec', {})
        if not all(isinstance(lmeta.get(key), str) and lmeta[key] for key in ('name', 'namespace', 'uid')):
            listener_state = 'unknown'
        elif lspec.get('githubConfigUrl') != spec.get('githubConfigUrl'):
            listener_state = 'scope-mismatch'
        else:
            listener_max = lspec.get('maxRunners')
            listener_min = lspec.get('minRunners')
            if type(listener_max) is int and type(maximum) is int:
                minimum_matches = (type(minimum) is int and minimum >= 0
                                   and (minimum == 0 if 'minRunners' not in lspec else
                                        type(listener_min) is int and listener_min == minimum))
                reconciliation = ('matched' if listener_max == maximum
                                  and minimum_matches
                                  else 'pending')
            registered = lspec.get('runnerScaleSetId')
            if type(registered) is int and registered > 0:
                registration = 'observed-scale-set-id'
            owned = [p for p in pods.get('items', [])
                     if p.get('metadata', {}).get('namespace') == lmeta.get('namespace')
                     and linked(p, pool)
                     and any(o.get('uid') == lmeta.get('uid') and o.get('kind') == 'AutoscalingListener'
                             and o.get('controller') is True
                             for o in p.get('metadata', {}).get('ownerReferences', []))]
            if lmeta.get('deletionTimestamp'):
                listener_state = 'terminating'
            elif len(owned) > 1:
                listener_state = 'ambiguous'
            elif len(owned) == 1:
                complete = covers(listeners) and covers(pods, lmeta.get('namespace'))
                listener_state = ('ready' if complete else 'observed-ready') if ready(owned[0]) else 'not-ready'
            elif covers(pods, lmeta.get('namespace')):
                listener_state = 'pod-absent'
    workers = [p for p in pods.get('items', [])
               if p.get('metadata', {}).get('namespace') == namespace and linked(p, pool)
               and any(o.get('kind') == 'EphemeralRunner' and o.get('controller') is True
                       for o in p.get('metadata', {}).get('ownerReferences', []))]
    worker_count = len(workers) if covers(pods, namespace) else None
    generation = meta.get('generation')
    observed = pool.get('status', {}).get('observedGeneration')
    current = type(generation) is int and generation > 0 and type(observed) is int and observed == generation
    terminating = bool(meta.get('deletionTimestamp'))
    state = 'unknown'
    if terminating:
        state = 'terminating'
    elif admission == 'held':
        state = 'held'
    elif (admission == 'enabled' and current and listener_state == 'ready'
          and registration != 'unknown' and reconciliation == 'matched'):
        if worker_count == 0 and type(minimum) is int and minimum == 0:
            state = 'idle-scale-to-zero'
        elif worker_count is not None and worker_count > 0:
            state = 'workers-observed'
    return {'namespace': namespace, 'name': name, 'scope': scope(pool, repository),
            'registration': registration, 'admission': admission, 'min_runners': minimum,
            'max_runners': maximum, 'controller_observed_generation': current,
            'listener': listener_state, 'listener_limit_reconciliation': reconciliation, 'worker_pods': worker_count,
            'observed_worker_pods': len(workers), 'ready_worker_pods': sum(ready(p) for p in workers),
            'state': state}


def evaluate(repositories, cluster, *, now=None, max_age_seconds=900):
    """Evaluate supplied evidence only; no result certifies runtime qualification."""
    if not all(isinstance(value, dict) and type(value.get('schema')) is int and value['schema'] == 1
               for value in (repositories, cluster)):
        raise ValueError('Unsupported snapshot schema')
    now = now or datetime.now(timezone.utc)
    ages = [(now - timestamp(value.get('observed_at'))).total_seconds() for value in (repositories, cluster)]
    fresh = all(0 <= age <= max_age_seconds for age in ages)
    entries = repositories.get('repositories')
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError('Invalid repository inventory')
    pools, listeners, pods = (collection(cluster, key) for key in ('asrs', 'listeners', 'pods'))
    rows, seen, unprofiled = [], set(), []
    for entry in entries:
        repository = entry.get('repository')
        if not isinstance(repository, str) or not REPO.fullmatch(repository) or repository.casefold() in seen:
            raise ValueError('Invalid or repeated repository')
        seen.add(repository.casefold())
        profiles = entry.get('profiles')
        if not isinstance(profiles, dict):
            raise ValueError('Explicit expected profiles are required')
        if not profiles:
            unprofiled.append(repository)
        for profile, labels in profiles.items():
            if not NAME.fullmatch(profile):
                raise ValueError('Invalid profile')
            selector = ('unknown' if labels is None else 'missing' if labels == [] else
                        'configured' if isinstance(labels, list) and all(isinstance(x, str) and NAME.fullmatch(x) for x in labels)
                        and len(labels) == len(set(labels)) else 'invalid')
            matches, elsewhere = [], False
            unknown = not covers(pools)
            if selector == 'configured':
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
            configuration = ('ambiguous' if len(matches) > 1 else 'unknown' if unknown else 'matched' if matches else
                             'scope-mismatch' if elsewhere else 'absent')
            state = matches[0]['state'] if configuration == 'matched' else 'unknown'
            demand = entry.get('queued_jobs', {}).get(profile)
            if demand is not None and (type(demand) is not int or demand < 0):
                raise ValueError('Invalid queued job count')
            if state == 'held' and demand:
                state = 'held-with-queued-demand'
            mode = entry.get('mode')
            rows.append({'repository': repository, 'profile': profile,
                         'mode': mode if mode in ('', 'github', 'self-hosted', 'k3s', None) else 'invalid',
                         'self_hosted_selected': entry.get('mode') in ('self-hosted', 'k3s'),
                         'selector': selector, 'configuration': configuration if selector == 'configured' else 'unknown',
                         'pools': matches, 'queued_jobs': demand, 'observed_state': state,
                         'evidence_fresh': fresh, 'qualification': 'not-assessed'})
    return {'schema': 1, 'read_only': True, 'evidence_fresh': fresh,
            'repository_inventory_complete': repositories.get('complete') is True,
            'profiles_not_declared': unprofiled,
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
