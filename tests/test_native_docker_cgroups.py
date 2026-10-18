"""Validate Docker containment against realistic cgroup controller layouts."""
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/arc-private/native-docker.sh'
PARENT = '/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod1234_abcd.slice'


class CgroupAdmissionTests(unittest.TestCase):
    def fixture(self, version='v2', parent=PARENT, cpu='100000', memory='4294967296', hybrid=False, container_mount=False, hierarchy='1'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        scope = parent + '/cri-containerd-123.scope'
        mount_root = scope if container_mount else '/'
        cgroups, mounts = [], []
        if version == 'v1':
            for controllers, name in [('cpu,cpuacct', 'cpu'), ('memory', 'memory')]:
                mount = root / name
                directory = mount if container_mount else mount / parent.lstrip('/')
                directory.mkdir(parents=True)
                cgroups.append(f'5:{controllers}:{parent}/cri-containerd-123.scope')
                mounts.append(f'1 0 0:1 {mount_root} {mount} rw - cgroup cgroup rw,{controllers}')
                if name == 'cpu':
                    (directory / 'cpu.cfs_quota_us').write_text(cpu + '\n')
                    (directory / 'cpu.cfs_period_us').write_text('100000\n')
                else:
                    (directory / 'memory.limit_in_bytes').write_text(memory + '\n')
                    (directory / 'memory.use_hierarchy').write_text(hierarchy + '\n')
        if version == 'v2' or hybrid:
            mount = root / 'unified'
            directory = mount if container_mount else mount / parent.lstrip('/')
            directory.mkdir(parents=True)
            cgroups.append(f'0::{parent}/cri-containerd-123.scope')
            mounts.append(f'2 0 0:2 {mount_root} {mount} rw - cgroup2 cgroup2 rw')
            if not hybrid:
                (directory / 'cpu.max').write_text(cpu + ' 100000\n')
                (directory / 'memory.max').write_text(memory + '\n')
        proc, mountfile = root / 'cgroups', root / 'mounts'
        proc.write_text('\n'.join(cgroups) + '\n')
        mountfile.write_text('\n'.join(mounts) + '\n')
        return proc, mountfile

    def check(self, **kwargs):
        proc, mounts = self.fixture(**kwargs)
        return subprocess.run(['/bin/bash', str(SCRIPT), '--check', str(proc), str(mounts)],
                              capture_output=True, text=True, timeout=5)

    def test_unified_v2_uses_pod_limits(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('v2 ' + PARENT, result.stdout)

    def test_v1_and_hybrid_choose_real_controllers(self):
        for hybrid in (False, True):
            result = self.check(version='v1', hybrid=hybrid)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('v1 ' + PARENT, result.stdout)

    def test_container_subtree_mount_uses_its_stricter_limits(self):
        for version in ('v1', 'v2'):
            result = self.check(version=version, container_mount=True, cpu='50000', memory='2147483648')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(PARENT + '/cri-containerd-123.scope', result.stdout)
            self.assertIn('CPU 50000/100000; memory 2147483648', result.stdout)

    def test_cgroupfs_parent(self):
        result = self.check(version='v1', parent='/kubepods/burstable/pod1234-abcd')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unlimited_cpu_or_memory_is_rejected(self):
        for settings in ({'cpu': 'max'}, {'memory': 'max'}, {'version': 'v1', 'cpu': '-1'},
                         {'version': 'v1', 'memory': '9223372036854771712'}, {'cpu': '0'}):
            self.assertNotEqual(self.check(**settings).returncode, 0, settings)

    def test_nonhierarchical_memory_is_rejected(self):
        result = self.check(version='v1', hierarchy='0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('hierarchical memory accounting', result.stderr)

    def test_non_pod_or_traversing_parent_is_rejected(self):
        for parent in ('/system.slice', '/kubepods.slice/../other', '/kubepods.slice/kubepods-burstable.slice'):
            self.assertNotEqual(self.check(parent=parent).returncode, 0, parent)

    def test_missing_mount_or_limit_is_rejected(self):
        proc, mounts = self.fixture()
        mounts.write_text('')
        result = subprocess.run(['/bin/bash', str(SCRIPT), '--check', str(proc), str(mounts)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)

    def test_fixture_paths_cannot_override_daemon_startup(self):
        result = subprocess.run(['/bin/bash', str(SCRIPT), '/tmp/cgroups', '/tmp/mounts'], capture_output=True)
        self.assertEqual(result.returncode, 2)


if __name__ == '__main__':
    unittest.main()
