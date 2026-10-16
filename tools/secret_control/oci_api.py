"""Narrow OCI adapter. SDK import and authentication occur only on explicit use."""

import base64
import configparser
import hmac
import importlib
import logging
import json
import time
from pathlib import Path
from .catalog import ControlError, MAX_VALUE, require, integer, text
from .private_io import read_private

SDK_VERSION = "2.187.2"
TAG = "secret-control-"


def validate_freeform_tags(tags):
    """OCI Tagging limits; SDK model constructors do not enforce these rules.

    https://docs.oracle.com/en-us/iaas/Content/Tagging/Concepts/taggingoverview.htm
    """
    require(type(tags) is dict and len(tags) <= 10, "invalid OCI freeform tag count")
    for key, value in tags.items():
        require(
            text(key, r"[!-~]{1,100}", 100) and "." not in key,
            "invalid OCI freeform tag key",
        )
        require(
            type(value) is str and len(value) <= 256, "invalid OCI freeform tag value"
        )
    try:
        size = len(
            json.dumps(tags, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
    except UnicodeError:
        raise ControlError("invalid OCI freeform tag encoding") from None
    require(size <= 5120, "OCI freeform tag data limit")


def safe_service_diagnostic(error):
    """Allow only a numeric HTTP status and fixed public OCI error identifiers."""
    status = getattr(error, "status", None)
    code = getattr(error, "code", None)
    fields = []
    if type(status) is int and 100 <= status <= 599:
        fields.append("HTTP " + str(status))
    if type(code) is str and code in {
        "NotAuthorizedOrNotFound",
        "NotAuthenticated",
        "InvalidParameter",
        "InvalidRequest",
        "IncorrectState",
        "Conflict",
        "PreconditionFailed",
        "LimitExceeded",
        "TooManyRequests",
        "InternalError",
        "InternalServerError",
        "ServiceUnavailable",
    }:
        fields.append(code)
    return "; " + "/".join(fields) if fields else ""


def unpack(bundle, secret_id, version):
    require(
        bundle.secret_id == secret_id and bundle.version_number == version,
        "unexpected secret version",
    )
    body = bundle.secret_bundle_content
    require(
        body.content_type == "BASE64"
        and type(body.content) is str
        and len(body.content) <= 24000,
        "invalid secret bundle",
    )
    try:
        data = base64.b64decode(body.content, validate=True)
    except (ValueError, TypeError):
        raise ControlError("invalid secret encoding") from None
    require(len(data) <= MAX_VALUE, "secret bundle size limit")
    return data


class SDK:
    def __init__(self, region, auth, config=None, profile="DEFAULT"):
        # Do not let SDK/HTTP debug loggers publish signed requests or payloads.
        logging.disable(logging.CRITICAL)
        try:
            self.oci = importlib.import_module("oci")
        except ImportError:
            raise ControlError("install the pinned optional OCI dependency") from None
        require(self.oci.__version__ == SDK_VERSION, "OCI SDK version differs from pin")
        require(text(region, r"[a-z][a-z0-9-]+", 64), "invalid region")
        cfg = {"region": region}
        if auth == "instance-principal":
            require(config is None, "config is not used with instance principal")
            signer = self.oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        else:
            require(
                auth == "api-key" and config is not None,
                "explicit OCI authentication required",
            )
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string(read_private(config, 65536).decode("utf-8"))
            require(profile in parser, "OCI config profile absent")
            item = parser[profile]
            key = Path(item["key_file"]).expanduser()
            require(key.is_absolute(), "OCI key path must be absolute")
            key_content = read_private(key, 65536).decode("utf-8")
            # A basic API-key Signer does not bypass the SDK's config validation.
            # Supply only its required fields; key_content is the supported
            # in-memory fallback for key_file, avoiding an unguarded second read.
            cfg.update(
                {name: item[name] for name in ("user", "tenancy", "fingerprint")}
            )
            cfg["key_content"] = key_content
            self.oci.config.validate_config(cfg)
            signer = self.oci.signer.Signer(
                cfg["tenancy"],
                cfg["user"],
                cfg["fingerprint"],
                private_key_file_location=None,
                private_key_content=key_content,
                pass_phrase=item.get("pass_phrase"),
            )
        retry = self.oci.retry.NoneRetryStrategy()
        self.vault = self.oci.vault.VaultsClient(
            cfg, signer=signer, retry_strategy=retry, timeout=(10, 60)
        )
        self.secrets = self.oci.secrets.SecretsClient(
            cfg, signer=signer, retry_strategy=retry, timeout=(10, 60)
        )

    def find(self, target):
        found, page, seen = [], None, set()
        while True:
            response = self.vault.list_secrets(
                target["compartment_id"],
                vault_id=target["vault_id"],
                name=target["name"],
                page=page,
                limit=100,
            )
            found.extend(x.id for x in response.data if x.secret_name == target["name"])
            require(len(found) <= 1, "ambiguous secret name")
            page = response.headers.get("opc-next-page")
            if not page:
                return found[0] if found else None
            require(page not in seen and len(seen) < 100, "invalid secret pagination")
            seen.add(page)

    def snapshot(self, secret_id):
        response = self.vault.get_secret(secret_id)
        require(bool(response.headers.get("etag")), "missing OCI concurrency token")
        return response.data, response.headers["etag"]

    def payload(self, secret_id, version):
        return unpack(
            self.secrets.get_secret_bundle(secret_id, version_number=version).data,
            secret_id,
            version,
        )

    def advance(self, secret_id, tags, etag):
        validate_freeform_tags(tags)
        details = self.oci.vault.models.UpdateSecretDetails(freeform_tags=tags)
        self.vault.update_secret(secret_id, details, if_match=etag)
        return secret_id

    def write(self, target, value, tags, secret_id=None, etag=None):
        validate_freeform_tags(tags)
        models = self.oci.vault.models
        content = models.Base64SecretContentDetails(
            content=base64.b64encode(value).decode("ascii"), stage="CURRENT"
        )
        if secret_id is None:
            details = models.CreateSecretDetails(
                compartment_id=target["compartment_id"],
                vault_id=target["vault_id"],
                key_id=target["key_id"],
                secret_name=target["name"],
                secret_content=content,
                freeform_tags=tags,
            )
            return self.vault.create_secret(details).data.id
        details = models.UpdateSecretDetails(secret_content=content, freeform_tags=tags)
        self.vault.update_secret(secret_id, details, if_match=etag)
        return secret_id


def ownership(secret, target, catalog, entry):
    require(
        secret.lifecycle_state == "ACTIVE",
        "secret is not active; reconcile before retry",
    )
    require(
        (secret.compartment_id, secret.vault_id, secret.key_id, secret.secret_name)
        == tuple(target[x] for x in ("compartment_id", "vault_id", "key_id", "name")),
        "secret destination mismatch",
    )
    tags = secret.freeform_tags or {}
    require(
        all(
            tags.get(TAG + k) == v
            for k, v in {
                "managed": "1",
                "catalog": catalog["catalog_id"],
                "entry": entry["id"],
            }.items()
        ),
        "unmanaged or foreign secret collision",
    )
    raw = tags.get(TAG + "revision", "")
    require(
        type(raw) is str
        and raw.isdecimal()
        and str(int(raw)) == raw
        and integer(int(raw)),
        "invalid remote revision",
    )
    require(int(raw) <= entry["revision"], "remote revision is newer")
    require(integer(secret.current_version_number), "invalid current secret version")
    return int(raw), tags


def publish(catalog, clients, *, apply=False, generation=None):
    """Preflight all targets, then one CAS mutation at a time; never retry a write.

    OCI is not a multi-secret transaction. A failed/uncertain write stops the batch;
    already verified writes remain. The next invocation reconciles from fresh state.
    """
    require(
        not apply or generation == catalog["generation"],
        "explicit matching generation required",
    )
    operations = []
    for entry in catalog["entries"]:
        for target in entry["targets"]:
            if target["type"] != "oci":
                continue
            api = clients(target["region"])
            sid = api.find(target)
            etag, tags, action = None, {}, "create"
            if sid:
                secret, etag = api.snapshot(sid)
                revision, tags = ownership(secret, target, catalog, entry)
                current = api.payload(sid, secret.current_version_number)
                require(api.snapshot(sid)[1] == etag, "secret changed during preflight")
                same = hmac.compare_digest(current, entry["value"].encode("utf-8"))
                require(
                    revision != entry["revision"] or same,
                    "same revision has different payload",
                )
                action = (
                    ("noop" if revision == entry["revision"] else "advance")
                    if same
                    else "update"
                )
            next_tags = dict(tags)
            next_tags.update(
                {
                    TAG + "managed": "1",
                    TAG + "catalog": catalog["catalog_id"],
                    TAG + "entry": entry["id"],
                    TAG + "revision": str(entry["revision"]),
                }
            )
            validate_freeform_tags(next_tags)
            operations.append((api, target, entry, sid, etag, next_tags, action))
    result = {
        "mode": "apply" if apply else "plan",
        "create": 0,
        "update": 0,
        "advance": 0,
        "noop": 0,
        "verified": 0,
    }
    for api, target, entry, sid, etag, tags, action in operations:
        result[action] += 1
        if not apply or action == "noop":
            continue
        try:
            if action == "advance":
                sid = api.advance(sid, tags, etag)
            else:
                sid = api.write(target, entry["value"].encode("utf-8"), tags, sid, etag)
            # Eventual completion may lag the accepted mutation. Retry reads only.
            # Any unexpected state/API failure stops; no write is repeated.
            for attempt in range(13):
                secret, returned_etag = api.snapshot(sid)
                if secret.lifecycle_state == "ACTIVE":
                    revision, _ = ownership(secret, target, catalog, entry)
                    same = hmac.compare_digest(
                        api.payload(sid, secret.current_version_number),
                        entry["value"].encode("utf-8"),
                    )
                    if revision == entry["revision"] and same:
                        require(
                            api.snapshot(sid)[1] == returned_etag,
                            "secret changed during readback",
                        )
                        break
                else:
                    require(
                        secret.lifecycle_state in ("CREATING", "UPDATING"),
                        "unexpected write state",
                    )
                require(attempt < 12, "write not yet verified")
                time.sleep(5)
            result["verified"] += 1
        except Exception as error:
            raise ControlError(
                "OCI batch stopped; verified writes: "
                + str(result["verified"])
                + safe_service_diagnostic(error)
                + "; outcome may be partial; reconcile before retry"
            ) from None
    return result
