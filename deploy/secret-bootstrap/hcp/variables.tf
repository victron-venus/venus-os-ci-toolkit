variable "hcp_variables" {
  description = "Nonsecret destination catalog. Each opaque ID owns one explicitly selected variable."
  type = map(object({
    key             = string
    category        = string
    revision        = number
    workspace_id    = optional(string)
    variable_set_id = optional(string)
  }))
  default  = {}
  nullable = false

  validation {
    condition = alltrue([
      for id, entry in var.hcp_variables :
      can(regex("^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", id)) &&
      contains(["env", "terraform"], entry.category) &&
      can(regex(entry.category == "env" ? "^[A-Za-z_][A-Za-z0-9_]*$" : "^[A-Za-z_][A-Za-z0-9_-]*$", entry.key)) &&
      entry.revision >= 1 && entry.revision == floor(entry.revision)
    ])
    error_message = "Use opaque entry IDs, valid variable keys/categories and positive integer revisions."
  }

  validation {
    condition = alltrue([
      for entry in values(var.hcp_variables) :
      (entry.workspace_id != null) != (entry.variable_set_id != null) &&
      (entry.workspace_id == null ? true : can(regex("^ws-[A-Za-z0-9]{16}$", entry.workspace_id))) &&
      (entry.variable_set_id == null ? true : can(regex("^varset-[A-Za-z0-9]{16}$", entry.variable_set_id)))
    ])
    error_message = "Each entry must select exactly one valid workspace_id or variable_set_id."
  }

  validation {
    condition = length(distinct([
      for entry in values(var.hcp_variables) :
      jsonencode([entry.workspace_id, entry.variable_set_id, entry.category, entry.key])
    ])) == length(var.hcp_variables)
    error_message = "Two entries cannot own the same destination, category and key."
  }
}

variable "hcp_secret_values" {
  description = "Runtime-only values keyed by the exact same IDs as hcp_variables. Never store in source or tfvars."
  type        = map(string)
  sensitive   = true
  ephemeral   = true
  default     = {}
  nullable    = false

  validation {
    condition     = toset(keys(var.hcp_secret_values)) == toset(keys(var.hcp_variables))
    error_message = "The secret-value IDs must exactly match the destination catalog IDs."
  }

  validation {
    condition     = alltrue([for value in values(var.hcp_secret_values) : value != null])
    error_message = "Secret values must be strings, not null."
  }
}
