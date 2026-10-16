terraform {
  required_version = ">= 1.11.0, < 2.0.0"

  required_providers {
    tfe = {
      source  = "hashicorp/tfe"
      version = "= 0.76.0"
    }
  }

  # Run a copy of this root in a private execution directory, separate from OCI.
  backend "local" {}
}

provider "tfe" {
  hostname = "app.terraform.io"
}
