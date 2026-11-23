package com.acme.dup

object Twin {
  def hi(): String = "two"
}

object UsesTwin {
  def call(): String = Twin.hi()
}
