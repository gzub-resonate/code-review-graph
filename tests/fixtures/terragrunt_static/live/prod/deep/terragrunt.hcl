include "root" {
  path = find_in_parent_folders("missing.hcl")
}

terraform {
  source = "${get_repo_root()}/../outside"
}

dependency "up" {
  config_path = "${get_terragrunt_dir()}/../../../../../.."
}

dependency "gone" {
  config_path = "${get_repo_root()}/live/prod/gone"
}
