locals {
  planted = "planted-local-token-3b7c"
}

include "from_env" {
  path = get_env("CRG_FIXTURE_ENV", "planted-env-default-token-5d1e")
}

include "from_command" {
  path = run_cmd("--terragrunt-quiet", "echo", "planted-command-token-8a40")
}

terraform {
  source = "git::https://planted-user:planted-pass-7f2b@git.example.com/acme/mods.git//x?ref=${var.version}"
}

dependency "a" {
  config_path = sops_decrypt_file("secrets.enc.json")
}

dependency "b" {
  config_path = read_terragrunt_config(find_in_parent_folders("common.hcl")).locals.module_repo
}

dependency "c" {
  config_path = "../${each.key}"
}

dependency "d" {
  config_path = format("%s/x", local.planted)
}

dependency "e" {
  config_path = local.not_defined
}

include "hidden" {
  path = "../../../common.hcl"
}

dependency "f" {
  config_path = include.hidden.locals.module_repo
}

dependencies {
  paths = concat(["../a"], ["../b"])
}
