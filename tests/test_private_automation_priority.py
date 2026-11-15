"""Opt-in output contracts plus real emitted-CEL fixtures; no scheduling simulation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('automation_priority_profiles', ROOT / 'tests/test_private_nuc_profiles.py')
profiles = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profiles)
arc = profiles.arc
SELECTED = 'example/heavy'
# Independently captured from immutable renderer commit4774181 before this change.
DEFAULT_GOLDENS = {
    'default': '0758944d004700a17e2552afc84b5def9363b6a93d15ac137783efce6b1d5bb3',
    'low_share': '6fcf34f0f5a247248b4f9e32d5849dd7dccbbacac3af6e0599a4399be813f76e',
    'full_memory': '48df67d5d2d8f700c3b115fc32ebc7717113ada70571e57fd69bba85e026ab18',
    'opportunistic': 'e58bd8e9476eb9a9df1430b787b95d899b3a46acf1ec942152fcf0f26fbaca09',
}


def chosen():
    return profiles.plan()._replace(nonpreempting_automation_scopes=(SELECTED,))


def priority_guard(documents):
    return next(item for item in documents['foundations.json']['items']
                if item['kind'] == 'ValidatingAdmissionPolicy'
                and item['metadata']['name'] == arc.AUTOMATION_PRIORITY_CLASS)


def priority_cel_fixtures():
    """The locked cel-go harness evaluates these actual complete policies."""
    targets = tuple(arc.NucKvmTarget(*target) for target in (
        ('example/standard', 'ci'), ('example/standard', 'automation'),
        ('example/standard', 'release'), (SELECTED, 'ci'), (SELECTED, 'automation')))
    config = arc.NucKvmPlan(targets, profiles.NUC_IMAGE, profiles.NUC_IP,
                            nonpreempting_automation_scopes=(SELECTED,))
    rows = copy.deepcopy(profiles.ROWS)
    rows[0]['automation_storage'] = 'tmpfs-3gi'
    documents = profiles.render(config, rows=rows)

    def pod(scope, profile):
        value = copy.deepcopy(profiles.worker(documents, (scope, profile))['template'])
        name = arc.name(scope, profile) + '-synthetic'
        value['metadata'].update(name=name, namespace='arc-private-' + profile)
        value['metadata']['ownerReferences'] = [{
            'apiVersion': 'actions.github.com/v1alpha1', 'kind': 'EphemeralRunner',
            'name': name, 'uid': 'synthetic-owner', 'controller': True}]
        value['spec']['containers'][0]['env'].append({
            'name': 'ACTIONS_RUNNER_INPUT_JITCONFIG', 'valueFrom': {
                'secretKeyRef': {'name': name, 'key': 'jitToken'}}})
        value['spec']['overhead'] = {'cpu': '100m', 'memory': '128Mi'}
        value['spec'].setdefault('securityContext', {})
        value['spec']['priority'] = (arc.AUTOMATION_PRIORITY_VALUE
                                    if value['spec'].get('priorityClassName') == arc.AUTOMATION_PRIORITY_CLASS else 0)
        value['spec'].setdefault('preemptionPolicy', 'PreemptLowerPriority')
        return value

    def context(value, old=None, operation='CREATE'):
        return {'object': copy.deepcopy(value), 'oldObject': copy.deepcopy(old),
                'request': {'namespace': value['metadata'].get('namespace', ''),
                            'operation': operation}}

    selected = pod(SELECTED, 'automation')
    cases = [{'name': 'selected automation fixed nonpreempting priority',
              'context': context(selected), 'allow': True}]
    for field, value in [('priority', 1000), ('preemptionPolicy', 'PreemptLowerPriority'),
                         ('priorityClassName', 'system-cluster-critical')]:
        bad = copy.deepcopy(selected)
        bad['spec'][field] = value
        cases.append({'name': 'reject selected wrong ' + field, 'context': context(bad), 'allow': False})
    bad = copy.deepcopy(selected)
    del bad['spec']['priorityClassName']
    cases.append({'name': 'selected missing class', 'context': context(bad), 'allow': False})
    for scope, profile in [('example/standard', 'automation'), (SELECTED, 'ci'), ('example/standard', 'release')]:
        ordinary = pod(scope, profile)
        cases.append({'name': 'ordinary unchanged ' + scope + profile, 'context': context(ordinary), 'allow': True})
        stolen = copy.deepcopy(ordinary)
        stolen['spec'].update(priorityClassName=arc.AUTOMATION_PRIORITY_CLASS,
                              priority=arc.AUTOMATION_PRIORITY_VALUE, preemptionPolicy='Never')
        cases.append({'name': 'class cannot be stolen by ' + scope + profile,
                      'context': context(stolen), 'allow': False})
    legacy = copy.deepcopy(selected)
    legacy['spec'].pop('priorityClassName')
    legacy['spec'].update(priority=0, preemptionPolicy='PreemptLowerPriority', nodeName='nuc')
    cases.append({'name': 'legacy selected status update preserved',
                  'context': context(legacy, legacy, 'UPDATE'), 'allow': True})
    upgraded = copy.deepcopy(legacy)
    upgraded['spec'].update(priorityClassName=arc.AUTOMATION_PRIORITY_CLASS,
                           priority=arc.AUTOMATION_PRIORITY_VALUE, preemptionPolicy='Never')
    cases.append({'name': 'cannot mutate legacy Pod priority',
                  'context': context(upgraded, legacy, 'UPDATE'), 'allow': False})
    admitted = copy.deepcopy(selected)
    admitted['spec']['nodeName'] = 'nuc'
    cases.append({'name': 'new selected status update preserved',
                  'context': context(admitted, admitted, 'UPDATE'), 'allow': True})
    old_other = pod('example/standard', 'ci')
    old_other['spec'].update(priorityClassName='existing-background-class', priority=-100,
                             preemptionPolicy='Never', nodeName='nuc')
    cases.append({'name': 'existing unrelated priority not changed on update',
                  'context': context(old_other, old_other, 'UPDATE'), 'allow': True})

    pc = arc.automation_priority_class()
    class_cases = [{'name': 'exact class', 'context': context(pc), 'allow': True}]
    for field, value in [('value', 1000), ('preemptionPolicy', 'PreemptLowerPriority'), ('globalDefault', True)]:
        bad = copy.deepcopy(pc)
        bad[field] = value
        class_cases.append({'name': 'class rejects wrong ' + field, 'context': context(bad), 'allow': False})
    bad = copy.deepcopy(pc)
    del bad['preemptionPolicy']
    class_cases.append({'name': 'class cannot default to preempting', 'context': context(bad), 'allow': False})
    return [{'name': 'nonpreempting-automation-worker',
             'policy': profiles.policy(documents, '-workers')['spec'], 'cases': cases},
            {'name': 'nonpreempting-automation-class',
             'policy': priority_guard(documents)['spec'], 'cases': class_cases}]


class AutomationPriorityTests(unittest.TestCase):
    def test_default_and_empty_opt_in_are_identical_to_independent_goldens(self):
        variants = {'default': profiles.plan(),
                    'low_share': profiles.plan()._replace(low_share_ci_scopes=('example/standard',)),
                    'full_memory': profiles.plan()._replace(full_memory_ci_scopes=('example/standard',)),
                    'opportunistic': profiles.plan()._replace(opportunistic_cpu_targets=(arc.NucKvmTarget(SELECTED, 'automation'),))}
        for key, config in variants.items():
            output = profiles.render(config)
            raw = json.dumps(output, sort_keys=True, separators=(',', ':')).encode()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), DEFAULT_GOLDENS[key])
            self.assertEqual(profiles.render(config._replace(nonpreempting_automation_scopes=())), output)

    def test_only_selected_worker_changes_two_fields_and_three_bounded_foundations(self):
        before = profiles.render(profiles.plan())
        after = profiles.render(chosen())
        normalized = copy.deepcopy(after)
        spec = profiles.worker(normalized, (SELECTED, 'automation'))['template']['spec']
        self.assertEqual(spec.pop('priorityClassName'), arc.AUTOMATION_PRIORITY_CLASS)
        self.assertEqual(spec.pop('preemptionPolicy'), 'Never')
        items = normalized['foundations.json']['items']
        extra = [x for x in items if x['metadata']['name'] == arc.AUTOMATION_PRIORITY_CLASS]
        self.assertEqual({x['kind'] for x in extra}, {'PriorityClass', 'ValidatingAdmissionPolicy', 'ValidatingAdmissionPolicyBinding'})
        normalized['foundations.json']['items'] = [x for x in items if x not in extra]
        rules = profiles.policy(normalized, '-workers')['spec']['validations']
        added = rules.pop()
        self.assertIn('explicitly selected automation', added['message'])
        self.assertEqual(normalized, before)

    def test_exact_class_is_modest_never_and_not_global_default(self):
        resource = arc.automation_priority_class()
        self.assertEqual(resource['value'], 100)
        self.assertEqual(resource['preemptionPolicy'], 'Never')
        self.assertIs(resource['globalDefault'], False)
        self.assertIn('not fair queueing', resource['description'])

    def test_invalid_duplicate_unselected_and_CI_only_scopes_refused(self):
        for value in (['example/heavy'], (True,), ('bad scope',), (SELECTED, SELECTED),
                      ('example/unselected',), ('example/standard',)):
            with self.assertRaises(ValueError):
                profiles.render(profiles.plan()._replace(nonpreempting_automation_scopes=value))

    def test_strict_plan_parser(self):
        value = {'targets': [{'scope': SELECTED, 'profile': 'automation'}],
                 'image': profiles.NUC_IMAGE, 'node_ip': profiles.NUC_IP,
                 'nonpreempting_automation_scopes': [SELECTED]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'plan.json'
            path.write_text(json.dumps(value))
            self.assertEqual(arc.load_nuc_kvm_plan(path).nonpreempting_automation_scopes, (SELECTED,))
            for bad in (True, SELECTED, [False]):
                path.write_text(json.dumps(dict(value, nonpreempting_automation_scopes=bad)))
                with self.assertRaises(ValueError):
                    arc.load_nuc_kvm_plan(path)

    def test_emitted_CEL_fixtures_include_real_positive_negative_and_legacy_updates(self):
        suites = priority_cel_fixtures()
        self.assertEqual(len(suites), 2)
        self.assertTrue(all(any(c['allow'] for c in s['cases']) and any(not c['allow'] for c in s['cases']) for s in suites))
        for suite in suites:
            for rule in suite['policy']['validations']:
                profiles.split_top_level(rule['expression'], '||')


if __name__ == '__main__':
    unittest.main()
