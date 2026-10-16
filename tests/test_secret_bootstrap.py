"""Keep the standalone Terraform roots out of the secret-persistence path.

These static fences run without Terraform or credentials in the normal suite.
The adjacent mocked Terraform tests exercise accepted and rejected inputs.
"""

import json
import re
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
        values = block(text, "variable", "hcp_secret_values")
        self.assertRegex(values, r'sensitive\s*=\s*true')
        self.assertRegex(values, r'ephemeral\s*=\s*true')
        self.assertIn("toset(keys(var.hcp_secret_values)) == toset(keys(var.hcp_variables))", values)
        resource = block(text, "resource", "tfe_variable")
        self.assertRegex(resource, r'for_each\s*=\s*var.hcp_variables')
        self.assertRegex(resource, r'value_wo\s*=\s*lookup\(var.hcp_secret_values, each.key, null\)')
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


if __name__ == "__main__":
    unittest.main()
