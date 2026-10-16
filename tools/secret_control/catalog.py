"""Strict catalog validation. Never include input values in errors."""

import json
import re
import uuid

MAX_CATALOG = 2 * 1024 * 1024
MAX_VALUE = 16 * 1024


class ControlError(Exception):
    """A safe, fixed diagnostic suitable for public logs."""


def require(ok, message):
    if not ok:
        raise ControlError(message)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def keys(obj, required, optional=()):
    require(
        type(obj) is dict
        and set(required) <= obj.keys() <= set(required) | set(optional),
        "invalid object fields",
    )


def integer(value):
    return type(value) is int and 0 < value < 2**53


def text(value, pattern, limit=256):
    return (
        type(value) is str
        and len(value) <= limit
        and re.fullmatch(pattern, value) is not None
    )


def decode(raw):
    require(type(raw) is bytes and len(raw) <= MAX_CATALOG, "catalog size limit")
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ControlError("invalid JSON number")
            ),
        )
    except (ValueError, UnicodeError, RecursionError):
        raise ControlError("invalid catalog JSON") from None
    validate(value)
    return value


def validate(catalog):
    keys(catalog, {"schema_version", "catalog_id", "generation", "entries"})
    require(
        type(catalog["schema_version"]) is int and catalog["schema_version"] == 1,
        "unsupported catalog schema",
    )
    try:
        valid_uuid = str(uuid.UUID(catalog["catalog_id"])) == catalog["catalog_id"]
    except (ValueError, TypeError, AttributeError):
        valid_uuid = False
    require(valid_uuid, "invalid catalog identifier")
    require(integer(catalog["generation"]), "invalid catalog generation")
    require(
        type(catalog["entries"]) is list and 0 < len(catalog["entries"]) <= 1000,
        "invalid catalog entries",
    )
    ids, destinations = set(), set()
    for entry in catalog["entries"]:
        keys(entry, {"id", "revision", "value", "targets"})
        require(
            text(entry["id"], r"[a-z][a-z0-9_-]{0,63}") and entry["id"] not in ids,
            "invalid or duplicate entry identifier",
        )
        ids.add(entry["id"])
        require(integer(entry["revision"]), "invalid entry revision")
        require(
            type(entry["value"]) is str and "\x00" not in entry["value"],
            "invalid secret value",
        )
        try:
            require(
                len(entry["value"].encode("utf-8")) <= MAX_VALUE, "secret size limit"
            )
        except UnicodeError:
            raise ControlError("invalid secret encoding") from None
        require(
            type(entry["targets"]) is list and 0 < len(entry["targets"]) <= 20,
            "invalid targets",
        )
        for target in entry["targets"]:
            require(type(target) is dict, "invalid target")
            if target.get("type") == "oci":
                keys(
                    target,
                    {"type", "region", "compartment_id", "vault_id", "key_id", "name"},
                )
                require(
                    text(target["region"], r"[a-z][a-z0-9-]+", 64), "invalid region"
                )
                for field, kind in [
                    ("compartment_id", "compartment"),
                    ("vault_id", "vault"),
                    ("key_id", "key"),
                ]:
                    require(
                        text(target[field], r"ocid1\." + kind + r"\.[A-Za-z0-9._-]+"),
                        "invalid OCI resource identifier",
                    )
                require(
                    text(target["name"], r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"),
                    "invalid OCI secret name",
                )
                dest = ("oci", target["region"], target["vault_id"], target["name"])
            elif target.get("type") == "hcp":
                keys(
                    target,
                    {"type", "key", "category"},
                    {"workspace_id", "variable_set_id"},
                )
                require(
                    ("workspace_id" in target) != ("variable_set_id" in target),
                    "one HCP scope required",
                )
                scope = (
                    "workspace_id" if "workspace_id" in target else "variable_set_id"
                )
                require(
                    text(target[scope], r"(ws|varset)-[A-Za-z0-9]{16}", 100),
                    "invalid HCP scope",
                )
                require(
                    target[scope].startswith(
                        "ws-" if scope == "workspace_id" else "varset-"
                    ),
                    "wrong HCP scope type",
                )
                require(
                    text(
                        target["key"],
                        r"[A-Za-z_][A-Za-z0-9_]*"
                        if target["category"] == "env"
                        else r"[A-Za-z_][A-Za-z0-9_-]*",
                        128,
                    ),
                    "invalid variable key",
                )
                require(
                    target["category"] in ("env", "terraform"),
                    "invalid variable category",
                )
                dest = ("hcp", scope, target[scope], target["category"], target["key"])
            else:
                raise ControlError("unsupported target")
            require(dest not in destinations, "duplicate destination")
            destinations.add(dest)
    return catalog


def lineage(current, previous=None):
    """Single-writer catalog revisions are verified before new encrypted files are written."""
    if previous is None:
        require(
            current["generation"] == 1
            and all(e["revision"] == 1 for e in current["entries"]),
            "initial catalog requires generation and revisions one",
        )
        return
    require(
        current["catalog_id"] == previous["catalog_id"]
        and current["generation"] == previous["generation"] + 1,
        "catalog lineage mismatch",
    )
    old = {e["id"]: e for e in previous["entries"]}
    new = {e["id"]: e for e in current["entries"]}
    require(old.keys() <= new.keys(), "entry removal is not supported")
    for name, entry in new.items():
        prior = old.get(name)
        if prior is None:
            require(entry["revision"] == 1, "new entry revision must be one")
        else:
            changed = (entry["value"], entry["targets"]) != (
                prior["value"],
                prior["targets"],
            )
            require(
                entry["revision"] == prior["revision"] + int(changed),
                "entry revision does not match change",
            )


def plan(catalog):
    return {
        "schema_version": 1,
        "generation": catalog["generation"],
        "entries": len(catalog["entries"]),
        "targets": {
            kind: sum(
                t["type"] == kind for e in catalog["entries"] for t in e["targets"]
            )
            for kind in ("oci", "hcp")
        },
        "values": "redacted",
        "deletions": 0,
    }
