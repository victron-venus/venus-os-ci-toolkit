"""Keep the standalone Terraform roots out of the secret-persistence path.

These static fences run without Terraform or credentials in the normal suite.
The adjacent mocked Terraform tests exercise accepted and rejected inputs.
"""

import json
import hashlib
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "deploy" / "secret-bootstrap"


def source(stage):
    return "\n".join(path.read_text() for path in sorted((ROOT / stage).glob("*.tf")))


def block(text, kind, name):
    """Extract a top-level block up to the next top-level declaration."""
    match = re.search(
        rf'(?ms)^{kind} "{re.escape(name)}"[^\n]*\{{\n(.*?)^\}}', text
    )
    if match is None:
        raise AssertionError(f"missing {kind} {name}")
    return match.group(1)


class SecretBootstrapSafetyTests(unittest.TestCase):
    def test_roots_have_separate_local_state_and_provider_pins(self):
        for stage, provider, version in (
            ("hcp", "hashicorp/tfe", "0.76.0"),
            ("oci", "oracle/oci", "9.7.1"),
        ):
            with self.subTest(stage=stage):
                text = source(stage)
                self.assertRegex(text, r'backend "local" \{\}')
                self.assertIn('required_version = ">= 1.11.0, < 2.0.0"', text)
                self.assertIn(f'source  = "{provider}"', text)
                self.assertIn(f'version = "= {version}"', text)
                lock = (ROOT / stage / ".terraform.lock.hcl").read_text()
                self.assertEqual(
                    re.findall(r'^provider "([^"]+)"', lock, re.M),
                    [f"registry.terraform.io/{provider}"],
                )
                self.assertRegex(lock, rf'version\s+= "{re.escape(version)}"')
                self.assertIn('"zh:', lock)
                ignores = (ROOT / stage / ".gitignore").read_text().splitlines()
                for pattern in (".terraform/", "*.tfstate*", "*.tfplan", "*.tfvars.json"):
                    self.assertIn(pattern, ignores)

    def test_only_reviewed_resource_types_and_no_execution_or_secret_reads(self):
        expected = {
            "hcp": {"tfe_variable"},
            "oci": {"oci_identity_compartment", "oci_kms_vault", "oci_kms_key", "oci_identity_policy"},
        }
        for stage, resources in expected.items():
            with self.subTest(stage=stage):
                text = source(stage)
                self.assertEqual(set(re.findall(r'^resource "([^"]+)"', text, re.M)), resources)
                self.assertNotRegex(text, r'(?m)^\s*(data|module|provisioner|connection)\s+"')
                self.assertNotRegex(text, r'\b(file|filebase64|templatefile|pathexpand)\s*\(')
                for forbidden in ("secret_content", "secretbundle", "local-exec", "remote-exec"):
                    self.assertNotIn(forbidden, text)

    def test_hcp_values_are_ephemeral_write_only_and_not_exported(self):
        text = source("hcp")
        values = block(text, "variable", "hcp_secret_values_json")
        self.assertRegex(values, r'type\s*=\s*string')
        self.assertRegex(values, r'sensitive\s*=\s*true')
        self.assertRegex(values, r'ephemeral\s*=\s*true')
        self.assertIn("length(keys(jsondecode(var.hcp_secret_values_json))) == length(var.hcp_variables)", values)
        self.assertIn("contains(keys(var.hcp_variables), id)", values)
        self.assertIn("value != null && value == tostring(value)", values)
        resource = block(text, "resource", "tfe_variable")
        self.assertRegex(resource, r'for_each\s*=\s*var.hcp_variables')
        self.assertRegex(resource, r'value_wo\s*=\s*lookup\(local.hcp_secret_values, each.key, null\)')
        self.assertRegex(resource, r'value_wo_version\s*=\s*each.value.revision')
        self.assertRegex(resource, r'sensitive\s*=\s*true')
        self.assertRegex(resource, r'hcl\s*=\s*false')
        self.assertNotRegex(resource, r'(?m)^\s*value\s*=')
        self.assertNotIn("readable_value", text)
        output = block(text, "output", "hcp_variable_ids")
        self.assertNotIn("hcp_secret_values", output)
        self.assertIn("variable.id", output)
        self.assertNotIn("tfe_variable_set", text)

    def test_oci_data_destruction_protection_and_exact_read_grants(self):
        text = source("oci")
        for resource_type in ("oci_identity_compartment", "oci_kms_vault", "oci_kms_key"):
            self.assertRegex(block(text, "resource", resource_type), r'prevent_destroy\s*=\s*true')
        self.assertRegex(text, r'enable_delete\s*=\s*false')
        self.assertRegex(text, r'vault_type\s*=\s*"DEFAULT"')
        self.assertRegex(text, r'protection_mode\s*=\s*"SOFTWARE"')
        self.assertRegex(text, r'algorithm\s*=\s*"AES"')
        self.assertRegex(text, r'length\s*=\s*32')
        self.assertRegex(text, r'config_file_profile\s*=\s*var.oci_config_file_profile')
        self.assertNotIn('"DEFAULT"', block(text, "variable", "oci_config_file_profile"))
        policy = block(text, "resource", "oci_identity_policy")
        self.assertIn("to read secret-bundles in compartment id", policy)
        self.assertIn("target.vault.id", policy)
        self.assertIn("target.secret.id", policy)
        self.assertNotRegex(policy, r'\b(manage|use|any-user|tenancy)\b')
        self.assertNotIn("*", policy)
        self.assertRegex(block(text, "variable", "secret_readers"), r'default\s*=\s*\{\}')

    def test_mock_tests_cannot_silently_contact_real_providers(self):
        for stage, provider in (("hcp", "tfe"), ("oci", "oci")):
            text = (ROOT / stage / "tests" / "contract.tftest.hcl").read_text()
            self.assertIn(f'mock_provider "{provider}"', text)
            self.assertNotRegex(text, r'(?m)^provider\s+"')
            self.assertNotRegex(text, r'command\s*=\s*apply')
            self.assertIn("expect_failures", text)

    def test_examples_do_not_embed_values_or_autoload(self):
        hcp = json.loads((ROOT / "hcp" / "inputs.example.json").read_text())
        self.assertEqual(set(hcp), {"hcp_variables"})
        oci = json.loads((ROOT / "oci" / "inputs.example.json").read_text())
        self.assertEqual(oci["secret_readers"], {})
        self.assertIn("replacewith", oci["oci_vault"]["tenancy_id"])
        for stage in ("hcp", "oci"):
            self.assertFalse(list((ROOT / stage).glob("*.auto.tfvars*")))
            self.assertFalse(list((ROOT / stage).glob("terraform.tfvars*")))


@unittest.skipUnless(os.environ.get("SECRET_CONTROL_TERRAFORM"), "requires explicitly selected Terraform")
class TerraformSecretTransportTests(unittest.TestCase):
    """Run the exact production variable definitions through Terraform's env parser."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.root.chmod(0o700)
        # This exact file contains the production type, validation and jsondecode.
        # No provider, resource, credentials or live API is part of this fixture.
        (self.root / "variables.tf").write_bytes((ROOT / "hcp/variables.tf").read_bytes())
        self.env = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("TF_", "TFE_", "OCI_"))
        }
        self.env.update(TF_CLI_CONFIG_FILE="/dev/null", CHECKPOINT_DISABLE="1", TF_IN_AUTOMATION="1")
        self.tf("init", "-backend=false", "-input=false", "-no-color")

    def tf(self, *args, success=True):
        result = subprocess.run(
            [os.environ["SECRET_CONTROL_TERRAFORM"], *args], cwd=self.root,
            env=self.env, capture_output=True, text=True, timeout=60, check=False,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def inputs(self, values, identifiers=None):
        identifiers = list(values) if identifiers is None else identifiers
        metadata = {
            key: {"key": key.upper(), "category": "env", "revision": 1,
                  "workspace_id": "ws-0000000000000001"}
            for key in identifiers
        }
        self.env["TF_VAR_hcp_variables"] = json.dumps(metadata, separators=(",", ":"))
        self.env["TF_VAR_hcp_secret_values_json"] = json.dumps(values, separators=(",", ":"))

    def test_actual_environment_preserves_bytes_and_omits_ephemeral_plan_input(self):
        values = {
            "interpolation": "HCP_VECTOR_${missing}",
            "directive": "HCP_VECTOR_%{ if true }content%{ endif }",
            "escaped": "HCP_VECTOR_$${escaped} %%{escaped}",
            "unicode": "HCP_VECTOR_\u00e9\u96ea\U0001f642",
            "multiline": 'HCP_VECTOR_line1\nline2\r\n\t"quote"\\path',
            "empty": "",
        }
        self.inputs(values)
        tests = self.root / "tests"
        tests.mkdir()
        assertions = [
            'assert {\n condition = sha256(local.hcp_secret_values[' + json.dumps(key) + ']) == "'
            + hashlib.sha256(value.encode()).hexdigest() + '"\n error_message = "Secret transport changed bytes"\n}\n'
            for key, value in values.items()
        ]
        (tests / "transport.tftest.hcl").write_text('run "transport" {\n command = plan\n' + ''.join(assertions) + '}\n')
        self.tf("test", "-no-color")
        self.tf("plan", "-input=false", "-no-color", "-out=synthetic.tfplan")
        shown = self.tf("show", "-json", "synthetic.tfplan")
        plan = json.loads(shown.stdout)
        # Terraform may retain the declaration's inert default, never the supplied payload.
        if "hcp_secret_values_json" in plan.get("variables", {}):
            self.assertEqual(plan["variables"]["hcp_secret_values_json"], {"value": "{}"})
        self.assertNotIn("HCP_VECTOR_", shown.stdout)
        self.assertFalse(list(self.root.glob("*.tfstate*")))

    def test_invalid_json_shapes_and_value_types_are_rejected(self):
        self.inputs({})
        self.tf("plan", "-input=false", "-no-color")
        invalid = [None, [], "string", 1, True, {"one": None}, {"one": 1},
                   {"one": False}, {"one": []}, {"one": {}}, {}, {"one": "ok", "extra": "bad"}]
        for value in invalid:
            with self.subTest(kind=repr(value)):
                self.inputs(value, identifiers=["one"])
                result = self.tf("plan", "-input=false", "-no-color", success=False)
                self.assertIn("Invalid value for variable", result.stderr)
        self.inputs({"one": "ok"})
        self.env["TF_VAR_hcp_secret_values_json"] = "not JSON"
        self.tf("plan", "-input=false", "-no-color", success=False)


if __name__ == "__main__":
    unittest.main()
