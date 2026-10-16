variable "oci_config_file_profile" {
  description = "Explicit local OCI SDK config profile for enabled provisioning; never inferred as DEFAULT."
  type        = string
  default     = null

  validation {
    condition = var.oci_vault == null ? true : can(
      regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", var.oci_config_file_profile)
    )
    error_message = "Enabled OCI provisioning requires an explicit local config profile."
  }
}

variable "oci_vault" {
  description = "Optional dedicated vault topology. Credentials remain outside this catalog."
  type = object({
    tenancy_id     = string
    region         = string
    name           = string
    key_name       = string
    compartment_id = optional(string)
    create_compartment = optional(object({
      parent_compartment_id = string
      name                  = string
    }))
  })
  default = null

  validation {
    condition = var.oci_vault == null ? true : (
      can(regex("^ocid1\\.tenancy\\.[a-z0-9]+\\.[a-z0-9.-]+$", var.oci_vault.tenancy_id)) &&
      can(regex("^[a-z][a-z0-9-]+[a-z0-9]$", var.oci_vault.region)) &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", var.oci_vault.name)) &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", var.oci_vault.key_name))
    )
    error_message = "Vault creation requires an explicit tenancy OCID, region and simple nonsecret names."
  }

  validation {
    condition = var.oci_vault == null ? true : (
      (var.oci_vault.compartment_id != null) != (var.oci_vault.create_compartment != null)
    )
    error_message = "Select an existing compartment_id or create_compartment, never both or neither."
  }

  validation {
    condition = try(var.oci_vault.compartment_id, null) == null ? true : can(
      regex("^ocid1\\.compartment\\.[a-z0-9]+\\.[a-z0-9.-]+$", var.oci_vault.compartment_id)
    )
    error_message = "The existing compartment must be an explicit compartment OCID."
  }

  validation {
    condition = try(var.oci_vault.create_compartment, null) == null ? true : (
      can(regex("^ocid1\\.(tenancy|compartment)\\.[a-z0-9]+\\.[a-z0-9.-]+$", var.oci_vault.create_compartment.parent_compartment_id)) &&
      can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", var.oci_vault.create_compartment.name))
    )
    error_message = "A new compartment requires an explicit parent OCID and simple nonsecret name."
  }
}

variable "secret_readers" {
  description = "Optional exact-secret read grants. No grants or publisher/admin permissions by default."
  type = map(object({
    principal_type = string
    principal_id   = string
    secret_ids     = set(string)
  }))
  default  = {}
  nullable = false

  validation {
    condition     = length(var.secret_readers) == 0 || var.oci_vault != null
    error_message = "Reader policies require the explicitly selected dedicated vault."
  }

  validation {
    condition = alltrue([
      for id, reader in var.secret_readers :
      can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", id)) &&
      contains(["group", "dynamic-group"], reader.principal_type) &&
      can(regex(reader.principal_type == "group" ? "^ocid1\\.group\\.[a-z0-9]+\\.[a-z0-9.-]+$" : "^ocid1\\.dynamicgroup\\.[a-z0-9]+\\.[a-z0-9.-]+$", reader.principal_id)) &&
      length(reader.secret_ids) > 0 &&
      alltrue([for id in reader.secret_ids : can(regex("^ocid1\\.vaultsecret\\.[a-z0-9]+\\.[a-z0-9.-]+$", id))])
    ])
    error_message = "Reader grants require explicit group/dynamic-group OCIDs and a nonempty set of exact secret OCIDs."
  }
}
