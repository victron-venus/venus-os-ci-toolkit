mock_provider "tfe" {}

run "empty_by_default" {
  command = plan
  assert {
    condition     = length(tfe_variable.secret) == 0
    error_message = "No HCP destination may be selected implicitly."
  }
}

run "explicit_workspace_and_set" {
  command = plan
  variables {
    hcp_variables = {
      first  = { key = "SERVICE_TOKEN", category = "env", revision = 2, workspace_id = "ws-0000000000000001" }
      second = { key = "other_token", category = "terraform", revision = 3, variable_set_id = "varset-0000000000000001", description = "Existing café description\nKeep whitespace  " }
    }
    hcp_secret_values_json = jsonencode({ first = "synthetic-only", second = "synthetic-only-too" })
  }
  assert {
    condition = (
      tfe_variable.secret["first"].workspace_id == "ws-0000000000000001" &&
      tfe_variable.secret["second"].variable_set_id == "varset-0000000000000001" &&
      tfe_variable.secret["first"].value_wo_version == 2 &&
      tfe_variable.secret["second"].value_wo_version == 3 &&
      tfe_variable.secret["first"].sensitive &&
      !tfe_variable.secret["first"].hcl &&
      tfe_variable.secret["first"].description == "" &&
      tfe_variable.secret["second"].description == "Existing café description\nKeep whitespace  "
    )
    error_message = "Explicit destinations/revisions and sensitive non-HCL delivery must survive projection."
  }
}

run "reject_both_destinations" {
  command = plan
  variables {
    hcp_variables = {
      one = { key = "TOKEN", category = "env", revision = 1, workspace_id = "ws-0000000000000001", variable_set_id = "varset-0000000000000001" }
    }
    hcp_secret_values_json = jsonencode({ one = "synthetic-only" })
  }
  expect_failures = [var.hcp_variables]
}

run "reject_missing_destination" {
  command = plan
  variables {
    hcp_variables          = { one = { key = "TOKEN", category = "env", revision = 1 } }
    hcp_secret_values_json = jsonencode({ one = "synthetic-only" })
  }
  expect_failures = [var.hcp_variables]
}

run "reject_duplicate_ownership" {
  command = plan
  variables {
    hcp_variables = {
      one = { key = "TOKEN", category = "env", revision = 1, workspace_id = "ws-0000000000000001" }
      two = { key = "TOKEN", category = "env", revision = 2, workspace_id = "ws-0000000000000001" }
    }
    hcp_secret_values_json = jsonencode({ one = "synthetic-one", two = "synthetic-two" })
  }
  expect_failures = [var.hcp_variables]
}

run "reject_missing_value" {
  command = plan
  variables {
    hcp_variables          = { one = { key = "TOKEN", category = "env", revision = 1, workspace_id = "ws-0000000000000001" } }
    hcp_secret_values_json = jsonencode({})
  }
  expect_failures = [var.hcp_secret_values_json]
}

run "reject_unmapped_value" {
  command = plan
  variables {
    hcp_secret_values_json = jsonencode({ extra = "synthetic-only" })
  }
  expect_failures = [var.hcp_secret_values_json]
}

run "reject_fractional_revision" {
  command = plan
  variables {
    hcp_variables          = { one = { key = "TOKEN", category = "env", revision = 1.5, workspace_id = "ws-0000000000000001" } }
    hcp_secret_values_json = jsonencode({ one = "synthetic-only" })
  }
  expect_failures = [var.hcp_variables]
}

run "reject_environment_key_injection" {
  command = plan
  variables {
    hcp_variables          = { one = { key = "TOKEN\nOTHER", category = "env", revision = 1, workspace_id = "ws-0000000000000001" } }
    hcp_secret_values_json = jsonencode({ one = "synthetic-only" })
  }
  expect_failures = [var.hcp_variables]
}

run "reject_overlong_description" {
  command = plan
  variables {
    hcp_variables = {
      one = { key = "TOKEN", category = "env", revision = 1, workspace_id = "ws-0000000000000001", description = join("", [for i in range(513) : "x"]) }
    }
    hcp_secret_values_json = jsonencode({ one = "synthetic-only" })
  }
  expect_failures = [var.hcp_variables]
}
