"""Offline verification against an independently approved ciphertext checkpoint."""

import hashlib
import hmac
import re
import shutil

from . import catalog
from .catalog import require
from .private_io import decrypt_bytes, read_private


def check(path, identity, age, *, expected_sha256, generation):
    """Check the exact bytes once, then decrypt those same bytes without cloud access."""
    require(
        type(expected_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is not None,
        "independent ciphertext SHA-256 required",
    )
    require(catalog.integer(generation), "approved catalog generation required")
    encrypted = read_private(path)
    require(
        hmac.compare_digest(hashlib.sha256(encrypted).hexdigest(), expected_sha256),
        "recovery ciphertext digest mismatch",
    )
    binary = shutil.which(age) if "/" not in age else age
    require(binary is not None, "required external tool unavailable")
    value = catalog.decode(decrypt_bytes(encrypted, identity, binary))
    require(value["generation"] == generation, "recovery catalog generation mismatch")
    return {
        **catalog.plan(value),
        "status": "verified",
        "scope": "local-ciphertext-and-catalog",
        "ciphertext_sha256": expected_sha256,
        "cloud_verified": False,
        "key_custody_verified": False,
    }
