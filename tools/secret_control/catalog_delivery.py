"""Pinned encrypted catalog reads and explicitly selected local delivery."""

import hashlib
import hmac
from pathlib import Path
import shutil

from . import catalog
from .catalog import require
from .materialize import materialize_bytes
from .private_io import decrypt_bytes, read_private


def load(path, identity, age, *, expected_sha256, generation, catalog_id=None):
    """Read ciphertext once; approve its digest before accessing an identity."""
    require(catalog.text(expected_sha256, r"[0-9a-f]{64}", 64),
            "approved ciphertext digest required")
    require(catalog.integer(generation), "approved catalog generation required")
    encrypted = read_private(path)
    require(hmac.compare_digest(hashlib.sha256(encrypted).hexdigest(), expected_sha256),
            "catalog ciphertext digest mismatch")
    binary = shutil.which(age) if "/" not in age else age
    require(binary is not None, "required external tool unavailable")
    value = catalog.decode(decrypt_bytes(encrypted, identity, binary))
    require(value["generation"] == generation, "catalog generation mismatch")
    require(catalog_id is None or value["catalog_id"] == catalog_id,
            "catalog identifier mismatch")
    return value


def entry(value, name, revision):
    require(catalog.text(name, r"[a-z][a-z0-9_-]{0,63}") and catalog.integer(revision),
            "explicit catalog entry and revision required")
    found = next((item for item in value["entries"] if item["id"] == name), None)
    require(found is not None and found["revision"] == revision,
            "catalog entry or revision mismatch")
    return found


def materialize(path, identity, age, *, expected_sha256, generation, name, revision,
                output):
    """Deliver one pinned revision, never the entire decrypted catalog or a hash."""
    value = load(path, identity, age, expected_sha256=expected_sha256,
                 generation=generation)
    selected = entry(value, name, revision)
    payload = selected["value"].encode("utf-8")
    result = materialize_bytes(payload, Path(output), hashlib.sha256(payload).hexdigest())
    return {"status": result["status"], "generation": generation, "revision": revision,
            "bytes": len(payload), "values": "redacted", "consumer_verified": False}
