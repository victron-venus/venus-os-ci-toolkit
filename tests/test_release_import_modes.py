"""Run release modules in isolated interpreters without test sys.path mutations."""

import os
import subprocess  # nosec B404 - isolated local Python interpreter only.
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "release_control",
    "release_state",
    "release_version_adapter",
    "prepare_version",
    "release_versioned",
    "version_receipt",
    "stage_release_assets",
    "version_plan",
)


class ReleaseImportTests(unittest.TestCase):
    def run_python(self, *arguments):
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        # Interpreter and arguments are fixed test inputs; no shell or external command.
        result = subprocess.run(  # nosec B603
            [sys.executable, *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_package_imports_without_scripts_on_path(self):
        script = "import importlib; " + "; ".join(
            f"importlib.import_module('scripts.{name}')" for name in MODULES
        )
        self.run_python("-c", script)

    def test_workflow_contract_validation_in_package_and_script_modes(self):
        script = """
policy = {'single_entry_ci': True, 'versioning': {},
          'validation_workflows': ['ci.yml'],
          'channel_validation_workflows': {'beta': ['beta.yml']}}
workflows = {
    'beta.yml': {'on': {'workflow_call': {}}, 'jobs': {}},
    'release-quality-gate.yml': {
        'on': {'workflow_call': {'inputs': {'channel': {'type': 'string', 'required': 'true'}}}},
        'jobs': {},
    },
}
try:
    contracts.validate_release_channels(policy, workflows)
except ValueError as error:
    assert str(error) == 'Release validation gate inventory differs from channel policy'
else:
    raise AssertionError('Incomplete channel gate accepted')
"""
        for setup in (
            "from scripts import workflow_contracts as contracts\n",
            "import sys; sys.path.insert(0, 'scripts'); import workflow_contracts as contracts\n",
        ):
            with self.subTest(setup=setup):
                self.run_python("-c", setup + script)

    def test_package_dependencies_share_module_and_exception_identity(self):
        script = """
import importlib
from pathlib import Path
from scripts import release_control as control
from scripts import release_state as state
from scripts import release_versioned as lifecycle
from scripts import version_plan, version_receipt
assert lifecycle.rc is control
assert state.rc is control
assert lifecycle.StateGitHub is state.StateGitHub
assert version_plan.atomic_write_bytes is control.atomic_write_bytes
assert version_receipt.stream_identity is control.stream_identity
assert issubclass(state.StateGitHub, control.GitHub)
for name in ('release_provenance', 'release_signing', 'assemble_native_container',
             'release_container_labels', 'publish_verified', 'runner_preflight',
             'verified_images', 'write_binary_build_metadata'):
    if (Path('scripts') / (name + '.py')).is_file():
        importlib.import_module('scripts.' + name)
"""
        self.run_python("-c", script)

    def test_package_lazy_validation_imports(self):
        script = """
from pathlib import Path
from scripts import release_control as control
policy = {'repository': 'example/project', 'mode': 'release', 'versioning': {},
          'single_entry_ci': True, 'validation_workflows': ['ci.yml'],
          'channel_validation_workflows': {'beta': ['beta.yml']}}
control.require_release_policy(policy, 'example/project', qualified=False)
assert control.validation_profile(policy, 'beta')['workflows'] == ['beta.yml']
if Path('scripts/release_fuzz.py').is_file():
    from scripts import release_fuzz
    policy['fuzzing'] = {'toolkit_ref': 'a' * 40}
    control.require_release_policy(policy, 'example/project', qualified=False)
    manifest = {'source_policy': {'data': policy}, 'channel': 'beta',
                'validation_profile': control.validation_profile(policy, 'beta'),
                'fuzzing_profile': release_fuzz.profile(policy, 'beta')}
    control.validate_validation_manifest(manifest)
    policy['fuzzing'] = {'toolkit_ref': 'mutable'}
    try:
        control.require_release_policy(policy, 'example/project', qualified=False)
    except control.ReleaseError:
        pass
    else:
        raise AssertionError('Invalid lazy-loaded policy accepted')
"""
        self.run_python("-c", script)

    def test_standalone_importlib_loads(self):
        script = """
import importlib.util
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
for name in MODULES:
    spec = importlib.util.spec_from_file_location(name, Path('scripts') / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
""".replace("MODULES", repr(MODULES))
        self.run_python("-c", script)

    def test_direct_cli_help(self):
        for name in MODULES:
            if name == "release_state":
                continue
            with self.subTest(module=name):
                self.run_python(f"scripts/{name}.py", "--help")

    def test_event_input_shapes(self):
        script = """
import json
import os
from pathlib import Path
import tempfile
from scripts import release_versioned as lifecycle
with tempfile.TemporaryDirectory() as directory:
    event = Path(directory) / 'event.json'
    os.environ['GITHUB_EVENT_PATH'] = str(event)
    for value in (None, [], 'event', *({'inputs': item} for item in (['untrusted'], [], '', 0, False))):
        event.write_text(json.dumps(value))
        try:
            lifecycle.event_inputs()
        except lifecycle.rc.ReleaseError:
            pass
        else:
            raise AssertionError('Malformed event accepted')
    for value in ({}, {'inputs': None}, {'inputs': {'channel': 'beta'}}):
        event.write_text(json.dumps(value))
        assert lifecycle.event_inputs() == (value.get('inputs') or {})
"""
        self.run_python("-c", script)
