package com.acme.app

import com.acme.model.{Circle, Geometry, Point, Store => ModelStore}
import com.acme.util.Helpers
import com.acme.util.Palette._
import scala.collection.mutable

class Runner(store: ModelStore) extends Base {
  val shape: Circle = new Circle(2.0)

  def run(items: List[String]): Unit = {
    Helpers.twice(2)
    Geometry.origin()
    val c = new Circle(1.0)
    c.area()
    shape.grow()
    store.save("x")
    ModelStore()
    Point(1, 2)
    inheritedHelper()
    this.inheritedHelper()
    render()
    mutable.Map()
    println("x")
    val m = mutable.Map[Int, Int]()
    m.drop(1)
    items.map(x => x.load())
    Helpers.pick(1)
    Unknown.thing()
  }
}

object Entry extends App {
  new Runner(ModelStore()).run(Nil)
}
