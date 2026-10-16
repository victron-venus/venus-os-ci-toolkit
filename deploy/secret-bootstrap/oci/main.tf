locals {
  enabled            = var.oci_vault != null
  create_compartment = try(var.oci_vault.create_compartment, null) != null
  compartment_id     = local.create_compartment ? oci_identity_compartment.recovery[0].id : try(var.oci_vault.compartment_id, null)
}

resource "oci_identity_compartment" "recovery" {
  count = local.create_compartment ? 1 : 0

  compartment_id = var.oci_vault.create_compartment.parent_compartment_id
  name           = var.oci_vault.create_compartment.name
  description    = "Dedicated recoverable secret distribution resources"
  enable_delete  = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_kms_vault" "recovery" {
  count = local.enabled ? 1 : 0

  compartment_id = local.compartment_id
  display_name   = var.oci_vault.name
  vault_type     = "DEFAULT"

  lifecycle {
    prevent_destroy = true
  }
}

resource "oci_kms_key" "recovery" {
  count = local.enabled ? 1 : 0

  compartment_id      = local.compartment_id
  display_name        = var.oci_vault.key_name
  management_endpoint = oci_kms_vault.recovery[0].management_endpoint
  protection_mode     = "SOFTWARE"
  key_shape {
    algorithm = "AES"
    length    = 32
  }

  lifecycle {
    prevent_destroy = true
  }
}

# No arbitrary policy strings. Reads require both this vault and an exact secret.
# Secret creation/version publication is owned by the separate publisher.
resource "oci_identity_policy" "reader" {
  for_each = local.enabled ? var.secret_readers : {}

  compartment_id = local.compartment_id
  name           = "secret-reader-${each.key}"
  description    = "Read only explicitly selected secret bundles"
  statements = [
    for secret_id in sort(tolist(each.value.secret_ids)) :
    "Allow ${each.value.principal_type} id ${each.value.principal_id} to read secret-bundles in compartment id ${local.compartment_id} where all {target.vault.id = '${oci_kms_vault.recovery[0].id}', target.secret.id = '${secret_id}'}"
  ]
}

output "vault" {
  description = "Resource bindings only. Secret payloads are never read or managed here."
  value = local.enabled ? {
    region         = var.oci_vault.region
    compartment_id = local.compartment_id
    vault_id       = oci_kms_vault.recovery[0].id
    key_id         = oci_kms_key.recovery[0].id
  } : null
}
