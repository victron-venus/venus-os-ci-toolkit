"""Offline rendering checks and fixtures for the real, separately locked CEL evaluator."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('qualification_arc', ROOT / 'deploy/arc-private/render.py')
arc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(arc)
IMAGE = 'registry.example/qualified@sha256:' + 'd' * 64
PRODUCTION_IMAGE = 'registry.example/production@sha256:' + 'c' * 64
OPERATOR = 'cluster-operator@example.invalid'
ROW = {'name': 'example/standard', 'visibility': 'private',
       'profiles': ['ci', 'automation', 'release'], 'ci_storage': 'tmpfs-2gi'}


def canary(mode='private'):
    return arc.QualificationCanary('qualification-' + mode, 'arc-private-ci', 'reserve-worker',
                                   ROW['name'], 'ci', 'nuc', arc.NUC_RUNTIME, IMAGE, mode, OPERATOR)


def render(probe=None, rows=None, targets=None):
    rows = copy.deepcopy(rows or [ROW])
    targets = targets or (arc.NucKvmTarget(ROW['name'], 'ci'),)
    plan = arc.NucKvmPlan(targets, PRODUCTION_IMAGE, '192.0.2.2')
    return arc.render({'repositories': rows}, 'registry.example/shared@sha256:' + 'a' * 64,
                      'registry.example/native@sha256:' + 'b' * 64, '192.0.2.1',
                      nuc_kvm=plan, qualification=probe)


def guard(documents):
    return next(x for x in documents['foundations.json']['items']
                if x['kind'] == 'ValidatingAdmissionPolicy'
                and x['metadata']['name'] == arc.NUC_RUNTIME + '-workers')


def context(pod, actor=OPERATOR, operation='CREATE', old=None):
    pod = copy.deepcopy(pod)
    # RuntimeClass admission supplies overhead before validation. This fixture
    # does not claim to model Kubernetes' typed schema or every defaulting step.
    pod['spec']['overhead'] = {'cpu': '100m', 'memory': '128Mi'}
    # Kubernetes defaults an omitted PodSecurityContext to an empty object.
    pod['spec'].setdefault('securityContext', {})
    return {'object': pod, 'oldObject': copy.deepcopy(old),
            'request': {'namespace': pod['metadata']['namespace'], 'operation': operation,
                        'userInfo': {'username': actor}}}


def set_at(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = copy.deepcopy(replacement)


def cel_fixtures():
    """Evaluate emitted CEL, not a handwritten equivalent Python predicate."""
    suites = []
    for mode in ('public', 'private'):
        documents = render(canary(mode))
        pod = documents['qualification-canary.json']
        base = context(pod)
        cases = [{'name': 'operator creates exact ' + mode, 'context': base, 'allow': True}]

        def changed(name, path, value):
            item = copy.deepcopy(base)
            set_at(item, path, value)
            cases.append({'name': name, 'context': item, 'allow': False})

        obj, spec, container = ('object',), ('object', 'spec'), ('object', 'spec', 'containers', 0)
        mutations = [
            ('other authenticated creator', ('request', 'userInfo', 'username'), 'other@example.invalid'),
            ('ARC cannot create probe', ('request', 'userInfo', 'username'),
             'system:serviceaccount:arc-systems:arc-controller'),
            ('PR service account cannot create probe', ('request', 'userInfo', 'username'),
             'system:serviceaccount:arc-private-ci:reserve-worker'),
            ('other name', obj + ('metadata', 'name'), 'qualification-other'),
            ('other image', container + ('image',), PRODUCTION_IMAGE),
            ('other runtime', spec + ('runtimeClassName',), 'runc'),
            ('other service account', spec + ('serviceAccountName',), 'default'),
            ('API identity', spec + ('automountServiceAccountToken',), True),
            ('host network', spec + ('hostNetwork',), True),
            ('host PID', spec + ('hostPID',), True),
            ('host IPC', spec + ('hostIPC',), True),
            ('other command', container + ('command',), ['/bin/sh']),
            ('command arguments', container + ('args',), ['-c', 'true']),
            ('envFrom', container + ('envFrom',), [{'secretRef': {'name': 'not-allowed'}}]),
            ('host privilege', container + ('securityContext', 'privileged'), True),
            ('pod identity override', spec + ('securityContext', 'runAsUser'), 1000),
            ('pod sysctl', spec + ('securityContext', 'sysctls'), [{'name': 'kernel.shm_rmid_forced', 'value': '1'}]),
            ('unmasked proc', container + ('securityContext', 'procMount'), 'Unmasked'),
            ('host volume', spec + ('volumes',), [{'name': 'docker-data', 'hostPath': {'path': '/'}}]),
            ('redirected mount', container + ('volumeMounts', 0, 'mountPath'), '/host'),
            ('mount propagation', container + ('volumeMounts', 0, 'mountPropagation'), 'Bidirectional'),
            ('subPath', container + ('volumeMounts', 0, 'subPath'), 'shared'),
            ('sidecar', spec + ('containers',), pod['spec']['containers'] * 2),
            ('init container', spec + ('initContainers',), copy.deepcopy(pod['spec']['containers'])),
            ('ephemeral container', spec + ('ephemeralContainers',), copy.deepcopy(pod['spec']['containers'])),
            ('host port', container + ('ports',), [{'containerPort': 8080, 'hostPort': 8080}]),
            ('pull secret', spec + ('imagePullSecrets',), [{'name': 'not-allowed'}]),
            ('owner reference', obj + ('metadata', 'ownerReferences'),
             [{'apiVersion': 'actions.github.com/v1alpha1', 'kind': 'EphemeralRunner',
               'name': pod['metadata']['name'], 'uid': 'synthetic-owner', 'controller': True}]),
            ('finalizer', obj + ('metadata', 'finalizers'), ['example.invalid/keep']),
            ('annotation', obj + ('metadata', 'annotations'), {'dev.gvisor.spec.mount': '/host'}),
            ('other node', spec + ('nodeSelector', arc.HOSTNAME_LABEL), 'other-node'),
            ('direct binding', spec + ('nodeName',), 'nuc'),
            ('other scheduler', spec + ('schedulerName',), 'other-scheduler'),
            ('longer deadline', spec + ('activeDeadlineSeconds',), 601),
            ('longer grace', spec + ('terminationGracePeriodSeconds',), 30),
            ('restarts', spec + ('restartPolicy',), 'Always'),
            ('preemption', spec + ('preemptionPolicy',), 'PreemptLowerPriority'),
            ('raised priority', spec + ('priority',), 1),
            ('more CPU', container + ('resources', 'limits', 'cpu'), '3'),
            ('less memory reservation', container + ('resources', 'requests', 'memory'), '1Gi'),
            ('more memory', container + ('resources', 'limits', 'memory'), '8Gi'),
            ('more disk', container + ('resources', 'limits', 'ephemeral-storage'), '21Gi'),
            ('no serialization', spec + ('affinity',), {}),
            ('wrong scope', obj + ('metadata', 'labels', arc.SCOPE_LABEL), 'private-other-scope'),
            ('wrong profile', obj + ('metadata', 'labels', arc.PROFILE_LABEL), 'release'),
            ('light concurrency', obj + ('metadata', 'labels', arc.CONCURRENCY_LABEL), 'light'),
            ('wrong marker', obj + ('metadata', 'labels', arc.QUALIFICATION_LABEL), 'other'),
        ]
        for name, path, value in mutations:
            changed(name, path, value)
        for name, extra in [
                ('unexpected environment', {'name': 'LD_PRELOAD', 'value': '/tmp/untrusted'}),
                ('JIT literal', {'name': 'ACTIONS_RUNNER_INPUT_JITCONFIG', 'value': 'synthetic'}),
                ('JIT secret', {'name': 'ACTIONS_RUNNER_INPUT_JITCONFIG', 'valueFrom': {
                    'secretKeyRef': {'name': pod['metadata']['name'], 'key': 'jitToken'}}}),
                ('extra private bootstrap input', {'name': 'RUNNER_PRIVATE_DOCKER_STORAGE', 'value': 'disk'}),
                ('duplicate environment', copy.deepcopy(pod['spec']['containers'][0]['env'][0]))]:
            changed(name, container + ('env',), pod['spec']['containers'][0]['env'] + [extra])
        for name, value in [('wrong request namespace', 'elsewhere'), ('other private namespace', 'arc-private-release')]:
            changed(name, ('request', 'namespace'), value)
            item = copy.deepcopy(base)
            item['request']['namespace'] = item['object']['metadata']['namespace'] = value
            cases.append({'name': name + ' both', 'context': item, 'allow': False})
        bound = copy.deepcopy(base['object'])
        bound['spec']['nodeName'] = 'nuc'
        update = context(bound, actor='system:node:nuc', operation='UPDATE', old=bound)
        cases.append({'name': 'bound kubelet status update', 'context': update, 'allow': True})
        for key, value in [('name', 'production-like-name'), ('labels', {})]:
            escape = copy.deepcopy(update)
            escape['object']['metadata'][key] = value
            escape['object']['spec']['runtimeClassName'] = 'runc'
            escape['object']['spec']['containers'][0]['image'] = PRODUCTION_IMAGE
            cases.append({'name': 'oldObject catches UPDATE escape ' + key, 'context': escape, 'allow': False})
        missing_old = copy.deepcopy(update)
        missing_old['oldObject'] = None
        cases.append({'name': 'UPDATE requires existing probe', 'context': missing_old, 'allow': False})
        cases.append({'name': 'DELETE cleanup outside admission operations',
                      'context': {'object': None, 'oldObject': bound,
                                  'request': {'operation': 'DELETE', 'namespace': 'arc-private-ci',
                                              'userInfo': {'username': OPERATOR}}}, 'allow': True})
        production = copy.deepcopy(documents[arc.name(ROW['name'], 'ci') + '.json']['template'])
        production['metadata'].update(name='production-worker', namespace='arc-private-ci')
        production['metadata']['ownerReferences'] = [{
            'apiVersion': 'actions.github.com/v1alpha1', 'kind': 'EphemeralRunner',
            'name': 'production-worker', 'uid': 'synthetic-owner', 'controller': True}]
        production['spec']['containers'][0]['env'].append({
            'name': 'ACTIONS_RUNNER_INPUT_JITCONFIG', 'valueFrom': {
                'secretKeyRef': {'name': 'production-worker', 'key': 'jitToken'}}})
        normal = context(production, actor='system:serviceaccount:arc-systems:arc-controller')
        cases.append({'name': 'normal ARC production JIT remains accepted', 'context': normal, 'allow': True})
        mixed = copy.deepcopy(normal)
        mixed['object']['spec']['containers'][0]['env'].append({'name': 'RUNNER_RESERVE_QUALIFICATION', 'value': '1'})
        cases.append({'name': 'production JIT cannot opt into qualification', 'context': mixed, 'allow': False})
        suites.append({'name': mode, 'policy': guard(documents)['spec'], 'cases': cases})
    disabled = [copy.deepcopy(next(c for c in suites[0]['cases']
                                  if c['name'] == 'normal ARC production JIT remains accepted'))]
    for mode in ('public', 'private'):
        disabled.append({'name': mode + ' probe denied without opt-in',
                         'context': context(render(canary(mode))['qualification-canary.json']), 'allow': False})
    suites.append({'name': 'production-only', 'policy': guard(render())['spec'], 'cases': disabled})
    return suites


class QualificationRenderTests(unittest.TestCase):
    def test_opt_in_changes_only_one_policy_and_adds_one_pod(self):
        baseline = render()
        for mode in ('public', 'private'):
            candidate = render(canary(mode))
            self.assertEqual(set(candidate) - set(baseline), {'qualification-canary.json'})
            candidate.pop('qualification-canary.json')
            prior, changed = guard(baseline), guard(candidate)
            self.assertEqual(len(prior['spec']['validations']), 20)
            self.assertEqual(len(changed['spec']['validations']), 22)
            differing = [i for i, (a, b) in enumerate(zip(prior['spec']['validations'],
                                                        changed['spec']['validations'])) if a != b]
            self.assertEqual(differing, [6, 10, 17])
            candidate['foundations.json']['items'][candidate['foundations.json']['items'].index(changed)] = copy.deepcopy(prior)
            self.assertEqual(candidate, baseline)

    def test_full_fleet_pool_count_and_routes_are_unchanged(self):
        rows = [dict(ROW, name=f'example/project-{i}', profiles=['ci', 'automation'],
                     automation_storage='tmpfs-3gi') for i in range(20)]
        rows[0]['profiles'] += ['release']
        rows[1]['profiles'] += ['arm64']
        rows[2]['profiles'] += ['lan']
        rows[2]['lan_egress'] = [{'cidr': '192.0.2.20/32', 'port': 9443}]
        targets = tuple(arc.NucKvmTarget(r['name'], p) for r in rows for p in ('ci', 'automation'))
        baseline = render(rows=rows, targets=targets)
        candidate = render(canary()._replace(scope=rows[0]['name']), rows=rows, targets=targets)
        self.assertEqual(len(candidate['index.json']), 43)
        for key in baseline:
            if key != 'foundations.json':
                self.assertEqual(candidate[key], baseline[key], key)

    def test_strict_plan_rejects_unsafe_or_incomplete_inputs(self):
        invalid = {'name': ['ordinary-worker', '../probe'], 'namespace': ['default'],
                   'service_account': ['default'], 'scope': ['example/unknown'], 'profile': ['release'],
                   'node': ['other-node'], 'runtime_class': ['runc'], 'image': ['image:latest', PRODUCTION_IMAGE],
                   'bootstrap_profile': ['arbitrary'], 'creator_username': ['', 'system:anonymous',
                   'system:serviceaccount:arc:controller', 'system:node:nuc', 'line\nbreak']}
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    render(canary()._replace(**{field: value}))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            value = dict(canary()._asdict(), schema=1)
            path.write_text(json.dumps(value))
            self.assertEqual(arc.load_qualification_canary(path), canary())
            for mutation in ({**value, 'unknown': 1}, {**value, 'schema': True}, {'schema': 1}):
                path.write_text(json.dumps(mutation))
                with self.assertRaises(ValueError):
                    arc.load_qualification_canary(path)
            path.write_text('{"schema":1,"schema":1}')
            with self.assertRaises(ValueError):
                arc.load_qualification_canary(path)

    def test_node_guard_discovers_probe_without_jit_or_a_new_namespace(self):
        spec = importlib.util.spec_from_file_location('qualification_guard', ROOT / 'deploy/runner-reserve/node_guard.py')
        node_guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(node_guard)
        pod = render(canary())['qualification-canary.json']
        record = {'id': 'a' * 64, 'metadata': {'namespace': pod['metadata']['namespace'], 'uid': 'synthetic-uid'},
                  'labels': pod['metadata']['labels'], 'state': 'SANDBOX_READY'}
        with mock.patch.object(node_guard, 'command', return_value=json.dumps({'items': [record]})), \
                mock.patch.object(node_guard, 'cached_worker', return_value={'uid': 'synthetic-uid'}) as cached:
            self.assertEqual(node_guard.discover(), [{'uid': 'synthetic-uid'}])
            cached.assert_called_once_with(record)
        self.assertFalse(any(e['name'] == 'ACTIONS_RUNNER_INPUT_JITCONFIG' for e in pod['spec']['containers'][0]['env']))


if __name__ == '__main__':
    if sys.argv[1:] == ['--cel-fixtures']:
        print(json.dumps(cel_fixtures()))
    else:
        unittest.main()
