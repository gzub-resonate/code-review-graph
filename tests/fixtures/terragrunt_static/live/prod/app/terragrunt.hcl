include "root" {
  path   = find_in_parent_folders("root.hcl")
  expose = true
}

include "envcommon" {
  path   = "${dirname(find_in_parent_folders("root.hcl"))}/_envcommon/app.hcl"
  expose = true
}

include {
  path = find_in_parent_folders()
}

terraform {
  source = "${get_repo_root()}/modules//app"
}

dependency "db" {
  config_path = "${get_terragrunt_dir()}/../db"
}

dependencies {
  paths = ["../db", "${get_terragrunt_dir()}/../lib", "../missing"]
}

inputs = {
  name = "app"
}
