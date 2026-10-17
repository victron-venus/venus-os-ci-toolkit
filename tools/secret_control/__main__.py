"""Explicit commands; help and local validation never load OCI or contact a service."""

import argparse
import json
from pathlib import Path
import re
import shutil
import sys
from . import catalog
from .catalog import ControlError, require
from .private_io import decrypt, new_private, process, trusted_tool
from .materialize import materialize_bytes, MaterializationError


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes invalid user arguments; those may contain a secret.
        raise ControlError("invalid command arguments; use --help")


def parser():
    p = Parser(prog="python3 -m tools.secret_control", allow_abbrev=False)
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    descriptions = {
        "encrypt": "Validate a catalog from stdin and encrypt it to a new private file.",
        "validate": "Decrypt and validate a catalog locally; print counts only.",
        "plan": "Summarize catalog destinations locally without contacting a service.",
        "oci-publish": "Compare OCI destinations; publish only with --apply and --generation.",
        "github-publish": "Preflight explicitly approved GitHub secrets; write only with --apply.",
        "hcp-prepare": "Copy the reviewed HCP Terraform template into a new private directory.",
        "hcp-plan": "Plan HCP changes using the isolated Terraform root.",
        "hcp-apply": "Apply an explicitly selected catalog generation to HCP.",
        "fetch": "Fetch an explicit OCI secret version and verify its independent digest.",
        "drill": "Exercise offline recovery using only disposable synthetic keys and values.",
        "recovery-check": "Verify a catalog against an independently approved ciphertext digest and generation.",
    }
    for name, description in descriptions.items():
        cmd = sub.add_parser(
            name, allow_abbrev=False, help=description, description=description
        )
        if name == "encrypt":
            cmd.add_argument("--output", required=True)
            cmd.add_argument("--recipient", action="append", required=True)
            cmd.add_argument("--previous")
            cmd.add_argument("--identity")
        if name in (
            "validate", "plan", "oci-publish", "hcp-plan", "hcp-apply", "recovery-check"
        ):
            cmd.add_argument("--catalog", required=True)
            cmd.add_argument("--identity", required=True)
        if name not in ("fetch", "hcp-prepare", "github-publish"):
            cmd.add_argument("--age", default="age")
        if name == "github-publish":
            for flag in ("manifest", "manifest-sha256", "token-file"):
                cmd.add_argument("--" + flag, required=True)
            cmd.add_argument("--journal-dir")
            cmd.add_argument("--apply", action="store_true")
        if name == "drill":
            cmd.add_argument("--age-keygen", default="age-keygen")
        if name == "recovery-check":
            cmd.add_argument("--expected-sha256", required=True)
            cmd.add_argument("--generation", type=int, required=True)
        if name.startswith("hcp-"):
            cmd.add_argument("--terraform-dir", required=True)
        if name in ("hcp-plan", "hcp-apply"):
            cmd.add_argument("--terraform", default="terraform")
        if name in ("hcp-apply", "oci-publish"):
            cmd.add_argument("--generation", type=int, required=name == "hcp-apply")
        if name == "oci-publish":
            cmd.add_argument("--apply", action="store_true")
        if name in ("oci-publish", "fetch"):
            cmd.add_argument(
                "--auth", choices=("api-key", "instance-principal"), required=True
            )
            cmd.add_argument("--config")
            cmd.add_argument("--profile", default="DEFAULT")
        if name == "fetch":
            for flag in ("region", "secret-id", "expected-sha256", "output"):
                cmd.add_argument("--" + flag, required=True)
            cmd.add_argument("--version-number", type=int, required=True)
    return p


def tool(name):
    result = shutil.which(name) if "/" not in name else name
    require(result is not None, "required external tool unavailable")
    return trusted_tool(result)


def execute(args):
    require(sys.version_info >= (3, 12), "Python 3.12 or newer required")
    if args.command == "github-publish":
        from .github_api import publish

        return publish(args.manifest, args.manifest_sha256, args.token_file,
                       apply=args.apply, journal=args.journal_dir)
    if args.command == "hcp-prepare":
        from .hcp import prepare

        return prepare(args.terraform_dir)
    if args.command == "drill":
        from .drill import run_drill

        return run_drill(tool(args.age), tool(args.age_keygen))
    if args.command == "recovery-check":
        from .recovery import check

        # Do not resolve the tool or open an identity until the ciphertext matches.
        return check(
            args.catalog,
            args.identity,
            args.age,
            expected_sha256=args.expected_sha256,
            generation=args.generation,
        )
    if args.command == "fetch":
        # Reject malformed requests before any credential or network access.
        require(
            args.version_number > 0
            and re.fullmatch("[0-9a-f]{64}", args.expected_sha256) is not None,
            "explicit version and independent SHA-256 required",
        )
        require(
            catalog.text(args.secret_id, r"ocid1\.vaultsecret\.[A-Za-z0-9._-]+"),
            "invalid secret identifier",
        )
        from .oci_api import SDK

        api = SDK(args.region, args.auth, args.config, args.profile)
        value = api.payload(args.secret_id, args.version_number)
        return materialize_bytes(value, Path(args.output), args.expected_sha256)
    age = tool(args.age)
    if args.command == "encrypt":
        require(
            bool(args.previous) == bool(args.identity),
            "previous catalog and identity must be supplied together",
        )
        raw = sys.stdin.buffer.read(catalog.MAX_CATALOG + 1)
        value = catalog.decode(raw)
        previous = (
            catalog.decode(decrypt(args.previous, args.identity, age))
            if args.previous
            else None
        )
        catalog.lineage(value, previous)
        for recipient in args.recipient:
            require(
                catalog.text(recipient, r"age1[0-9a-z]{58}", 62),
                "use a native age recipient",
            )
        argv = [age, "--encrypt"]
        for recipient in args.recipient:
            argv.extend(["--recipient", recipient])
        encrypted = process(argv, raw)
        require(
            encrypted.startswith(b"age-encryption.org/v1\n"), "unexpected age output"
        )
        new_private(args.output, encrypted)
        return {
            "status": "encrypted",
            "generation": value["generation"],
            "entries": len(value["entries"]),
        }
    value = catalog.decode(decrypt(args.catalog, args.identity, age))
    if args.command in ("validate", "plan"):
        result = catalog.plan(value)
        result["status"] = "valid" if args.command == "validate" else "planned"
        return result
    if args.command == "oci-publish":
        require(
            not args.apply or args.generation == value["generation"],
            "explicit matching generation required",
        )
        from .oci_api import SDK, publish

        clients = {}

        def get(region):
            if region not in clients:
                clients[region] = SDK(region, args.auth, args.config, args.profile)
            return clients[region]

        return publish(value, get, apply=args.apply, generation=args.generation)
    from .hcp import run

    return run(
        value,
        args.terraform_dir,
        tool(args.terraform),
        apply=args.command == "hcp-apply",
        generation=getattr(args, "generation", None),
    )


def main(argv=None):
    try:
        result = execute(parser().parse_args(argv))
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ControlError, MaterializationError) as error:
        print(json.dumps({"status": "refused", "reason": str(error)}), file=sys.stderr)
        return 2
    except Exception:
        # SDK, parser, process and filesystem exceptions may contain private values.
        print(
            json.dumps(
                {"status": "refused", "reason": "operation failed; details withheld"}
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
