mock_provider "oci" {
  mock_resource "oci_identity_compartment" {
    defaults = { id = "ocid1.compartment.oc1..synthetic" }
  }
  mock_resource "oci_kms_vault" {
    defaults = {
      id                  = "ocid1.vault.oc1.test.synthetic"
      management_endpoint = "https://vault.example.invalid"
    }
  }
}

variables {
  oci_config_file_profile = "SYNTHETIC"
}

run "empty_by_default" {
  command = plan
  assert {
    condition = (
      length(oci_identity_compartment.recovery) == 0 &&
      length(oci_kms_vault.recovery) == 0 &&
      length(oci_kms_key.recovery) == 0 &&
      length(oci_identity_policy.reader) == 0
    )
    error_message = "No resource or grant may be selected implicitly."
  }
}

run "dedicated_compartment_vault_key" {
  command = plan
  variables {
    oci_vault = {
      tenancy_id = "ocid1.tenancy.oc1..synthetic"
      region     = "us-ashburn-1"
      name       = "synthetic-recovery"
      key_name   = "synthetic-recovery-key"
      create_compartment = {
        parent_compartment_id = "ocid1.tenancy.oc1..synthetic"
        name                  = "synthetic-compartment"
      }
    }
  }
  assert {
    condition = (
      length(oci_identity_compartment.recovery) == 1 &&
      !oci_identity_compartment.recovery[0].enable_delete &&
      oci_kms_vault.recovery[0].vault_type == "DEFAULT" &&
      oci_kms_key.recovery[0].protection_mode == "SOFTWARE" &&
      oci_kms_key.recovery[0].key_shape[0].algorithm == "AES" &&
      oci_kms_key.recovery[0].key_shape[0].length == 32 &&
      length(oci_identity_policy.reader) == 0
    )
    error_message = "The explicit three-resource topology must not grant access or select a paid dedicated vault/HSM key."
  }
}

run "existing_compartment" {
  command = plan
  variables {
    oci_vault = {
      tenancy_id     = "ocid1.tenancy.oc1..synthetic"
      region         = "us-ashburn-1"
      name           = "synthetic-recovery"
      key_name       = "synthetic-recovery-key"
      compartment_id = "ocid1.compartment.oc1..existing"
    }
  }
  assert {
    condition = (
      length(oci_identity_compartment.recovery) == 0 &&
      oci_kms_vault.recovery[0].compartment_id == "ocid1.compartment.oc1..existing"
    )
    error_message = "An existing compartment must be used exactly."
  }
}

run "exact_reader_grant" {
  command = plan
  override_resource {
    target          = oci_kms_vault.recovery[0]
    values          = { id = "ocid1.vault.oc1.test.synthetic" }
    override_during = plan
  }
  variables {
    oci_vault = {
      tenancy_id     = "ocid1.tenancy.oc1..synthetic"
      region         = "us-ashburn-1"
      name           = "synthetic-recovery"
      key_name       = "synthetic-recovery-key"
      compartment_id = "ocid1.compartment.oc1..existing"
    }
    secret_readers = {
      app = { principal_type = "dynamic-group", principal_id = "ocid1.dynamicgroup.oc1..synthetic", secret_ids = ["ocid1.vaultsecret.oc1.test.synthetic"] }
    }
  }
  assert {
    condition = oci_identity_policy.reader["app"].statements == tolist([
      "Allow dynamic-group id ocid1.dynamicgroup.oc1..synthetic to read secret-bundles in compartment id ocid1.compartment.oc1..existing where all {target.vault.id = 'ocid1.vault.oc1.test.synthetic', target.secret.id = 'ocid1.vaultsecret.oc1.test.synthetic'}" # gitleaks:allow -- synthetic OCIDs in policy assertion
    ])
    error_message = "Reader permissions must preserve exact principal/compartment/vault/secret scope."
  }
}

run "reject_both_compartment_choices" {
  command = plan
  variables {
    oci_vault = {
      tenancy_id         = "ocid1.tenancy.oc1..synthetic"
      region             = "us-ashburn-1"
      name               = "synthetic-recovery"
      key_name           = "synthetic-recovery-key"
      compartment_id     = "ocid1.compartment.oc1..existing"
      create_compartment = { parent_compartment_id = "ocid1.tenancy.oc1..synthetic", name = "synthetic-compartment" }
    }
  }
  expect_failures = [var.oci_vault]
}

run "reject_implicit_auth_profile" {
  command = plan
  variables {
    oci_config_file_profile = null
    oci_vault = {
      tenancy_id     = "ocid1.tenancy.oc1..synthetic"
      region         = "us-ashburn-1"
      name           = "synthetic-recovery"
      key_name       = "synthetic-recovery-key"
      compartment_id = "ocid1.compartment.oc1..existing"
    }
  }
  expect_failures = [var.oci_config_file_profile]
}

run "reject_missing_compartment" {
  command = plan
  variables {
    oci_vault = {
      tenancy_id = "ocid1.tenancy.oc1..synthetic"
      region     = "us-ashburn-1"
      name       = "synthetic-recovery"
      key_name   = "synthetic-recovery-key"
    }
  }
  expect_failures = [var.oci_vault]
}

run "reject_policy_without_vault" {
  command = plan
  variables {
    secret_readers = {
      app = { principal_type = "group", principal_id = "ocid1.group.oc1..synthetic", secret_ids = ["ocid1.vaultsecret.oc1.test.synthetic"] }
    }
  }
  expect_failures = [var.secret_readers]
}

run "reject_policy_injection" {
  command = plan
  variables {
    oci_vault = {
      tenancy_id     = "ocid1.tenancy.oc1..synthetic"
      region         = "us-ashburn-1"
      name           = "synthetic-recovery"
      key_name       = "synthetic-recovery-key"
      compartment_id = "ocid1.compartment.oc1..existing"
    }
    secret_readers = {
      app = { principal_type = "any-user", principal_id = "all", secret_ids = ["*"] }
    }
  }
  expect_failures = [var.secret_readers]
}
