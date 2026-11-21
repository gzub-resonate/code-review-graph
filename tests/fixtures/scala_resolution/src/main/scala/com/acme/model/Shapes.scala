package com.acme.model

trait Shape {
  def area(): Double
  def describe(): String = "shape"
}

class Circle(radius: Double) extends Shape {
  def area(): Double = 3.14 * radius * radius
  def grow(): Circle = new Circle(radius + 1)
}

case class Point(x: Int, y: Int)

class Store {
  def save(item: String): Unit = ()
  def load(): String = ""
  def drop(n: Int): Unit = ()
}

object Store {
  def apply(): Store = new Store
  def default(): Store = new Store
}

object Geometry {
  def origin(): Point = Point(0, 0)
  def unit(): Circle = new Circle(1.0)
}
