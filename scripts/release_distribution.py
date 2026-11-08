"""Derive a stable distribution from an independently authenticated RC.

This module performs no I/O. A valid compact envelope is not proof that the RC
or its payloads have been authenticated; consumers must verify the entire RC.
"""

from __future__ import annotations

import hashlib
import json
import re

# Exact JSON types prevent booleans from masquerading as versions or counters.
# pylint: disable=unidiomatic-typecheck

KIND = "rc-payloads-v1"
MAX_BYTES = 2_000_000
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}\Z", re.ASCII)
RECEIPT = re.compile(r"release-inputs-[A-Za-z0-9._-]+\.json\Z", re.ASCII)
REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*\Z", re.ASCII)
VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z", re.ASCII)
SHA = re.compile(r"[a-f0-9]{40}\Z", re.ASCII)
SHA256 = re.compile(r"[a-f0-9]{64}\Z", re.ASCII)
FIELDS = {"schema", "kind", "repository", "version", "channel", "tag", "source_sha", "assets", "rc_evidence"}
REFERENCE_FIELDS = {"tag", "manifest_sha256", "source_sha", "run_id", "run_attempt"}


def _require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def enabled(policy: dict) -> bool:
    """Opt in only through the current publisher's reviewed repository policy."""
    _require(isinstance(policy, dict), "Invalid distribution policy")
    if "stable_distribution" not in policy:
        return False
    _require(policy["stable_distribution"] == KIND,
             "Unsupported stable_distribution; expected rc-payloads-v1")
    return True


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        _require(key not in result, "Duplicate distribution JSON field: " + key)
        result[key] = value
    return result


def _constant(_value: str) -> None:
    raise ValueError("Non-finite distribution JSON value")


def _parse(raw: bytes) -> dict:
    _require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES,
             "Invalid distribution manifest size")
    try:
        value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_constant)
    except (ValueError, RecursionError) as error:
        raise ValueError("Invalid distribution JSON") from error
    _require(isinstance(value, dict), "Distribution must be an object")
    return value


def _matches(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _assets(assets: object, *, closed: bool = True) -> list[dict]:
    _require(isinstance(assets, list) and assets, "Distribution contains no assets")
    names = set()
    for item in assets:
        fields = {"name", "size", "sha256"}
        _require(isinstance(item, dict)
                 and (set(item) == fields if closed else fields <= set(item)),
                 "Invalid distribution asset fields")
        name = item["name"]
        _require(_matches(NAME, name) and name.casefold() != "release-manifest.json",
                 "Unsafe distribution asset name")
        _require(name.casefold() not in names, "Duplicate distribution asset name")
        names.add(name.casefold())
        _require(type(item["size"]) is int and item["size"] >= 0,
                 "Invalid distribution asset size")
        _require(_matches(SHA256, item["sha256"]), "Invalid distribution asset checksum")
    return assets


def _validate(value: dict, repository: str, tag: str) -> dict:
    _require(set(value) == FIELDS and type(value.get("schema")) is int
             and value["schema"] == 2 and value.get("kind") == KIND,
             "Unsupported distribution schema or fields")
    _require(_matches(REPOSITORY, repository) and _matches(REPOSITORY, value["repository"])
             and value["repository"].lower() == repository.lower(),
             "Distribution repository mismatch")
    version = value["version"]
    _require(_matches(VERSION, version) and value["channel"] == "stable"
             and value["tag"] == tag == "v" + version,
             "Distribution stable tag/version mismatch")
    _require(_matches(SHA, value["source_sha"]), "Invalid distribution source")
    _assets(value["assets"])
    reference = value["rc_evidence"]
    _require(isinstance(reference, dict) and set(reference) == REFERENCE_FIELDS,
             "Invalid RC evidence reference fields")
    _require(isinstance(reference["tag"], str)
             and re.fullmatch(r"v" + re.escape(version) + r"-rc\.[1-9]\d*", reference["tag"], re.ASCII),
             "RC evidence tag/version mismatch")
    _require(reference["source_sha"] == value["source_sha"]
             and _matches(SHA256, reference["manifest_sha256"]),
             "RC evidence source or manifest hash mismatch")
    for key in ("run_id", "run_attempt"):
        _require(type(reference[key]) is int and reference[key] > 0,
                 "Invalid RC evidence " + key)
    return value


def parse_distribution(raw: bytes, repository: str, tag: str) -> dict:
    """Parse a bounded closed schema without following its untrusted reference."""
    return _validate(_parse(raw), repository, tag)


def stable_distribution(candidate: dict, candidate_raw: bytes) -> dict:
    """Project authenticated schema-1 RC bytes; never classify by extension."""
    _require(isinstance(candidate, dict) and _parse(candidate_raw) == candidate,
             "RC bytes differ from the verified manifest")
    _require(type(candidate.get("schema")) is int and candidate["schema"] == 1
             and candidate.get("channel") == "rc", "Expected a schema-1 RC")
    plan = candidate.get("version_plan")
    _require(isinstance(plan, dict) and plan.get("promotion") == "promote-bytes",
             "Compact promotion requires the accepted promote-bytes plan")
    assets = _assets(candidate.get("assets"), closed=False)
    expected = {item["name"]: item for item in assets}
    build = candidate.get("build_receipts")
    validation = candidate.get("validation_receipts", [])
    _require(isinstance(build, list) and build, "Missing declared build receipts")
    _require(isinstance(validation, list), "Invalid validation receipts")
    omit = set()
    for receipt in build + validation:
        _require(isinstance(receipt, dict), "Invalid receipt declaration")
        name = receipt.get("name")
        _require(_matches(RECEIPT, name), "Only declared release-inputs receipts may be withheld")
        _require(name not in omit and name in expected
                 and receipt.get("sha256") == expected[name]["sha256"],
                 "Receipt declaration differs from the RC inventory")
        omit.add(name)
    receipt_names = {name for name in expected if name.startswith("release-inputs-")
                     and name.endswith(".json")}
    _require(receipt_names == omit, "Undeclared release-inputs asset")
    fuzz_receipts = [item for item in validation if item.get("name") == "release-inputs-fuzz.json"]
    if fuzz_receipts or "fuzzing_profile" in candidate:
        _require(bool(fuzz_receipts) and "fuzz-report.json" in expected,
                 "Incomplete declared fuzz evidence")
        _require(isinstance(candidate.get("fuzzing_profile"), dict)
                 and candidate["fuzzing_profile"], "Missing RC fuzz profile")
        omit.add("fuzz-report.json")
    shipped = [{key: item[key] for key in ("name", "size", "sha256")}
               for item in sorted(assets, key=lambda item: item["name"])
               if item["name"] not in omit]
    value = {
        "schema": 2, "kind": KIND, "repository": candidate["repository"],
        "version": candidate["version"], "channel": "stable",
        "tag": "v" + candidate["version"], "source_sha": candidate["source_sha"],
        "assets": shipped,
        "rc_evidence": {
            "tag": candidate["tag"], "manifest_sha256": hashlib.sha256(candidate_raw).hexdigest(),
            "source_sha": candidate["source_sha"], "run_id": candidate["run_id"],
            "run_attempt": candidate["run_attempt"],
        },
    }
    return _validate(value, candidate["repository"], value["tag"])


def verify_distribution(compact: dict, candidate: dict, candidate_raw: bytes) -> None:
    """Require the complete deterministic projection after full RC verification."""
    _require(isinstance(compact, dict), "Invalid stable distribution")
    _validate(compact, candidate["repository"], "v" + candidate["version"])
    _require(compact == stable_distribution(candidate, candidate_raw),
             "Stable distribution differs from the verified RC projection")
