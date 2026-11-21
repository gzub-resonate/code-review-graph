package com.acme.chain

import org.example.Helpers

object External {
  def go(): Int = Helpers.twice(1)

  def shadowed(): Int = {
    val Palette = new Other
    Palette.render()
  }
}

class Other {
  def render(): String = ""
}
