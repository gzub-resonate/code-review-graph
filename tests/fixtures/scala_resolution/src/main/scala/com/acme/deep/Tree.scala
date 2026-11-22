package com.acme.deep

class Top {
  def topMethod(): Int = 1
}

class Mid extends Top

class Leaf extends Mid {
  def own(): Int = topMethod()
}

class External2 extends scala.collection.immutable.Seq[Int] {
  def own(): Int = size()
}

object DeepUse {
  def run(leaf: Leaf, other: List[Leaf]): Int = {
    other.head.own()
    leaf.topMethod() + leaf.own()
  }
}
