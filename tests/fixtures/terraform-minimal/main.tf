terraform {
  required_version = ">= 1.5.0"
}

variable "name" {
  description = "Synthetic value for the Terraform validation contract."
  type        = string
  default     = "contract"
}

output "name" {
  description = "Prove that the fixture's input is used."
  value       = var.name
}
