"""Verify the runner permits only the reviewed dependency generators."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = ['uv export --locked --offline --no-build --extra dev --group build --group security --no-emit-project --output-file .github/requirements-ci.txt', 'uv export --locked --offline --no-build --no-default-groups --no-emit-project --output-file .github/requirements-runtime.txt', 'uv export --locked --offline --no-build --no-dev --extra monitor --no-emit-project --format requirements-txt --output-file requirements-docker.txt', 'uv export --locked --offline --no-build --no-dev --no-emit-project --format requirements-txt --output-file .renovate-constraints.txt', 'uv export --locked --offline --no-build --no-dev --no-emit-project --format requirements-txt --output-file .renovate-constraints.txt --no-hashes', 'uv export --locked --offline --no-build --only-group build --no-emit-project --output-file .github/requirements-release-build.txt', 'uv export --locked --offline --no-build --only-group security --no-emit-project --output-file .github/requirements-release-security.txt', 'uv pip compile --generate-hashes --python-version 3.11 --python-platform x86_64-apple-darwin --no-build --output-file requirements-kitten-macos-intel.lock requirements-kitten-macos-intel.in', 'uv pip compile --no-deps --generate-hashes --python-version 3.11 --no-build --output-file requirements-kitten-package.lock requirements-kitten-package.in', 'uv pip compile --python-version 3.14 --python-platform linux --generate-hashes --no-emit-index-url --no-build tests/tls/recovery-test-requirements.in --output-file tests/tls/recovery-test-requirements.lock', 'uv pip compile --python-version=3.12 --python-platform=x86_64-manylinux_2_28 --generate-hashes --no-build .github/requirements-fuzz.in --output-file=.github/requirements-fuzz.txt', 'uv pip compile .github/requirements-browser.in --constraint .renovate-constraints.txt --python-version 3.12 --universal --no-build --generate-hashes --no-header --no-annotate --output-file .github/requirements-browser.txt', 'uv pip compile .github/requirements-fuzz.in --constraint .renovate-constraints.txt --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --no-build --generate-hashes --no-header --no-annotate --output-file .github/requirements-fuzz.txt', 'uv pip compile .github/requirements-fuzz.in --generate-hashes --python-version 3.12 --python-platform x86_64-unknown-linux-gnu --no-build --output-file .github/requirements-fuzz.txt', 'uv pip compile recovery/runtime/requirements.in --generate-hashes --python-version 3.14 --python-platform linux --no-build --output-file recovery/runtime/requirements.lock', 'uv pip compile requirements-russian-silero.in --generate-hashes --emit-index-url --index-strategy unsafe-best-match --python-version 3.11 --python-platform x86_64-manylinux_2_28 --no-build --output-file requirements-russian-silero.lock', 'uv pip compile requirements-mercedes.in --no-deps --no-build --generate-hashes --universal --python-version 3.12 --no-annotate --output-file requirements-mercedes.txt']

class GeneratorCommandPolicyTests(unittest.TestCase):
    def test_exact_commands_reject_shells_and_policy_changes(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node is required to validate the actual runner configuration")
        script = r"""
const assert = require('node:assert/strict');
const config = require(process.argv[1]);
const commands = JSON.parse(process.argv[2]);
assert.equal(config.ignoreScripts, true);
assert.equal(config.allowPlugins, false);
assert.equal(config.allowShellExecutorForPostUpgradeCommands, false);
assert.equal(config.allowedCommands.length, commands.length);
const matches = command => config.allowedCommands.some(pattern => new RegExp(pattern).test(command));
for (const pattern of config.allowedCommands) assert(pattern.startsWith('^') && pattern.endsWith('$'));
for (const command of commands) {
  assert(matches(command), command);
  const invalid = [command + '; touch marker', command + ' && touch marker',
    command + '\n touch marker', ' ' + command, command + ' ',
    command + ' --output-file ../../marker', command.replace('--no-build', ''),
    command.replace('--no-deps', ''),
    command.replace('--locked', '--frozen')];
  for (const candidate of invalid) if (candidate !== command) assert(!matches(candidate), candidate);
}
for (const command of ['uv run script.py', 'sh -c true', 'python script.py',
  'uv export', 'uv pip compile requirements.in', 'echo $RENOVATE_TOKEN']) assert(!matches(command));
"""
        result = subprocess.run([node, "-e", script, str(ROOT / "renovate-runner.cjs"), json.dumps(COMMANDS)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

if __name__ == "__main__":
    unittest.main()
