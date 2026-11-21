locals {
  a = local.b
  b = local.a
}

include "loop" {
  path = get_parent_terragrunt_dir("loop")
}

terraform {
  source = local.a
}

dependency "x" {
  config_path = local.b
}
