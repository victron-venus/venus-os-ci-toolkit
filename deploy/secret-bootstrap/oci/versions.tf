terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "= 9.7.1"
    }
  }

  # This root has its own private state; never share it with the HCP stage.
  backend "local" {}
}

provider "oci" {
  region              = try(var.oci_vault.region, null)
  tenancy_ocid        = try(var.oci_vault.tenancy_id, null)
  config_file_profile = var.oci_config_file_profile
}
