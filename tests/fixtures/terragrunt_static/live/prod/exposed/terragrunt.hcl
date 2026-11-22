include "envcommon" {
  path   = "${get_repo_root()}/_envcommon/app.hcl"
  expose = true
}

terraform {
  source = "${include.envcommon.locals.base_source_url}?ref=v9.0.0"
}
