"""Exercise the workflow's real uv install command against an offline wheelhouse."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import zipfile

import yaml

ROOT = Path(__file__).resolve().parents[1]


class LockedPythonDependencies(unittest.TestCase):
    """A lock selects exact packages and cannot be repaired silently during CI."""

    def setUp(self):
        self.assertIsNotNone(shutil.which("uv"), "Install the pinned uv test runtime")
        # pylint: disable-next=consider-using-with
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.project = self.directory / "nested project"
        self.project.mkdir()
        self.wheels = self.directory / "wheels"
        self.wheels.mkdir()
        for version in ("1.0.0", "2.0.0"):
            self.make_wheel(version)
        self.manifest = self.project / "pyproject.toml"
        self.manifest.write_text(
            '[project]\nname = "ci-lock-consumer"\nversion = "0.1.0"\n'
            'requires-python = ">=3.11"\ndependencies = ["locked-fixture==1.0.0"]\n'
        )
        self.path_file = self.directory / "github-path"
        self.environment = {
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "HOME": str(self.directory),
            "UV_CACHE_DIR": str(self.directory / "cache"),
            "UV_FIND_LINKS": str(self.wheels),
            "UV_NO_INDEX": "true",
            "UV_OFFLINE": "true",
            "UV_PYTHON_DOWNLOADS": "never",
            "GITHUB_PATH": str(self.path_file),
            "BUILD_DEPENDENCY_GROUP": "",
        }
        workflow = yaml.safe_load((ROOT / ".github/workflows/python-ci.yml").read_text())
        self.steps = {s["name"]: s for s in workflow["jobs"]["ci"]["steps"]}
        self.install = self.steps["Install locked dependencies"]
        self.assertEqual(
            self.install["if"], "inputs.install-dependencies && inputs.use-uv-lock"
        )
        self.assertFalse(workflow[True]["workflow_call"]["inputs"]["use-uv-lock"]["default"])
        self.assertEqual(
            workflow[True]["workflow_call"]["inputs"]["build-dependency-group"]["default"],
            "",
        )
        self.assertEqual(
            self.steps["Install dependencies"]["if"],
            "inputs.install-dependencies && !inputs.use-uv-lock",
        )

    def make_wheel(self, version):
        """Create two real installable releases without network access."""
        name = f"locked_fixture-{version}"
        with zipfile.ZipFile(self.wheels / f"{name}-py3-none-any.whl", "w") as wheel:
            wheel.writestr("locked_fixture/__init__.py", f'VERSION = "{version}"\n')
            info = f"{name}.dist-info"
            wheel.writestr(
                f"{info}/METADATA",
                f"Metadata-Version: 2.1\nName: locked-fixture\nVersion: {version}\n",
            )
            wheel.writestr(
                f"{info}/WHEEL",
                "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(f"{info}/RECORD", "")

    def run_command(self, *command):
        """Run in the isolated consumer, never the toolkit's environment."""
        return subprocess.run(
            command, cwd=self.project, env=self.environment,
            capture_output=True, text=True, check=False,
        )

    def configure_locked_backend(self):
        """Build the real consumer with one of two available backend versions."""
        for version in ("1.0.0", "2.0.0"):
            filename = self.wheels / f"locked_backend-{version}-py3-none-any.whl"
            source = textwrap.dedent('''\
                import pathlib
                import zipfile

                def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
                    pathlib.Path("used-build-backend").write_text(VERSION)
                    name = "ci_lock_consumer-0.1.0"
                    filename = name + "-py3-none-any.whl"
                    with zipfile.ZipFile(pathlib.Path(wheel_directory) / filename, "w") as wheel:
                        wheel.writestr("ci_lock_consumer/__init__.py", "BACKEND = " + repr(VERSION))
                        info = name + ".dist-info"
                        wheel.writestr(info + "/METADATA", "Metadata-Version: 2.1\\nName: ci-lock-consumer\\nVersion: 0.1.0\\nRequires-Dist: locked-fixture==1.0.0\\n")
                        wheel.writestr(info + "/WHEEL", "Wheel-Version: 1.0\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
                        wheel.writestr(info + "/RECORD", "")
                    return filename

                build_wheel = build_editable
                ''')
            with zipfile.ZipFile(filename, "w") as wheel:
                wheel.writestr("locked_backend.py", f"VERSION = {version!r}\n" + source)
                info = f"locked_backend-{version}.dist-info"
                wheel.writestr(
                    info + "/METADATA",
                    f"Metadata-Version: 2.1\nName: locked-backend\nVersion: {version}\n",
                )
                wheel.writestr(info + "/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
                wheel.writestr(info + "/RECORD", "")
        self.manifest.write_text(
            self.manifest.read_text()
            + '\n[build-system]\nrequires = ["locked-backend>=1"]\nbuild-backend = "locked_backend"\n'
            + '\n[dependency-groups]\nbuild = ["locked-backend==1.0.0"]\n'
        )
        self.environment["BUILD_DEPENDENCY_GROUP"] = "build"

    def test_build_group_uses_locked_backend_without_isolated_resolution(self):
        """The unlocked backend requirement cannot select the newer available version."""
        self.configure_locked_backend()
        original = self.lock()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.project / "used-build-backend").read_text(), "1.0.0")
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)
        result = self.run_command(
            str(self.project / ".venv/bin/python"), "-c",
            "import ci_lock_consumer,locked_fixture; assert ci_lock_consumer.BACKEND == '1.0.0'; assert locked_fixture.VERSION == '1.0.0'",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_local_builds_only_run_after_locked_backend_bootstrap(self):
        """Local builds wait until the published locked backend is installed."""
        manifest_before = self.manifest.read_text()
        for source in ('workspace = true', 'path = "local-backend", editable = true', 'path = "local-backend"'):
            with self.subTest(source=source):
                self.manifest.write_text(manifest_before)
                self.configure_locked_backend()
                local = self.project / "local-backend"
                local.mkdir(exist_ok=True)
                (local / "local-build-backend").unlink(missing_ok=True)
                shutil.rmtree(self.project / ".venv", ignore_errors=True)
                (local / "pyproject.toml").write_text(
                    '[project]\nname = "local-backend"\nversion = "1.0.0"\n'
                    '[build-system]\nrequires = []\nbuild-backend = "local_build"\nbackend-path = ["."]\n'
                )
                (local / "local_build.py").write_text(
                    'from pathlib import Path\n'
                    'def build_wheel(*args, **kwargs):\n'
                    '    try:\n'
                    '        from locked_backend import VERSION\n'
                    '    except ImportError:\n'
                    '        VERSION = "missing"\n'
                    '    Path("local-build-backend").write_text(VERSION)\n'
                    '    raise RuntimeError("stop after observing the local build environment")\n'
                    'build_editable = build_wheel\n'
                )
                original_manifest = self.manifest.read_text()
                manifest = original_manifest.replace(
                    'build = ["locked-backend==1.0.0"]',
                    'build = ["locked-backend==1.0.0", "local-backend"]',
                )
                if source == 'workspace = true':
                    manifest += '\n[tool.uv.workspace]\nmembers = ["local-backend"]\n'
                manifest += f'\n[tool.uv.sources]\nlocal-backend = {{{source}}}\n'
                self.manifest.write_text(manifest)
                original_lock = self.lock()
                result = self.sync()
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((local / "local-build-backend").exists(), result.stderr)
                self.assertEqual((local / "local-build-backend").read_text(), "1.0.0")
                self.assertFalse(self.path_file.exists())
                self.assertEqual((self.project / "uv.lock").read_bytes(), original_lock)

    def test_missing_build_group_fails_before_project_build(self):
        """A misspelled group cannot fall back to isolated build downloads."""
        self.configure_locked_backend()
        self.lock()
        self.environment["BUILD_DEPENDENCY_GROUP"] = "missing"
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / "used-build-backend").exists())
        self.assertFalse(self.path_file.exists())

    def test_stale_build_group_fails_before_project_build(self):
        """Backend changes require a reviewed lock update just like runtime changes."""
        self.configure_locked_backend()
        original = self.lock()
        self.manifest.write_text(self.manifest.read_text().replace('"locked-backend==1.0.0"', '"locked-backend==2.0.0"'))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / "used-build-backend").exists())
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)
        self.assertFalse(self.path_file.exists())

    def lock(self):
        """Prepare a lock as a developer would before handing it to CI."""
        result = self.run_command("uv", "lock", "--python", sys.executable)
        self.assertEqual(result.returncode, 0, result.stderr)
        return (self.project / "uv.lock").read_bytes()

    def sync(self):
        """Execute the workflow shell verbatim with fail-fast semantics."""
        return self.run_command("bash", "-e", "-c", self.install["run"])

    def test_exact_version_and_environment_are_used_without_mutating_lock(self):
        """A newer available release cannot replace the committed version."""
        original = self.lock()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        environment_bin = Path(self.path_file.read_text().strip())
        self.assertEqual(environment_bin, self.project / ".venv/bin")
        result = self.run_command(
            str(environment_bin / "python"), "-c",
            "import json,sys,locked_fixture; print(json.dumps([locked_fixture.VERSION,sys.prefix]))",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["1.0.0", str(self.project / ".venv")])
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)

    def test_stale_lock_is_rejected_without_rewriting(self):
        """A manifest update without its lock must fail the required job."""
        original = self.lock()
        self.manifest.write_text(self.manifest.read_text().replace("==1.0.0", "==2.0.0"))
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locked", result.stderr)
        self.assertEqual((self.project / "uv.lock").read_bytes(), original)
        self.assertFalse(self.path_file.exists())

    def test_missing_lock_fails_before_installation(self):
        """Opting in must not silently create an unreviewed lock."""
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.project / "uv.lock").exists())
        self.assertFalse(self.path_file.exists())

    def test_missing_nested_project_cannot_install_from_parent(self):
        """uv's upward discovery must not hide a wrong working-directory input."""
        self.lock()
        parent_lock = (self.project / "uv.lock").read_bytes()
        parent = self.project
        self.project = parent / "missing nested project"
        self.project.mkdir()
        result = self.sync()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.path_file.exists())
        self.assertFalse((self.project / ".venv").exists())
        self.assertFalse((parent / ".venv").exists())
        self.assertEqual((parent / "uv.lock").read_bytes(), parent_lock)

    def test_missing_mypy_cannot_be_installed_over_locked_environment(self):
        """The existing check must use a tool selected by the consumer lock."""
        self.lock()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.environment["PATH"] = self.path_file.read_text().strip() + ":" + self.environment["PATH"]
        self.environment["LOCKED_DEPENDENCIES"] = "true"
        result = self.run_command("bash", "-e", "-c", self.steps["Install MyPy"]["run"])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No module named mypy", result.stderr)

    def test_locked_checks_do_not_fall_back_to_global_tools(self):
        """A globally available executable must not mask a missing locked tool."""
        self.lock()
        result = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        global_bin = self.directory / "global-bin"
        global_bin.mkdir()
        marker = self.directory / "global-tool-ran"
        for name in ("ruff", "pytest"):
            executable = global_bin / name
            executable.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
            executable.chmod(0o755)
        self.environment["PATH"] = (
            self.path_file.read_text().strip() + ":" + str(global_bin)
            + ":" + self.environment["PATH"]
        )
        for step_name, module in (("Run Ruff lint", "ruff"), ("Run Tests", "pytest")):
            with self.subTest(step=step_name):
                command = self.steps[step_name]["run"]
                command = command.replace("${{ inputs.test-args }}", "")
                command = command.replace("${{ inputs.coverage-threshold }}", "0")
                result = self.run_command("bash", "-e", "-c", command)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"No module named {module}", result.stderr)
                self.assertFalse(marker.exists())
