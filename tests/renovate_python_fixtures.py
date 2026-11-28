"""Create inert local package fixtures without executing any build backend."""

import io
import sys
import tarfile
import zipfile
from pathlib import Path


def create(root: Path, case: str) -> None:
    root.mkdir(parents=True, exist_ok=False)
    marker = root / "backend-executed.txt"
    backend = f"""from pathlib import Path
MARKER = Path({str(marker)!r})
def _mark():
    MARKER.write_text("inert test metadata backend executed\\n")
def get_requires_for_build_wheel(config_settings=None):
    _mark()
    return []
def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    _mark()
    name = "boundary_canary-1.0.0.dist-info"
    dest = Path(metadata_directory) / name
    dest.mkdir(exist_ok=True)
    (dest / "METADATA").write_text("Metadata-Version: 2.3\\nName: boundary-canary\\nVersion: 1.0.0\\n")
    return name
get_requires_for_build_editable = get_requires_for_build_wheel
prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel
def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _mark()
    from zipfile import ZipFile
    filename = "boundary_canary-1.0.0-py3-none-any.whl"
    with ZipFile(Path(wheel_directory) / filename, "w") as wheel:
        wheel.writestr("boundary_canary-1.0.0.dist-info/METADATA", "Metadata-Version: 2.3\\nName: boundary-canary\\nVersion: 1.0.0\\n")
        wheel.writestr("boundary_canary-1.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\\nGenerator: inert-fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
        wheel.writestr("boundary_canary-1.0.0.dist-info/RECORD", "")
    return filename
build_wheel = build_editable
"""
    dynamic_project = """[build-system]
requires = []
build-backend = "backend"
backend-path = ["."]
[project]
name = "boundary-canary"
requires-python = ">=3.9"
dynamic = ["version", "dependencies"]
"""
    if case.endswith("project"):
        (root / "backend.py").write_text(backend)
        static = case in {"static-project", "post-static-project", "post-nested-static-project"}
        source = (
            (
                '[project]\nname = "boundary-canary"\nversion = "1.0.0"\n'
                'requires-python = ">=3.9"\ndependencies = []\n'
            )
            if static
            else dynamic_project
        )
        (root / "pyproject.toml").write_text(source)
        if case == "compile-project":
            (root / "requirements.txt").write_text(
                "# Local fixture\n#    uv pip compile pyproject.toml --generate-hashes --output-file=requirements.txt\n"
            )
        else:
            source_kind = "virtual" if static else "editable"
            version = "1.0.0" if case.startswith("post-") else "0.0.0"
            version_line = "" if case in {"post-dynamic-project", "post-nested-dynamic-project"} else f'version = "{version}"\n'
            (root / "uv.lock").write_text(
                'version = 1\nrevision = 5\nrequires-python = ">=3.9"\n\n[[package]]\n'
                f'name = "boundary-canary"\n{version_line}'
                f'source = {{{source_kind} = "."}}\n'
            )
        return

    index = root / "index"
    index.mkdir()
    if case == "wheel":
        prefix = "boundary_canary-1.0.0.dist-info/"
        with zipfile.ZipFile(
            index / "boundary_canary-1.0.0-py3-none-any.whl", "w"
        ) as wheel:
            wheel.writestr(
                prefix + "METADATA",
                "Metadata-Version: 2.3\nName: boundary-canary\nVersion: 1.0.0\n",
            )
            wheel.writestr(
                prefix + "WHEEL",
                "Wheel-Version: 1.0\nGenerator: inert-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            )
            wheel.writestr(prefix + "RECORD", "")
    else:
        with tarfile.open(index / "boundary_canary-1.0.0.tar.gz", "w:gz") as archive:
            for name, text in {
                "pyproject.toml": dynamic_project,
                "backend.py": backend,
            }.items():
                data = text.encode()
                entry = tarfile.TarInfo("boundary_canary-1.0.0/" + name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        # A repository-level false setting must not defeat the CLI guard.
        (root / "uv.toml").write_text("no-build = false\n[pip]\nno-build = false\n")
    (root / "requirements.in").write_text(
        f"--no-index\n--find-links {index}\n"
        + ("--no-binary :all:\n" if case != "wheel" else "")
        + "boundary-canary==1.0.0\n"
    )
    (root / "requirements.txt").write_text(
        "# Local fixture\n#    uv pip compile requirements.in --generate-hashes --output-file=requirements.txt\n"
        "boundary-canary==1.0.0\n"
    )


if __name__ == "__main__":
    assert len(sys.argv) == 3
    assert sys.argv[2] in {
        "sdist",
        "wheel",
        "dynamic-project",
        "static-project",
        "compile-project",
        "post-dynamic-project",
        "post-static-project",
        "post-nested-dynamic-project",
        "post-nested-static-project",
    }
    create(Path(sys.argv[1]), sys.argv[2])
