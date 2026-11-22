# Settings shared by every "app" unit. This file is included, not run.
locals {
  base_source_url = "git::https://git.example.com/acme/mods.git//app"
}

terraform {
  source = "${local.base_source_url}?ref=v1.2.0"
}
