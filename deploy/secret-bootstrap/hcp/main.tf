resource "tfe_variable" "secret" {
  for_each = var.hcp_variables

  key              = each.value.key
  category         = each.value.category
  workspace_id     = each.value.workspace_id
  variable_set_id  = each.value.variable_set_id
  value_wo         = lookup(local.hcp_secret_values, each.key, null)
  value_wo_version = each.value.revision
  sensitive        = true
  hcl              = false
}

output "hcp_variable_ids" {
  description = "Managed variable IDs only; no values are exported."
  value       = { for id, variable in tfe_variable.secret : id => variable.id }
}
