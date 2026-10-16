"""A self-contained, synthetic-only recovery exercise using official age tools.

No existing catalogs, identities or cloud credentials are read. This verifies local
recovery mechanics, not off-device backup availability or production recovery.
"""

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

from . import catalog
from .catalog import ControlError, require
from .materialize import MaterializationError, materialize_bytes
from .private_io import decrypt, new_private, process, read_private, trusted_tool


def _identity(directory, name, keygen):
    raw = process([keygen], timeout=20, limit=8192)
    recipient = process([keygen, "-y"], raw, timeout=20, limit=8192).decode().strip()
    require(
        catalog.text(recipient, r"age1[0-9a-z]{58}", 62),
        "recovery drill received an invalid recipient",
    )
    path = directory / name
    new_private(path, raw)
    return path, recipient


def _encrypt(document, previous, destination, recipients, age):
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    catalog.decode(raw)
    catalog.lineage(document, previous)
    argv = [age, "--encrypt"]
    for recipient in recipients:
        argv.extend(["--recipient", recipient])
    ciphertext = process(argv, raw, timeout=20)
    require(
        ciphertext.startswith(b"age-encryption.org/v1\n"),
        "recovery drill received invalid ciphertext",
    )
    new_private(destination, ciphertext)
    return raw


def _refused(operation, error_type, expected_reason):
    try:
        operation()
    except error_type as error:
        # Timeouts, missing files and unrelated failures are not evidence of a
        # successful negative check. Only the intended refusal counts.
        require(str(error) == expected_reason, "recovery drill unexpected refusal")
    else:
        raise ControlError("recovery drill expected refusal was not observed")


def _exercise(directory, age, keygen):
    operator, operator_recipient = _identity(directory, "operator.key", keygen)
    recovery, recovery_recipient = _identity(directory, "recovery.key", keygen)
    wrong, wrong_recipient = _identity(directory, "wrong.key", keygen)
    require(
        len({operator_recipient, recovery_recipient, wrong_recipient}) == 3,
        "recovery drill identities are not independent",
    )
    recipients = [operator_recipient, recovery_recipient]
    first = {
        "schema_version": 1,
        "catalog_id": str(uuid.uuid4()),
        "generation": 1,
        "entries": [
            {
                "id": "synthetic-recovery-profile",
                "revision": 1,
                "value": '{"synthetic":true,"revision":1,"label":"fictional"}\n',
                "targets": [
                    {
                        "type": "hcp",
                        "category": "terraform",
                        "key": "synthetic_recovery_profile",
                        "workspace_id": "ws-0000000000000001",
                    }
                ],
            }
        ],
    }
    second = json.loads(json.dumps(first))
    second["generation"] = 2
    second["entries"][0]["revision"] = 2
    second["entries"][0]["value"] = (
        '{"synthetic":true,"revision":2,"label":"fictional-é"}\n'
    )
    paths = [directory / "generation-1.age", directory / "generation-2.age"]
    raw_first = _encrypt(first, None, paths[0], recipients, age)
    raw_second = _encrypt(second, first, paths[1], recipients, age)
    for path, expected in zip(paths, [raw_first, raw_second]):
        for identity in [operator, recovery]:
            actual = decrypt(path, identity, age)
            require(actual == expected, "recovery drill decryption mismatch")
            catalog.decode(actual)
    checks = {"independent_ephemeral_keys": True, "both_keys_both_generations": True}

    # Retain the latest trusted generation while testing an attempted downgrade.
    # Encryption alone does not detect replay of a valid old ciphertext.
    _refused(
        lambda: catalog.lineage(first, second),
        ControlError,
        "catalog lineage mismatch",
    )
    checks["rollback_against_latest_generation_refused"] = True
    _refused(
        lambda: decrypt(paths[1], wrong, age),
        ControlError,
        "external tool failed; output withheld",
    )
    checks["wrong_key_refused"] = True
    corrupted = bytearray(read_private(paths[1]))
    corrupted[-1] ^= 1
    tampered = directory / "tampered.age"
    new_private(tampered, bytes(corrupted))
    _refused(
        lambda: decrypt(tampered, recovery, age),
        ControlError,
        "external tool failed; output withheld",
    )
    checks["tampering_refused"] = True

    operator.unlink()
    require(not operator.exists(), "recovery drill operator key removal failed")
    for path, expected in zip(paths, [raw_first, raw_second]):
        require(
            decrypt(path, recovery, age) == expected,
            "recovery drill failed after operator key loss",
        )
    recovered = catalog.decode(decrypt(paths[1], recovery, age))
    checks["recovery_after_operator_key_loss"] = True

    # The digest comes from the original expected payload, independently of the
    # recovered bytes being checked by materialize_bytes.
    expected_payload = second["entries"][0]["value"].encode()
    digest = hashlib.sha256(expected_payload).hexdigest()
    payload = recovered["entries"][0]["value"].encode()
    destination = directory / "recovered-profile.json"
    created = materialize_bytes(payload, destination, digest)
    require(
        created["status"] == "created"
        and read_private(destination) == expected_payload,
        "recovery drill materialization mismatch",
    )
    unchanged = materialize_bytes(payload, destination, digest)
    require(unchanged["status"] == "unchanged", "recovery drill repeat write mismatch")
    bad_destination = directory / "wrong-digest.json"
    _refused(
        lambda: materialize_bytes(payload, bad_destination, "0" * 64),
        MaterializationError,
        "bundle digest mismatch",
    )
    require(not bad_destination.exists(), "recovery drill wrote an unverified payload")
    replacement = payload + b"different"
    _refused(
        lambda: materialize_bytes(
            replacement, destination, hashlib.sha256(replacement).hexdigest()
        ),
        MaterializationError,
        "existing output differs; use a new destination",
    )
    require(
        read_private(destination) == expected_payload, "recovery drill changed output"
    )
    checks["private_materialization_verified"] = True
    checks["digest_mismatch_refused_without_write"] = True
    checks["existing_output_not_overwritten"] = True
    for path in directory.iterdir():
        info = path.lstat()
        require(
            stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1,
            "recovery drill temporary file is not private",
        )
    checks["temporary_files_private"] = True
    return checks


def run_drill(age: str, age_keygen: str) -> dict:
    """Run an offline synthetic drill and delete all temporary data before success.

    The tools are explicitly selected executables. Only newly generated native
    age identities are used; no user key, catalog, cloud or service is accessed.
    Filesystem deletion is verified, but is not a secure-erasure guarantee.
    """
    try:
        age, keygen = trusted_tool(age), trusted_tool(age_keygen)
        with tempfile.TemporaryDirectory(prefix="secret-control-drill-") as temporary:
            directory = Path(temporary).resolve(strict=True)
            info = directory.stat()
            require(
                info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "recovery drill temporary directory is not private",
            )
            checks = _exercise(directory, age, keygen)
        require(not directory.exists(), "recovery drill temporary cleanup failed")
        checks["temporary_files_removed"] = True
        return {
            "status": "passed",
            "scope": "offline-local-recovery-mechanics",
            "syntheticOnly": True,
            "generations": 2,
            "checks": checks,
        }
    except Exception:
        # Tool/OS exceptions can contain paths, key material or private output.
        raise ControlError(
            "synthetic recovery drill failed; details withheld"
        ) from None
