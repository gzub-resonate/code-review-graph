include "envcommon" {
  path   = "../../../_envcommon/app.hcl"
  expose = true
}

include "common" {
  path   = find_in_parent_folders("common.hcl")
  expose = true
}

locals {
  module_dir = "${get_repo_root()}/modules"
  app_module = "${local.module_dir}//app"
}

terraform {
  source = local.app_module
}

# A remote address used as a path never names a file in the repository.
dependency "shared" {
  config_path = "${get_terragrunt_dir()}/../${include.envcommon.locals.base_source_url}"
}
