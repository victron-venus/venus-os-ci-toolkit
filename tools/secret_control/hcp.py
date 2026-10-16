"""Execute only the bundled HCP Terraform root in a private local working copy."""

import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import unicodedata
from .catalog import require, _pairs
from .private_io import (
    private_parent,
    new_private,
    read_private,
    trusted_tool,
    clean_env,
    process,
)


def module_root():
    return Path(__file__).resolve().parents[2] / "deploy" / "secret-bootstrap" / "hcp"


def prepare(destination):
    path, parent = private_parent(destination)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent)
    finally:
        os.close(parent)
    for source in sorted(module_root().glob("*.tf")):
        new_private(path / source.name, source.read_bytes())
    lock = module_root() / ".terraform.lock.hcl"
    if lock.is_file():
        new_private(path / lock.name, lock.read_bytes())
    return {
        "status": "prepared",
        "initialization": "run terraform init explicitly in this private HCP root",
    }


def mappings(catalog):
    metadata, values = {}, {}
    for entry in catalog["entries"]:
        for target in entry["targets"]:
            if target["type"] != "hcp":
                continue
            require(
                unicodedata.normalize("NFC", entry["value"]) == entry["value"],
                "HCP values must already be NFC-normalized; Terraform would change their bytes",
            )
            require(
                target["category"] != "env"
                or not any(char in entry["value"] for char in "\r\n"),
                "HCP environment variable values cannot contain CR or LF",
            )
            digest = hashlib.sha256(
                json.dumps(target, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            name = entry["id"][:40] + "_" + digest[:16]
            require(name not in metadata, "duplicate HCP resource identity")
            metadata[name] = {k: v for k, v in target.items() if k != "type"}
            metadata[name]["revision"] = entry["revision"]
            values[name] = entry["value"]
    return metadata, values


def internal_file(root, relative, limit=4 * 1024 * 1024):
    """Read Terraform metadata inside the private root, rejecting link traversal."""
    parts = Path(relative).parts
    require(
        parts and all(x not in ("..", "/", ".") for x in parts), "invalid internal path"
    )
    _, fd = private_parent(root / ".sentinel")
    try:
        for part in parts[:-1]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            require(
                info.st_uid == os.geteuid() and not info.st_mode & 0o022,
                "unsafe internal directory",
            )
        child = os.open(
            parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd
        )
        os.close(fd)
        fd = child
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == os.geteuid()
            and before.st_nlink == 1
            and not before.st_mode & 0o022
            and before.st_size <= limit,
            "unsafe internal file",
        )
        data = bytearray()
        while len(data) <= limit:
            part = os.read(fd, min(65536, limit + 1 - len(data)))
            if not part:
                break
            data.extend(part)
        after = os.fstat(fd)
        require(
            len(data) == before.st_size
            and before.st_mtime_ns == after.st_mtime_ns
            and before.st_ctime_ns == after.st_ctime_ns,
            "internal file changed",
        )
        return bytes(data)
    finally:
        os.close(fd)


def verify_state(root, metadata):
    backend = json.loads(
        internal_file(root, ".terraform/terraform.tfstate"), object_pairs_hook=_pairs
    )
    info = backend.get("backend", {})
    require(info.get("type") == "local", "HCP execution requires local backend")
    config = info.get("config", {})
    require(
        config.get("path") in (None, "", "terraform.tfstate")
        and config.get("workspace_dir") in (None, ""),
        "custom state location refused",
    )
    environment = root / ".terraform/environment"
    if os.path.lexists(environment):
        require(
            internal_file(root, ".terraform/environment").strip() == b"default",
            "custom Terraform workspace refused",
        )
    if os.path.lexists(root / "terraform.tfstate"):
        state = json.loads(
            internal_file(root, "terraform.tfstate"), object_pairs_hook=_pairs
        )
        require(
            state.get("version") == 4 and type(state.get("resources", [])) is list,
            "unsupported Terraform state shape",
        )
        for resource in state.get("resources", []):
            require(
                resource.get("mode") == "managed"
                and resource.get("type") == "tfe_variable"
                and resource.get("name") == "secret"
                and not resource.get("module"),
                "unrelated Terraform resource refused",
            )
            for instance in resource.get("instances", []):
                require(
                    not instance.get("deposed") and instance.get("status") is None,
                    "deposed or tainted Terraform variable refused",
                )
                name = instance.get("index_key")
                require(name in metadata, "unlisted Terraform variable refused")
                attributes = instance.get("attributes", {})
                require(
                    not attributes.get("value")
                    and not attributes.get("readable_value"),
                    "stateful secret value refused",
                )
                require(
                    all(
                        attributes.get(k) == metadata[name].get(k)
                        for k in ("key", "category", "workspace_id", "variable_set_id")
                    ),
                    "Terraform variable destination mismatch",
                )
                require(
                    attributes.get("value_wo_version", 0) <= metadata[name]["revision"],
                    "Terraform variable revision would decrease",
                )


def verify_root(directory):
    root = Path(directory).absolute()
    _, fd = private_parent(root / ".secret-control.lock")
    handle = None
    try:
        # Serialize before reading any mutable execution state/configuration.
        handle = os.open(
            ".secret-control.lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
            dir_fd=fd,
        )
        info = os.fstat(handle)
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600,
            "unsafe execution lock",
        )
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        expected = {p.name: p.read_bytes() for p in module_root().glob("*.tf")}
        actual = {
            p.name
            for p in root.iterdir()
            if p.name.endswith((".tf", ".tf.json", ".tfvars", ".tfvars.json"))
        }
        require(
            actual == set(expected), "Terraform root contains unexpected configuration"
        )
        for name, data in expected.items():
            require(read_private(root / name) == data, "Terraform module copy differs")
        bundled = module_root() / ".terraform.lock.hcl"
        require(bundled.is_file(), "bundled Terraform lock missing")
        require(
            internal_file(root, ".terraform.lock.hcl") == bundled.read_bytes(),
            "Terraform provider lock differs",
        )
        return root, handle
    except BaseException:
        if handle is not None:
            os.close(handle)
        raise
    finally:
        os.close(fd)


def record_intent(root, catalog, metadata, values, apply):
    """An append-only private local revision ledger also survives partial applies."""
    key_path = root / ".secret-control-ledger-key"
    if not os.path.lexists(key_path):
        require(
            not list(root.glob(".secret-control-intent-*.json")),
            "HCP ledger key missing",
        )
        if apply:
            new_private(key_path, os.urandom(32))
    key = read_private(key_path, 32) if os.path.lexists(key_path) else None
    if key is None:
        require(
            not os.path.lexists(root / "terraform.tfstate"),
            "existing HCP state has no ownership ledger",
        )
        return
    require(len(key) == 32, "invalid HCP ledger key")
    document = {
        "catalog_id": catalog["catalog_id"],
        "generation": catalog["generation"],
        "entries": {
            name: {
                "metadata": item,
                "value_hmac": hmac.new(
                    key, values[name].encode(), hashlib.sha256
                ).hexdigest(),
            }
            for name, item in metadata.items()
        },
    }
    histories = sorted(root.glob(".secret-control-intent-*.json"))
    require(
        histories or not os.path.lexists(root / "terraform.tfstate"),
        "existing HCP state has no ownership ledger",
    )
    for path in histories:
        prior = json.loads(read_private(path), object_pairs_hook=_pairs)
        require(
            prior["catalog_id"] == document["catalog_id"]
            and prior["generation"] <= document["generation"],
            "HCP catalog identity or generation conflicts",
        )
        require(
            prior["entries"].keys() <= document["entries"].keys(),
            "HCP destination deletion is not supported",
        )
        for name, old in prior["entries"].items():
            new = document["entries"][name]
            a, b = old["metadata"]["revision"], new["metadata"]["revision"]
            require(b >= a, "HCP revision would decrease")
            require(b != a or old == new, "same HCP revision has different input")
        if prior["generation"] == document["generation"]:
            require(prior == document, "same HCP generation has different input")
    target = root / (".secret-control-intent-" + str(catalog["generation"]) + ".json")
    if apply and not os.path.lexists(target):
        new_private(target, json.dumps(document, sort_keys=True).encode())


def run(catalog, directory, terraform, *, apply=False, generation=None):
    require(
        not apply or generation == catalog["generation"],
        "explicit matching generation required",
    )
    metadata, values = mappings(catalog)
    require(bool(metadata), "catalog has no HCP targets")
    root, lock = verify_root(directory)
    try:
        binary = trusted_tool(terraform)
        env = clean_env()
        if "TFE_TOKEN" in os.environ:
            env["TFE_TOKEN"] = os.environ["TFE_TOKEN"]
        env["TF_VAR_hcp_variables"] = json.dumps(metadata, separators=(",", ":"))
        env["TF_VAR_hcp_secret_values_json"] = json.dumps(values, separators=(",", ":"))
        require(
            len(env["TF_VAR_hcp_secret_values_json"].encode()) < 120000
            and len(env["TF_VAR_hcp_variables"].encode()) < 120000,
            "HCP environment payload limit",
        )
        env["TF_CLI_CONFIG_FILE"] = "/dev/null"
        env["TF_WORKSPACE"] = "default"
        verify_state(root, metadata)
        record_intent(root, catalog, metadata, values, apply)
        argv = [
            binary,
            "apply" if apply else "plan",
            "-input=false",
            "-lock=true",
            "-no-color",
        ]
        process_options = {}
        if apply:
            argv.append("-auto-approve")
        else:
            argv.append("-detailed-exitcode")
            process_options = {"accepted_exit_codes": (0, 2), "return_exit_code": True}
        # Never expose Terraform output; even an unexpected provider diagnostic is withheld.
        status = process(
            argv, env=env, cwd=root, timeout=900, limit=8 * 1024 * 1024,
            **process_options,
        )
        result = {
            "status": "applied" if apply else "planned",
            "variables": len(metadata),
            "output": "withheld",
        }
        if not apply:
            result["changes"] = status == 2
        return result
    finally:
        os.close(lock)
