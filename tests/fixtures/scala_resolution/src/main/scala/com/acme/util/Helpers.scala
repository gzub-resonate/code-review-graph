package com.acme.util

object Helpers {
  def twice(x: Int): Int = x * 2
  def load(path: String): String = path
  def assert(flag: Boolean): Unit = ()
  def pick(a: Int): Int = a
  def pick(a: String): String = a
}

object Palette {
  def render(): String = ""
  def twice(x: Int): Int = x + x
}
