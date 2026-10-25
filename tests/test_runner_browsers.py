"""Offline tests for signed browser pins, capabilities and scoped installation."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runner_browsers', ROOT / 'scripts/install_runner_browsers.py')
browser = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser)


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.lock = json.loads(browser.LOCK.read_text())

    def test_actual_signed_metadata_and_exact_package_rows(self):
        if not shutil.which('gpgv'):
            self.skipTest('gpgv unavailable')
        browser.authenticate(self.lock)

    def test_signature_requires_one_expected_valid_primary(self):
        fp = self.lock['vendors']['google']['fingerprint']
        valid = '[GNUPG:] VALIDSIG subkey date 1 0 4 0 1 8 00 ' + fp
        browser.verify_signature(valid, fp)
        for status in ('', valid.replace(fp, 'F' * 40), valid + '\n' + valid,
                       valid + '\n[GNUPG:] BADSIG key'):
            with self.subTest(status=status), self.assertRaises(ValueError):
                browser.verify_signature(status, fp)

    def test_release_requires_sha256_and_unique_index(self):
        row = ' aaaa 42 main/binary-amd64/Packages\n'
        self.assertEqual(browser.release_index('SHA256:\n' + row, 'main/binary-amd64/Packages'),
                         ('aaaa', 42))
        for text in ('SHA1:\n' + row, 'SHA256:\n' + row + row, 'SHA256:\n malformed\n'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                browser.release_index(text, 'main/binary-amd64/Packages')

    def test_package_metadata_changes_are_rejected(self):
        package = self.lock['packages'][0]
        file = self.lock['vendors'][package['vendor']]['files']['packages']['path']
        rows = browser.package_rows((browser.SNAPSHOTS / file).read_text())
        browser.bind_package(package, rows)
        for key, value in [('version', '999.0.0.0-1'), ('architecture', 'arm64'),
                           ('sha256', 'a' * 64), ('bytes', 1), ('installed_bytes', 1),
                           ('filename', 'pool/other.deb')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                browser.bind_package({**package, key: value}, rows)
        selected = [row for row in rows if row['Package'] == package['name']]
        with self.assertRaises(ValueError):
            browser.bind_package(package, selected * 2)

    def test_metadata_hash_and_child_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'input').write_bytes(b'original')
            pin = {'path': 'input', 'bytes': 8, 'sha256': hashlib.sha256(b'original').hexdigest()}
            browser.metadata(pin, root)
            (root / 'link').symlink_to(root / 'input')
            for changed in ({**pin, 'path': 'link'}, {**pin, 'path': '../input'},
                            {**pin, 'bytes': True}, {**pin, 'sha256': '0' * 64}):
                with self.subTest(changed=changed), self.assertRaises(ValueError):
                    browser.metadata(changed, root)

    def test_complete_amd64_set_and_explicit_arm64_capability(self):
        browser.validate_lock(self.lock)
        self.assertEqual(len(browser.selected_packages(self.lock, 'amd64')), 4)
        self.assertEqual(browser.selected_packages(self.lock, 'arm64'), [])
        with self.assertRaises(ValueError):
            browser.selected_packages(self.lock, 'ppc64le')
        for mutation in ('missing', 'wrong_arch', 'claim_arm64', 'schema_bool'):
            lock = copy.deepcopy(self.lock)
            if mutation == 'missing':
                lock['packages'].pop()
            elif mutation == 'wrong_arch':
                lock['packages'][0]['architecture'] = 'arm64'
            elif mutation == 'claim_arm64':
                lock['architectures']['arm64']['full_branded'] = True
            else:
                lock['schema'] = True
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                browser.validate_lock(lock)

    def test_dependency_union_and_current_three_generations(self):
        self.assertEqual(len(self.lock['playwright']['dependencies']), 79)
        self.assertEqual({p['version'] for p in self.lock['playwright']['sources']},
                         {'1.64.0', '1.63.0', '1.62.1'})
        for name in ('libgtk-4-1', 'libavif16', 'libvpx9', 'libevent-2.1-7t64', 'xvfb'):
            self.assertIn(name, self.lock['playwright']['dependencies'])
        lock = copy.deepcopy(self.lock)
        lock['playwright']['dependencies'][0] = '--allow-unauthenticated'
        with self.assertRaises(ValueError):
            browser.validate_lock(lock)

    def test_scoped_updater_removal_preserves_other_files_and_link_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            other = root / 'ubuntu.sources'
            other.write_text('preserved')
            owned = root / 'google.sources'
            owned.write_text('owned')
            link = root / 'google-cron'
            link.symlink_to(other)
            records = browser.disable_updaters([owned, link])
            self.assertEqual(len(records), 2)
            self.assertEqual(other.read_text(), 'preserved')
            self.assertFalse(owned.exists())
            self.assertFalse(link.is_symlink())

    def test_installed_version_and_cli_version_both_bound(self):
        package = self.lock['packages'][0]
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'browser'
            executable.write_bytes(b'fixture')
            package = {**package, 'executable': str(executable)}
            identity = '\t'.join([package['name'], package['version'], 'amd64', 'install ok installed'])
            output = 'Browser ' + package['version'].rsplit('-', 1)[0]
            with patch.object(browser, 'run', side_effect=[identity, output]):
                self.assertEqual(browser.installed_record(package)['executable_sha256'],
                                 hashlib.sha256(b'fixture').hexdigest())
            for values in ([identity.replace('amd64', 'arm64')], [identity, 'Browser 0.0.0.0']):
                with patch.object(browser, 'run', side_effect=values), self.assertRaises(ValueError):
                    browser.installed_record(package)

    def test_install_order_and_no_ad_hoc_browser_flags(self):
        docker = (ROOT / 'deploy/runner-reserve/Dockerfile').read_text()
        install = 'RUN python3 /opt/victron-image-build/browsers/scripts/install_runner_browsers.py'
        self.assertLess(docker.index(install), docker.index('RUN dpkg -i /tmp/victron-libbpf'))
        self.assertLess(docker.index('COPY --from=node'), docker.index(install + ' --qualify'))
        fixture = (ROOT / 'deploy/runner-reserve/qualify-browsers.cjs').read_text()
        self.assertIn('chromium.launch({ headless: true, channel, timeout: 30000 })', fixture)
        self.assertNotIn('--no-sandbox', fixture)
        self.assertIn('finally', fixture)
        self.assertIn('await browser.close()', fixture)
        self.assertIn('gvisor_qualified: false', fixture)

    def test_actual_probe_control_flow_closes_each_browser_on_failure(self):
        if not shutil.which('node'):
            self.skipTest('Node unavailable')
        harness = r'''
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const lock = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const source = fs.readFileSync(process.argv[1], 'utf8');
const mode = process.argv[3];
const opened = [], closed = [], outputs = [];
const fakeProcess = {platform:'linux', arch:'x64', getuid:()=>1001,
  argv:['node','probe','/toolchain', mode === 'record-path' ? '/other-record' :
    '/usr/local/share/victron-browsers/installed.json'], exitCode:0};
const record = {packages:lock.packages};
const toolchain = name => name.endsWith('package.json') ? {version:'1.64.0'} : {
  chromium:{launch:async options => {
    assert.deepEqual(Object.keys(options).sort(), ['channel','headless','timeout']);
    opened.push(options.channel);
    const channel = options.channel;
    return {
      version:()=>mode === 'version' && channel === 'chrome-beta' ? '0.0.0.0' :
        lock.packages.find(p=>p.channel===channel).version.split('-')[0],
      newContext:async()=>({route:async()=>{}, close:async()=>{}, newPage:async()=>({
        goto:async url=>{assert.ok(url.startsWith('data:text/html,'));
          if(mode==='page') throw Error('page failed');},
        title:async()=>'runner-browser-probe', locator:()=>({textContent:async()=>'ready'}),
        evaluate:async()=>42
      })}), close:async()=>closed.push(channel)
    };
  }}
};
const context = {process:fakeProcess, console:{log:x=>outputs.push(JSON.parse(x)),error:()=>{}},
 require:name=>name==='node:fs' ? {readFileSync:()=>JSON.stringify(record)} :
   name==='node:module' ? {createRequire:()=>toolchain} : require(name)};
(async()=>{await vm.runInNewContext(source,context);
console.log(JSON.stringify({opened,closed,outputs,exitCode:fakeProcess.exitCode}));})()
.catch(e=>{console.error(e);process.exitCode=2;});
'''
        for mode in ('success', 'version', 'page', 'record-path'):
            with self.subTest(mode=mode):
                result = subprocess.run(['node', '-e', harness,
                    str(ROOT / 'deploy/runner-reserve/qualify-browsers.cjs'),
                    str(browser.LOCK), mode], check=True, capture_output=True, text=True, timeout=10)
                value = json.loads(result.stdout)
                self.assertEqual(value['opened'], value['closed'])
                if mode == 'success':
                    self.assertEqual(len(value['opened']), 4)
                    self.assertTrue(value['outputs'][0]['full_branded_qualified'])
                else:
                    self.assertEqual(value['exitCode'], 1)
                    self.assertEqual(value['outputs'], [])
                    if mode == 'record-path':
                        self.assertEqual(value['opened'], [])


if __name__ == '__main__':
    unittest.main()
