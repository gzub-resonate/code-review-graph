"""Any resolver that rebinds calls in unparsed files can mark the flows dirty.

``csharp_flows_dirty`` was the only such flag and was checked by name in four places. The
checks now read ``FLOWS_DIRTY_KEYS``, so a resolver adds its key there and every place that
decides between an incremental and a full retrace follows.
"""

from __future__ import annotations

from code_review_graph.flows import FLOWS_DIRTY_KEYS, flows_dirty, store_flows
from code_review_graph.graph import GraphStore


def _store(tmp_path) -> GraphStore:
    return GraphStore(tmp_path / "graph.db")


def test_flows_are_clean_when_no_resolver_set_a_flag(tmp_path):
    store = _store(tmp_path)
    try:
        assert not flows_dirty(store)
    finally:
        store.close()


def test_the_csharp_flag_is_listed():
    assert "csharp_flows_dirty" in FLOWS_DIRTY_KEYS


def test_each_listed_flag_marks_the_flows_dirty(tmp_path):
    for key in FLOWS_DIRTY_KEYS:
        folder = tmp_path / key
        folder.mkdir()
        store = _store(folder)
        try:
            store.set_metadata(key, "1")
            assert flows_dirty(store)
        finally:
            store.close()


def test_a_flag_that_is_not_one_does_not_count(tmp_path):
    store = _store(tmp_path)
    try:
        store.set_metadata(FLOWS_DIRTY_KEYS[0], "0")
        assert not flows_dirty(store)
    finally:
        store.close()


def test_storing_flows_clears_every_flag_and_nothing_else(tmp_path):
    store = _store(tmp_path)
    try:
        for key in FLOWS_DIRTY_KEYS:
            store.set_metadata(key, "1")
        store.set_metadata("unrelated_key", "keep")
        store_flows(store, [])
        assert not flows_dirty(store)
        assert all(store.get_metadata(key) is None for key in FLOWS_DIRTY_KEYS)
        assert store.get_metadata("unrelated_key") == "keep"
    finally:
        store.close()
