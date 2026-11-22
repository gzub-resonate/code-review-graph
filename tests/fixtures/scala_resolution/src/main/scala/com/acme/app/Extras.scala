package com.acme.app

import com.acme.model.Circle
import com.acme.util._
import com.acme.util.Palette._

trait Secrets {
  private def hidden(): Int = 1
  def shown(): Int = 2
}

class Peek extends Secrets {
  def a(): Int = hidden()
  def b(): Int = shown()
}

class Parent {
  def greet(): String = "hi"
}

class Child extends Parent {
  override def greet(): String = super.greet() + "!"
}

class Blocked extends scala.Serializable {
  def a(): String = render()
}

class Wild {
  def a(): Int = Helpers.twice(1)
  def b(): Int = Helpers.twice(1, 2)
  def c(): Unit = assert(true)
  def d(): Int = {
    val c = new Circle(1.0)
    c.area().toInt
  }
  def e(): Circle = new Circle(1.0).grow()
}

case class Wrapper(a: Int)

object Wrapper {
  def apply(s: String): Wrapper = new Wrapper(s.length)
}

class UsesWrapper {
  def a(): Wrapper = Wrapper(1)
}
