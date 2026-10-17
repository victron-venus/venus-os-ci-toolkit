"""Explicit delivery to reviewed GitHub secret destinations; never an issuer or vault."""

import base64
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import fcntl
import hashlib
import hmac
import http.client
import importlib
import json
import os
from pathlib import Path
import ssl
import stat
import time
import uuid

from .catalog import ControlError, MAX_CATALOG, MAX_VALUE, _pairs, integer, keys, require, text
from .private_io import new_private, private_parent, read_private

SECRET_NAMES = frozenset(("BOT_PAT", "RENOVATE_SIGNING_KEY"))
PYNACL_VERSION = "1.6.2"
# Selected-repository pages contain full repository objects, not just IDs.
MAX_RESPONSE = 2 * 1024 * 1024
# A 16 KiB value after sealed-box/base64 plus 1,000 immutable repository IDs.
MAX_REQUEST = 64 * 1024
PUBLIC_KEY_PATH = "/actions/secrets/public-key"


def decode(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, RecursionError):
        raise ControlError("invalid GitHub publication JSON") from None


def timestamp(value):
    require(text(value, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", 20),
            "invalid secret metadata timestamp")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ControlError("invalid secret metadata timestamp") from None


def metadata(value, name, scope="repository"):
    if value is None:
        return None
    require(type(value) is dict and value.get("name") == name,
            "GitHub secret name mismatch")
    result = {key: value.get(key) for key in ("created_at", "updated_at")}
    require(timestamp(result["created_at"]) <= timestamp(result["updated_at"]),
            "invalid secret metadata ordering")
    if scope == "organization":
        require(value.get("visibility") in ("all", "private", "selected"),
                "invalid organization secret visibility")
        result["visibility"] = value["visibility"]
    return result


def manifest(raw):
    require(type(raw) is bytes and len(raw) <= MAX_CATALOG, "GitHub manifest size limit")
    value = decode(raw)
    if type(value) is dict and value.get("schema_version") == 2:
        from .github_catalog import validate_manifest

        return validate_manifest(value)
    require(len(raw) <= 65536, "GitHub manifest size limit")
    keys(value, {"schema_version", "operation_id", "actor", "organization", "repository", "secrets"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1,
            "unsupported GitHub publication schema")
    try:
        valid_id = str(uuid.UUID(value["operation_id"])) == value["operation_id"]
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    require(valid_id, "invalid GitHub publication identifier")
    keys(value["actor"], {"login", "id"})
    require(text(value["actor"]["login"], r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", 39) and integer(value["actor"]["id"]),
            "unapproved GitHub publishing identity")
    keys(value["organization"], {"login", "id"})
    org = value["organization"]
    require(text(org["login"], r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", 39)
            and integer(org["id"]), "invalid GitHub organization binding")
    keys(value["repository"], {"full_name", "id", "owner_id"})
    repo = value["repository"]
    require(text(repo["full_name"], r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]{1,100}", 140)
            and repo["full_name"].split("/")[0] == org["login"]
            and repo["full_name"].split("/")[1] not in (".", "..")
            and integer(repo["id"]) and integer(repo["owner_id"])
            and repo["owner_id"] == org["id"], "unapproved GitHub repository")
    require(type(value["secrets"]) is list and 1 <= len(value["secrets"]) <= 2,
            "invalid GitHub publication targets")
    seen = set()
    for item in value["secrets"]:
        keys(item, {"scope", "name", "source", "source_sha256", "expected"})
        require(type(item["name"]) is str and item["name"] in SECRET_NAMES
                and item["name"] not in seen, "unapproved or duplicate GitHub secret")
        require((item["scope"], item["name"]) in
                (("organization", "BOT_PAT"), ("repository", "RENOVATE_SIGNING_KEY")),
                "unapproved GitHub secret scope")
        seen.add(item["name"])
        require(type(item["source"]) is str and len(item["source"]) <= 4096
                and Path(item["source"]).is_absolute(), "absolute secret source required")
        require(text(item["source_sha256"], r"[0-9a-f]{64}", 64),
                "independent source digest required")
        # A missing repository secret may be inherited from an organization.
        # Never create an override/shadow secret as an accidental side effect.
        fields = {"created_at", "updated_at"}
        if item["scope"] == "organization":
            fields |= {"visibility", "selected_repository_ids"}
        keys(item["expected"], fields)
        metadata({"name": item["name"], **item["expected"]}, item["name"], item["scope"])
        if item["scope"] == "organization":
            selection(item["expected"]["selected_repository_ids"], item["expected"]["visibility"])
    return value


def selection(ids, visibility):
    require(type(ids) is list and len(ids) <= 1000 and all(integer(i) for i in ids)
            and ids == sorted(set(ids)) and (visibility == "selected" or not ids),
            "invalid organization secret selection")
    return ids


def seal(value, key):
    """GitHub's documented libsodium sealed-box format, entirely in memory."""
    try:
        nacl = importlib.import_module("nacl")
        public = importlib.import_module("nacl.public")
    except ImportError:
        raise ControlError("install the pinned optional GitHub dependency") from None
    require(nacl.__version__ == PYNACL_VERSION, "PyNaCl version differs from pin")
    return base64.b64encode(public.SealedBox(public.PublicKey(key)).encrypt(value)).decode("ascii")


class API:
    """Fixed HTTPS origin; no redirects, ambient proxies, retries or diagnostics."""

    def __init__(self, token_file, binding):
        raw = read_private(token_file, 4096)
        try:
            token = raw.decode("ascii").strip()
        except UnicodeError:
            raise ControlError("invalid GitHub authentication input") from None
        require(text(token, r"[A-Za-z0-9_]{20,512}", 512),
                "invalid GitHub authentication input")
        self.token = token
        self.repo = "/repos/" + binding["repository"]["full_name"]
        self.org = "/orgs/" + binding["organization"]["login"]
        self.membership = "/user/memberships/orgs/" + binding["organization"]["login"]

    def validate_request(self, method, path, body):
        allowed = {"/user", self.repo, self.org, self.membership,
                   self.repo + PUBLIC_KEY_PATH,
                   self.org + PUBLIC_KEY_PATH}
        repo_secret = self.repo + "/actions/secrets/RENOVATE_SIGNING_KEY"
        org_secret = self.org + "/actions/secrets/BOT_PAT"
        selected_pages = {org_secret + "/repositories?per_page=100&page=" + str(i)
                          for i in range(1, 11)}
        fields = {"encrypted_value", "key_id"}
        valid_put = method == "PUT" and type(body) is dict and (
            (path == repo_secret and set(body) == fields)
            or (path == org_secret and body.get("visibility") in ("all", "private", "selected")
                and set(body) == fields | {"visibility"} |
                ({"selected_repository_ids"} if body.get("visibility") == "selected" else set())))
        require((method == "GET" and path in allowed | {repo_secret, org_secret} | selected_pages
                 and body is None) or valid_put, "unapproved GitHub API operation")

    def request(self, method, path, body=None, *, absent=False):
        self.validate_request(method, path, body)
        context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
        # Explicit as well as default: authentication must never use an unchecked peer.
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        connection = http.client.HTTPSConnection("api.github.com", timeout=15, context=context)
        connection.set_debuglevel(0)
        deadline = time.monotonic() + 45
        try:
            payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
            require(payload is None or len(payload) <= MAX_REQUEST, "GitHub request size limit")
            connection.request(method, path, body=payload, headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
                "User-Agent": "secret-control",
                "Content-Type": "application/json",
            })
            socket = connection.sock
            response = connection.getresponse()
            expected = (200,) if method == "GET" else (201, 204)
            if absent and response.status == 404:
                return None
            require(response.status in expected, "GitHub request refused; response withheld")
            if method == "PUT":
                # Only status is useful: a successful secret update has no value body.
                return response.status
            content = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                require(remaining > 0, "GitHub request timed out")
                socket.settimeout(min(15, remaining))
                chunk = response.read1(min(16384, MAX_RESPONSE + 1 - len(content)))
                if not chunk:
                    break
                content.extend(chunk)
                require(len(content) <= MAX_RESPONSE, "GitHub response size limit")
            result = decode(bytes(content))
            require(type(result) is dict, "unexpected GitHub response")
            return result
        except ControlError:
            raise
        except Exception:
            raise ControlError("GitHub request failed; details withheld") from None
        finally:
            connection.close()

    def actor(self):
        return self.request("GET", "/user")

    def repository(self):
        return self.request("GET", self.repo)

    def organization(self):
        return self.request("GET", self.org)

    def organization_membership(self):
        return self.request("GET", self.membership)

    def prefix(self, item):
        return self.org if item["scope"] == "organization" else self.repo

    def public_key(self, item):
        return self.request("GET", self.secret_path(item) + "/public-key")

    def secret_path(self, item):
        return self.prefix(item) + "/actions/secrets"

    def secret(self, item):
        path = self.secret_path(item) + "/" + item["name"]
        value = metadata(self.request("GET", path, absent=True), item["name"], item["scope"])
        if value is not None and item["scope"] == "organization":
            ids = self.selected_repositories(path) if value["visibility"] == "selected" else []
            value["selected_repository_ids"] = selection(ids, value["visibility"])
        return value

    def selected_repositories(self, path):
        ids, count = [], None
        for page in range(1, 11):
            result = self.request("GET", path + "/repositories?per_page=100&page=" + str(page))
            total = result.get("total_count")
            require(type(total) is int and 0 <= total <= 1000
                    and (count is None or count == total), "GitHub selection changed")
            count = total
            repos = result.get("repositories")
            require(type(repos) is list and len(repos) <= 100
                    and all(type(repo) is dict and integer(repo.get("id")) for repo in repos),
                    "invalid GitHub selection")
            ids.extend(repo["id"] for repo in repos)
            require(len(repos) == 100 or len(ids) == total, "incomplete GitHub selection")
            if len(ids) == total:
                break
        require(len(ids) == count and len(set(ids)) == count, "incomplete GitHub selection")
        return sorted(ids)

    def write(self, item, encrypted, key_id):
        body = {"encrypted_value": encrypted, "key_id": key_id}
        if item["scope"] == "organization":
            body["visibility"] = item["expected"]["visibility"]
            if body["visibility"] == "selected":
                body["selected_repository_ids"] = item["expected"]["selected_repository_ids"]
        return self.request("PUT", self.secret_path(item) + "/" + item["name"], body)


def ownership(api, value):
    actor, repo, org = api.actor(), api.repository(), api.organization()
    require(integer(actor.get("id")) and (actor.get("login"), actor.get("id"))
            == (value["actor"]["login"], value["actor"]["id"]),
            "GitHub publishing identity mismatch")
    require(integer(org.get("id")) and (org.get("login"), org.get("id"))
            == (value["organization"]["login"], value["organization"]["id"]),
            "GitHub organization identity mismatch")
    require(integer(repo.get("id")) and integer((repo.get("owner") or {}).get("id"))
            and (repo.get("full_name"), repo.get("id"), (repo.get("owner") or {}).get("id"),
                 (repo.get("owner") or {}).get("login"))
            == (value["repository"]["full_name"], value["repository"]["id"],
                value["repository"]["owner_id"], value["organization"]["login"])
            and (repo.get("permissions") or {}).get("admin") is True,
            "GitHub repository ownership or permission mismatch")
    require(repo.get("archived") is False and repo.get("disabled") is False,
            "GitHub repository is unavailable")
    if any(item["scope"] == "organization" for item in value["secrets"]):
        member = api.organization_membership()
        require(member.get("state") == "active" and member.get("role") == "admin"
                and integer((member.get("organization") or {}).get("id"))
                and (member.get("organization") or {}).get("id") == org["id"]
                and integer((member.get("user") or {}).get("id"))
                and (member.get("user") or {}).get("id") == actor["id"],
                "GitHub organization administrator membership required")


def public_key(value):
    require(type(value) is dict and text(value.get("key_id"), r"[A-Za-z0-9_-]{1,128}", 128),
            "invalid GitHub public key identifier")
    try:
        key = base64.b64decode(value["key"], validate=True)
    except (KeyError, ValueError, TypeError):
        raise ControlError("invalid GitHub public key") from None
    require(len(key) == 32, "invalid GitHub public key length")
    return value["key_id"], key


@contextmanager
def journal_lock(directory):
    root = Path(directory)
    _, parent = private_parent(root / ".publish-lock")
    fd = None
    try:
        fd = os.open(".publish-lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_uid == os.geteuid() and info.st_nlink == 1,
                "unsafe GitHub publication lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ControlError("another GitHub publication holds the journal") from None
        yield root
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def record(root, operation, suffix, value):
    new_private(root / (operation + "." + suffix + ".json"),
                json.dumps(value, sort_keys=True).encode())


def publish(manifest_file, expected_sha256, token_file, *, apply=False,
            journal=None, api_factory=API, encrypt=seal, catalog_file=None,
            identity=None, age="age"):
    """Preflight every destination before writes; opaque values cannot be read back."""
    require(text(expected_sha256, r"[0-9a-f]{64}", 64), "approved manifest digest required")
    raw = read_private(manifest_file, MAX_CATALOG)
    require(hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256),
            "approved GitHub manifest digest mismatch")
    value = manifest(raw)
    if value["schema_version"] == 2:
        from .github_catalog import publish_catalog

        return publish_catalog(value, expected_sha256, token_file, catalog_file=catalog_file,
                               identity=identity, age=age, apply=apply, journal=journal,
                               encrypt=encrypt)
    require(catalog_file is None and identity is None,
            "file-source manifest does not accept catalog arguments")
    payloads = []
    for item in value["secrets"]:
        payload = read_private(item["source"], MAX_VALUE)
        require(bool(payload) and b"\x00" not in payload, "invalid GitHub secret payload")
        require(item["name"] != "BOT_PAT" or (b"\r" not in payload and b"\n" not in payload),
                "BOT_PAT must be a single line; normalize its source explicitly")
        require(hmac.compare_digest(hashlib.sha256(payload).hexdigest(), item["source_sha256"]),
                "approved GitHub source digest mismatch")
        payloads.append(payload)
    def prepare():
        api = api_factory(token_file, value)
        return [(api, item, lambda: ownership(api, value)) for item in value["secrets"]]

    return deliver(value, payloads, prepare, expected_sha256, apply=apply,
                   journal=journal, encrypt=encrypt,
                   context={"repository_id": value["repository"]["id"]})


def deliver(value, payloads, prepare, expected_sha256, *, apply, journal, encrypt, context):
    """One transaction protocol for both legacy file and catalog delivery."""
    require(not apply or journal is not None, "private GitHub publication journal required")
    with journal_lock(journal) if apply else nullcontext(None) as root:
        operation = value["operation_id"]
        if apply:
            require(not os.path.lexists(root / (operation + ".intent.json")),
                    "GitHub operation already attempted; reconcile before retry")
        operations = prepare()
        require(len(operations) == len(payloads), "GitHub delivery count mismatch")
        public_keys = []
        for api, item, verify_owner in operations:
            verify_owner()
            public_keys.append(public_key(api.public_key(item)))
            require(api.secret(item) == item["expected"],
                    "GitHub secret metadata drift or unapproved collision")
        # Bind exact approved bytes in memory before the first network mutation.
        encrypted = [encrypt(payload, key) for payload, (_, key) in zip(payloads, public_keys)]
        if not apply:
            return {"status": "planned", "secrets": len(payloads), "writes": 0,
                    "values": "redacted", "consumer_verified": False}
        record(root, operation, "intent", {"status": "pending", "manifest_sha256": expected_sha256,
               **context, "secrets": [item["name"] for _, item, _ in operations]})
        accepted, observed = 0, 0
        phase = "revalidation"
        try:
            for index, (api, item, verify_owner) in enumerate(operations):
                phase = "revalidation"
                verify_owner()
                key_id, key = public_keys[index]
                require(public_key(api.public_key(item)) == (key_id, key),
                        "GitHub encryption key changed during preflight")
                require(api.secret(item) == item["expected"],
                        "GitHub secret changed during preflight")
                phase = "write"
                status = api.write(item, encrypted[index], key_id)
                require(status == 204,
                        "GitHub secret existence changed during write")
                accepted += 1
                phase = "receipt"
                record(root, operation, str(index) + ".accepted",
                       {"status": "accepted", "secret": item["name"], "http_status": status})
                phase = "observation"
                after = api.secret(item)
                require(after is not None, "GitHub accepted secret metadata is absent")
                require(after["created_at"] == item["expected"]["created_at"]
                        and timestamp(after["updated_at"]) >= timestamp(item["expected"]["updated_at"]),
                        "GitHub accepted secret metadata mismatch")
                if item["scope"] == "organization":
                    require(all(after[field] == item["expected"][field]
                                for field in ("visibility", "selected_repository_ids")),
                            "GitHub accepted secret policy changed")
                record(root, operation, str(index) + ".observed",
                       {"status": "metadata_observed", "secret": item["name"], **after})
                observed += 1
            result = {"status": "accepted", "secrets": accepted,
                      "metadata_observed": observed, "consumer_verified": False,
                      "values": "redacted"}
            record(root, operation, "complete", result)
            return result
        except Exception:
            record(root, operation, "stopped", {"status": "stopped", "phase": phase,
                   "accepted": accepted, "metadata_observed": observed,
                   "write_outcome_may_be_unknown": phase == "write"})
            raise ControlError("GitHub publication stopped; reconcile private journal before retry") from None
