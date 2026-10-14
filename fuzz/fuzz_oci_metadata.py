"""Exercise the owned OCI JSON and tar-member parsers without filesystem I/O."""

import json
import sys
import tarfile
from pathlib import Path

import atheris

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
with atheris.instrument_imports():
    import merge_oci_archives as oci


def test_one_input(data: bytes) -> None:
    """Check deterministic JSON parsing and safe, idempotent member names."""
    try:
        value = oci.document(data)
    except ValueError:
        pass
    else:
        assert isinstance(value, dict)
        assert value == oci.document(data)
        # Standard JSON serialization preserves accepted numeric values, including
        # JSON exponent overflow. Named nonstandard constants remain forbidden.
        if "Infinity" not in json.dumps(value):
            assert value == oci.document(oci.canonical(value))
    entry = tarfile.TarInfo(data.decode("utf-8", errors="replace"))
    if data and data[0] & 1:
        entry.type = tarfile.DIRTYPE
    try:
        name = oci.member_name(entry)
    except ValueError:
        return
    assert name and not name.startswith("/") and "\\" not in name and "\0" not in name
    assert all(part not in {"", ".", ".."} for part in name.split("/"))
    entry.name = name
    assert oci.member_name(entry) == name


if __name__ == "__main__":
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()
