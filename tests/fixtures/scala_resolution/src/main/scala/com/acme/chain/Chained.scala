package com.acme
package chain

import util.Helpers

object Chained {
  object Nested {
    def deep(): Int = 1
  }

  def go(): Int = Helpers.twice(3)
  def local(): Int = Nested.deep()
}
