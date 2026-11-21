include "root" {
  path = find_in_parent_folders("root.hcl")
}

terraform {
  source = "${get_parent_terragrunt_dir("root")}/modules//lib"
}

dependency "app" {
  config_path = "${get_parent_terragrunt_dir("root")}/${path_relative_to_include("root")}/../app"
}

dependency "db" {
  config_path = "${get_terragrunt_dir()}/${get_path_to_repo_root()}/live/prod/db"
}

dependencies {
  paths = ["${path_relative_from_include("root")}/live/prod/db"]
}
