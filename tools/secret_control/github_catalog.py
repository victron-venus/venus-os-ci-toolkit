"""Existing GitHub destinations bound to selected encrypted catalog revisions."""

import json
import uuid

from . import catalog_delivery
from .catalog import github_target, integer, keys, require, text
from .github_api import API, deliver, metadata, seal
from .private_io import read_private


def validate_manifest(value):
    keys(value, {"schema_version", "operation_id", "actor", "catalog", "secrets"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 2,
            "unsupported GitHub publication schema")
    try:
        valid_id = str(uuid.UUID(value["operation_id"])) == value["operation_id"]
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    require(valid_id, "invalid GitHub publication identifier")
    keys(value["actor"], {"login", "id"})
    require(text(value["actor"]["login"], r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", 39)
            and integer(value["actor"]["id"]), "unapproved GitHub publishing identity")
    reference = value["catalog"]
    keys(reference, {"catalog_id", "generation", "sha256"})
    try:
        valid_id = str(uuid.UUID(reference["catalog_id"])) == reference["catalog_id"]
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    require(valid_id and integer(reference["generation"])
            and text(reference["sha256"], r"[0-9a-f]{64}", 64), "invalid catalog reference")
    require(type(value["secrets"]) is list and 1 <= len(value["secrets"]) <= 100,
            "invalid GitHub publication targets")
    destinations = set()
    for item in value["secrets"]:
        keys(item, {"entry_id", "revision", "target", "expected"})
        require(text(item["entry_id"], r"[a-z][a-z0-9_-]{0,63}")
                and integer(item["revision"]), "invalid catalog entry reference")
        require(type(item["target"]) is dict, "invalid GitHub target")
        destination = github_target(item["target"])
        require(destination not in destinations, "duplicate GitHub destination")
        destinations.add(destination)
        target = item["target"]
        fields = {"created_at", "updated_at"}
        if target["scope"] == "organization":
            fields |= {"visibility", "selected_repository_ids"}
        keys(item["expected"], fields)
        metadata({"name": target["name"], **item["expected"]}, target["name"], target["scope"])
        if target["scope"] == "organization":
            require(all(item["expected"][key] == target[key]
                        for key in ("visibility", "selected_repository_ids")),
                    "GitHub manifest cannot change catalog visibility or selection")
    return value


class CatalogAPI(API):
    """Reuse the fixed-origin transport, allow only one explicit destination."""

    def __init__(self, token_file, target):
        # Validate again at the credential boundary; this adapter is also importable.
        github_target(target)
        self.target = json.loads(json.dumps(target))
        raw = read_private(token_file, 4096)
        try:
            token = raw.decode("ascii").strip()
        except UnicodeError:
            token = ""
        require(text(token, r"[A-Za-z0-9_]{20,512}", 512),
                "invalid GitHub authentication input")
        self.token = token
        self.org = "/orgs/" + target["owner"]["login"]
        self.membership = "/user/memberships/orgs/" + target["owner"]["login"]
        self.repo = ("/repos/" + target["repository"]["full_name"]
                     if target["scope"] == "repository" else None)

    def prefix(self, item):
        require(item["name"] == self.target["name"]
                and item["scope"] == self.target["scope"]
                and item.get("secret_store", "actions") == self.target.get("secret_store", "actions"),
                "unapproved GitHub destination")
        return self.org if self.target["scope"] == "organization" else self.repo

    def secret_path(self, item):
        return self.prefix(item) + "/" + self.target.get("secret_store", "actions") + "/secrets"

    def validate_request(self, method, path, body):
        prefix = self.prefix(self.target)
        namespace = self.secret_path(self.target)
        secret = namespace + "/" + self.target["name"]
        allowed = {"/user", prefix, namespace + "/public-key", secret}
        organization = self.target["scope"] == "organization"
        if organization:
            allowed.add(self.membership)
            allowed.update(secret + "/repositories?per_page=100&page=" + str(page)
                           for page in range(1, 11))
        fields = {"encrypted_value", "key_id"}
        policy_matches = True
        if organization:
            fields.add("visibility")
            policy_matches = type(body) is dict and body.get("visibility") == self.target["visibility"]
            if self.target["visibility"] == "selected":
                fields.add("selected_repository_ids")
                policy_matches = policy_matches and body.get("selected_repository_ids") == self.target["selected_repository_ids"]
        valid_put = (method == "PUT" and path == secret and type(body) is dict
                     and set(body) == fields and policy_matches)
        require((method == "GET" and path in allowed and body is None) or valid_put,
                "unapproved GitHub API operation")


def verify_owner(api, target, actor):
    observed = api.actor()
    require(integer(observed.get("id"))
            and (observed.get("id"), observed.get("login")) == (actor["id"], actor["login"]),
            "GitHub publishing identity mismatch")
    owner = target["owner"]
    if target["scope"] == "organization":
        current = api.organization()
        require(integer(current.get("id"))
                and all(current.get(key) == owner[key] for key in ("id", "login", "type")),
                "GitHub organization identity mismatch")
        member = api.organization_membership()
        require(member.get("state") == "active" and member.get("role") == "admin"
                and integer((member.get("organization") or {}).get("id"))
                and integer((member.get("user") or {}).get("id"))
                and (member.get("organization") or {}).get("id") == owner["id"]
                and (member.get("user") or {}).get("id") == actor["id"],
                "GitHub organization administrator membership required")
    else:
        current = api.repository()
        require(integer(current.get("id")) and integer((current.get("owner") or {}).get("id"))
                and all(current.get(key) == target["repository"][key] for key in ("id", "full_name"))
                and all((current.get("owner") or {}).get(key) == owner[key]
                        for key in ("id", "login", "type"))
                and (current.get("permissions") or {}).get("admin") is True,
                "GitHub repository ownership or permission mismatch")
        require(current.get("archived") is False and current.get("disabled") is False,
                "GitHub repository is unavailable")


def publish_catalog(value, manifest_sha256, token_file, *, catalog_file, identity, age,
                    apply=False, journal=None, encrypt=seal, api_factory=None):
    validate_manifest(value)
    require(catalog_file is not None and identity is not None,
            "catalog publication requires catalog and identity paths")
    reference = value["catalog"]
    source = catalog_delivery.load(catalog_file, identity, age,
                                   expected_sha256=reference["sha256"],
                                   generation=reference["generation"],
                                   catalog_id=reference["catalog_id"])
    require(source["schema_version"] == 2, "GitHub targets require catalog schema two")
    payloads = []
    for item in value["secrets"]:
        entry = catalog_delivery.entry(source, item["entry_id"], item["revision"])
        require(item["target"] in entry["targets"], "unapproved catalog destination")
        payload = entry["value"].encode("utf-8")
        require(bool(payload), "invalid GitHub secret payload")
        require(item["target"]["name"] != "BOT_PAT" or (b"\r" not in payload and b"\n" not in payload),
                "BOT_PAT must be a single line; normalize its source explicitly")
        payloads.append(payload)

    def prepare():
        factory = CatalogAPI if api_factory is None else api_factory
        operations = []
        for item in value["secrets"]:
            target = item["target"]
            api = factory(token_file, target)
            selected = {**target, "expected": item["expected"]}
            operations.append((api, selected, lambda api=api, target=target:
                               verify_owner(api, target, value["actor"])))
        return operations

    return deliver(value, payloads, prepare, manifest_sha256, apply=apply,
                   journal=journal, encrypt=encrypt,
                   context={"catalog": reference, "targets": [item["target"] for item in value["secrets"]]})
