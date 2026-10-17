# Repository management with Terraform

Using the toolkit's workflows does not require a Terraform import. Start with
the [consumer guide](docs/CONSUMER_GUIDE.md) to configure CI for your project.

The GitHub repository itself is declared in
[terraform-github-victron](https://github.com/4alvit/terraform-github-victron).
Repository settings and Terraform state maintenance belong in that management
repository, rather than in a toolkit checkout.

For maintainers investigating an unmanaged or migrated repository:

1. Follow the management repository's backend and recovery instructions before
   inspecting state. An empty or unavailable backend is not evidence that an
   existing repository needs importing.
2. Check the current resource definition in its `main.tf`. The toolkit is already
   declared under the `venus_os_ci_toolkit` key of `module.repos`.
3. Compare the configuration with the selected state. The module resource address
   is `module.repos["venus_os_ci_toolkit"].github_repository.this`.
4. If state still uses an older address, follow that repository's
   [state migration guide](https://github.com/4alvit/terraform-github-victron/blob/main/STATE_MIGRATION.md).
   Review the resulting plan before applying any management changes.

The former `github_repository.venus_os_ci_toolkit` address referred to the old
configuration. This file no longer presents that one-time import note as a
pending setup step.
