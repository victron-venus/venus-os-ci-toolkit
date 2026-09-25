"""Admission, isolation and offline Helm rendering for the private runner templates."""

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "deploy/arc-ottplay"
POOLS = ("linux", "kvm", "release")


def read_values(pool):
    return yaml.safe_load((CONFIG / f"{pool}-values.yaml").read_text())


def job(pool):
    return read_values(pool)["template"]["spec"]


class ArcOttplayTests(unittest.TestCase):
    def test_exact_scopes_and_release_admission(self):
        groups = json.loads((CONFIG / "github-groups.json").read_text())
        self.assertEqual(groups["organization"], "open-ott-play")
        ordinary, release = groups["groups"]
        self.assertEqual(ordinary["name"], "ottplay-private-ci")
        self.assertEqual(
            set(ordinary["repositories"]),
            {"open-ott-play/ottplay-core", "open-ott-play/ottplay-android"},
        )
        self.assertEqual(release["name"], "ottplay-private-release")
        self.assertEqual(release["repositories"], ["open-ott-play/ottplay-android"])
        self.assertTrue(release["restricted_to_workflows"])
        self.assertEqual(
            set(release["selected_workflows"]),
            {
                "open-ott-play/ottplay-android/.github/workflows/release.yml@refs/heads/main",
                "open-ott-play/ottplay-android/.github/workflows/runner-smoke.yml@refs/heads/main",
            },
        )
        for group in groups["groups"]:
            self.assertEqual(group["visibility"], "selected")
            self.assertFalse(group["allows_public_repositories"])

    def test_named_pools_start_small_and_missing_admission_stays_disabled(self):
        for pool in POOLS:
            values = read_values(pool)
            self.assertEqual(values["runnerScaleSetName"], f"ottplay-k3s-{pool}-x64")
            self.assertEqual(values["minRunners"], 0)
            self.assertEqual(values["maxRunners"], 1 if pool == "linux" else 0)
            self.assertEqual(
                values["runnerGroup"],
                "ottplay-private-release"
                if pool == "release"
                else "ottplay-private-ci",
            )
            self.assertEqual(
                values["githubConfigUrl"], "https://github.com/open-ott-play"
            )
            self.assertEqual(values["githubConfigSecret"], "ottplay-runner-app")
            self.assertNotIn("containerMode", values)
        # One approved runner in each pool leaves capacity for current workloads.
        limits = [job(pool)["containers"][0]["resources"]["limits"] for pool in POOLS]
        self.assertLessEqual(sum(int(row["cpu"]) for row in limits), 12)
        self.assertLessEqual(sum(int(row["memory"][:-2]) for row in limits), 24)

    def test_runner_security_no_host_authority_or_persistent_job_data(self):
        for pool in POOLS:
            spec = job(pool)
            self.assertEqual(spec["nodeSelector"]["kubernetes.io/hostname"], "mp")
            self.assertEqual(spec["nodeSelector"]["kubernetes.io/arch"], "amd64")
            self.assertFalse(spec["automountServiceAccountToken"])
            self.assertEqual(spec["serviceAccountName"], "ottplay-runner-no-api")
            self.assertEqual(spec["restartPolicy"], "Never")
            self.assertEqual(spec["securityContext"]["runAsUser"], 1001)
            self.assertTrue(spec["securityContext"]["runAsNonRoot"])
            self.assertEqual(
                spec["securityContext"]["seccompProfile"]["type"], "RuntimeDefault"
            )
            self.assertEqual(len(spec["containers"]), 1)
            runner = spec["containers"][0]
            self.assertEqual(runner["name"], "runner")
            self.assertEqual(runner["command"], ["/opt/ottplay/start-runner.sh"])
            self.assertFalse(runner["securityContext"]["allowPrivilegeEscalation"])
            self.assertTrue(runner["securityContext"]["readOnlyRootFilesystem"])
            self.assertEqual(
                runner["securityContext"]["capabilities"], {"drop": ["ALL"]}
            )
            for volume in spec["volumes"]:
                self.assertEqual(set(volume), {"name", "emptyDir"})
                self.assertIn("sizeLimit", volume["emptyDir"])
            for field in ("hostNetwork", "hostPID", "hostIPC"):
                self.assertFalse(spec.get(field, False))
            self.assertNotIn("envFrom", runner)
            self.assertNotIn("env", runner)

    def test_setup_actions_sdk_and_browser_have_ephemeral_writable_paths(self):
        expected = {
            "/home/runner",
            "/home/runner/_work",
            "/opt/hostedtoolcache",
            "/tmp",
            "/opt/android-sdk",
            "/dev/shm",
        }
        for pool in POOLS:
            spec = job(pool)
            mounts = spec["containers"][0]["volumeMounts"]
            self.assertEqual({row["mountPath"] for row in mounts}, expected)
            self.assertTrue(all(not row.get("readOnly") for row in mounts))
            self.assertEqual(
                spec["containers"][0]["resources"]["limits"]["ephemeral-storage"],
                "48Gi",
            )

    def test_kvm_requires_admitted_node_and_real_device_allocation(self):
        spec = job("kvm")
        self.assertEqual(spec["nodeSelector"]["ottplay.dev/kvm-admitted"], "true")
        self.assertEqual(spec["securityContext"]["supplementalGroups"], [993])
        for kind in ("requests", "limits"):
            self.assertEqual(
                spec["containers"][0]["resources"][kind]["devic.es/kvm"], "1"
            )
        for pool in ("linux", "release"):
            self.assertNotIn(
                "devic.es/kvm", job(pool)["containers"][0]["resources"]["limits"]
            )

    def test_job_networks_and_serviceaccounts_do_not_inherit_controller_authority(self):
        documents = list(yaml.safe_load_all((CONFIG / "namespaces.yaml").read_text()))
        namespaces = {
            d["metadata"]["name"] for d in documents if d["kind"] == "Namespace"
        }
        self.assertEqual(
            namespaces,
            {
                "arc-ottplay-system",
                "arc-ottplay-ci",
                "arc-ottplay-kvm",
                "arc-ottplay-release",
            },
        )
        accounts = [d for d in documents if d["kind"] == "ServiceAccount"]
        self.assertEqual(len(accounts), 3)
        self.assertTrue(all(not d["automountServiceAccountToken"] for d in accounts))
        self.assertFalse(
            any(
                d["kind"] in ("Secret", "RoleBinding", "ClusterRoleBinding")
                for d in documents
            )
        )
        policies = [d for d in documents if d["kind"] == "NetworkPolicy"]
        self.assertEqual(len(policies), 3)
        for policy in policies:
            spec = policy["spec"]
            self.assertEqual(
                spec["podSelector"]["matchLabels"], {"ottplay.dev/workload": "job"}
            )
            self.assertEqual(spec["ingress"], [])
            dns, public = spec["egress"]
            self.assertEqual({p["port"] for p in dns["ports"]}, {53})
            self.assertEqual(public["ports"], [{"protocol": "TCP", "port": 443}])
            excluded = set(public["to"][0]["ipBlock"]["except"])
            self.assertLessEqual(
                {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"},
                excluded,
            )

    def test_image_inputs_are_pinned_and_shell_contract_is_valid(self):
        dockerfile = (CONFIG / "Dockerfile").read_text()
        bases = re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE)
        self.assertEqual(
            bases,
            [
                "ghcr.io/actions/actions-runner@sha256:e5496277be5d09bc968b3d64911b74e219ac4a3f2edce956a3ecf9271bea1ef4",
                "node@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c",
                "ubuntu@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3",
            ],
        )
        self.assertIn("# ubuntu:24.04", dockerfile)
        for tool in (
            "python3-pip",
            "python3-venv",
            "openjdk-17-jdk-headless",
            "ffmpeg",
            "gh",
            "procps",
        ):
            self.assertIn(tool, dockerfile)
        self.assertIn("USER 1001:1001", dockerfile)
        self.assertIn("RUNNER_MANUALLY_TRAP_SIG=1", dockerfile)
        self.assertNotIn("sudo", dockerfile)
        for script in ("start-runner.sh", "smoke.sh"):
            subprocess.run(["bash", "-n", str(CONFIG / script)], check=True)
        rejected = subprocess.run(
            ["bash", str(CONFIG / "smoke.sh"), "unknown"],
            check=False,
            capture_output=True,
        )
        self.assertEqual(rejected.returncode, 2)

    def test_browser_dependency_installer_uses_only_locked_packages(self):
        package = json.loads((CONFIG / "buildtools/package.json").read_text())
        lock = json.loads((CONFIG / "buildtools/package-lock.json").read_text())
        self.assertTrue(package["private"])
        self.assertNotIn("scripts", package)
        self.assertEqual(package["dependencies"], {"playwright": "1.63.0"})
        self.assertEqual(lock["lockfileVersion"], 3)
        self.assertEqual(lock["packages"][""]["dependencies"], package["dependencies"])
        self.assertEqual(
            set(lock["packages"]),
            {"", "node_modules/playwright", "node_modules/playwright-core"},
        )
        for name in ("playwright", "playwright-core"):
            entry = lock["packages"][f"node_modules/{name}"]
            self.assertEqual(entry["version"], "1.63.0")
            self.assertEqual(
                entry["resolved"],
                f"https://registry.npmjs.org/{name}/-/{name}-1.63.0.tgz",
            )
            self.assertRegex(entry["integrity"], r"^sha512-[A-Za-z0-9+/]{86}==$")
        dockerfile = (CONFIG / "Dockerfile").read_text()
        self.assertIn(
            "npm ci --ignore-scripts --no-audit --no-fund --prefix /opt/ottplay-buildtools",
            dockerfile,
        )
        self.assertIn(
            "node /opt/ottplay-buildtools/node_modules/playwright/cli.js install-deps chromium",
            dockerfile,
        )
        self.assertNotIn("npx --yes", dockerfile)
        self.assertNotIn("install-deps chromium webkit", dockerfile)

    def test_startup_with_root_owned_fsgroup_mount_preserves_executables(self):
        image = os.environ.get("ARC_TEST_DOCKER_IMAGE")
        if not image:
            self.skipTest(
                "Set ARC_TEST_DOCKER_IMAGE to a local Python/Debian test image"
            )
        # A scratch container reproduces emptyDir ownership without touching a
        # host path, cluster, registration, or the actual runner distribution.
        script = (
            r"""
import json, os, pathlib, stat, subprocess, sys
source = pathlib.Path('/opt/actions-runner')
target = pathlib.Path('/home/runner')
source.mkdir(parents=True)
(source / 'bin').mkdir()
(source / 'bin/tool').write_text('#!/bin/sh\nprintf executable-ok\\n\n')
(source / 'bin/tool').chmod(0o755)
(source / 'bin/link').symlink_to('tool')
(source / '.hidden').write_text('copied')
(source / 'run.sh').write_text("""
            + '"""'
            + r"""#!/bin/bash
set -eu
test "$(id -u)" = 1001
test -x bin/tool
test -L bin/link
test "$(readlink bin/link)" = tool
test -f .hidden
test -d _work
test -w "$RUNNER_TOOL_CACHE"
test -w "$ANDROID_HOME"
printf RUNNER_STARTED
"""
            + '"""'
            + r""")
(source / 'run.sh').chmod(0o755)
def mount(path):
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o2775)
    os.chown(path, 0, 1001)
def drop():
    os.setgroups([])
    os.setgid(1001)
    os.setuid(1001)
    os.umask(0o077)
mount(target)
old = subprocess.run(['cp', '-a', '--no-preserve=ownership', str(source) + '/.', str(target)],
                     preexec_fn=drop, text=True, capture_output=True)
assert old.returncode != 0 and 'Operation not permitted' in old.stderr, old
# Root has no DAC override capability and cannot traverse copied mode-0700
# directories. Retire the scratch mount without weakening the tested rights.
target.rename('/home/runner-old')
mount(target)
for path in ('/opt/hostedtoolcache', '/opt/android-sdk'):
    mount(pathlib.Path(path))
start = pathlib.Path('/tmp/start-runner.sh')
start.write_text(json.loads(sys.stdin.read()))
result = subprocess.run(['bash', str(start)], preexec_fn=drop, text=True, capture_output=True,
    env=dict(os.environ, RUNNER_TOOL_CACHE='/opt/hostedtoolcache', ANDROID_HOME='/opt/android-sdk'))
assert result.returncode == 0, result.stderr
assert result.stdout == 'RUNNER_STARTED', result.stdout
drop()
assert (source / 'bin/link').is_symlink() and (target / 'bin/link').is_symlink()
assert stat.S_IMODE((target / 'run.sh').stat().st_mode) == 0o700
actual = target.stat()
assert (actual.st_uid, actual.st_gid, stat.S_IMODE(actual.st_mode)) == (0, 1001, 0o2775)
print('old archive copy rejected; actual startup succeeded under UID1001')
"""
        )
        completed = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "SETUID",
                "--cap-add",
                "SETGID",
                "--cap-add",
                "CHOWN",
                "--entrypoint",
                "python3",
                image,
                "-c",
                script,
            ],
            input=json.dumps((CONFIG / "start-runner.sh").read_text()),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("actual startup succeeded", completed.stdout)

    def test_offline_real_chart_rendering_preserves_runner_isolation(self):
        chart_dir = os.environ.get("ARC_CHART_DIR")
        if not chart_dir or not shutil.which("helm"):
            self.skipTest(
                "Set ARC_CHART_DIR to cached pinned Helm charts for offline rendering"
            )
        version = json.loads((CONFIG / "versions.json").read_text())[
            "arc_chart_version"
        ]
        namespaces = {
            "linux": "arc-ottplay-ci",
            "kvm": "arc-ottplay-kvm",
            "release": "arc-ottplay-release",
        }
        for pool in POOLS:
            command = [
                "helm",
                "template",
                f"ottplay-k3s-{pool}-x64",
                str(Path(chart_dir) / f"gha-runner-scale-set-{version}.tgz"),
                "--namespace",
                namespaces[pool],
                "--values",
                str(CONFIG / f"{pool}-values.yaml"),
            ]
            rendered = subprocess.run(
                command, check=True, text=True, capture_output=True
            ).stdout
            documents = list(yaml.safe_load_all(rendered))
            resource = next(
                d for d in documents if d and d.get("kind") == "AutoscalingRunnerSet"
            )
            self.assertEqual(resource["spec"]["template"]["spec"], job(pool))
            self.assertEqual(
                resource["spec"]["githubConfigSecret"], "ottplay-runner-app"
            )
            self.assertFalse(any(d and d.get("kind") == "Secret" for d in documents))
            for document in documents:
                if document and document.get("kind") in (
                    "RoleBinding",
                    "ClusterRoleBinding",
                ):
                    self.assertNotIn(
                        "ottplay-runner-no-api",
                        [s["name"] for s in document.get("subjects", [])],
                    )
        rendered = subprocess.run(
            [
                "helm",
                "template",
                "ottplay-arc",
                str(Path(chart_dir) / f"gha-runner-scale-set-controller-{version}.tgz"),
                "--namespace",
                "arc-ottplay-system",
                "--values",
                str(CONFIG / "controller-values.yaml"),
            ],
            check=True,
            text=True,
            capture_output=True,
        ).stdout
        self.assertIn("name: ottplay-arc-controller", rendered)
        self.assertNotIn("kind: Secret", rendered)


if __name__ == "__main__":
    unittest.main()
