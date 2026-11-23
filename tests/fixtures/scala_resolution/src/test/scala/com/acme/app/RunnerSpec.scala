package com.acme.app

import com.acme.util.Helpers

class RunnerSpec {
  def test_run(): Unit = {
    new Runner(null).run(Nil)
    Helpers.twice(4)
  }
}
