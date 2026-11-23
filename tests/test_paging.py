"""Cursor paging for list tools whose ceiling would otherwise hide the rest of a list."""

from __future__ import annotations

import base64
import inspect
import json
import tempfile
from pathlib import Path

import pytest

import code_review_graph.main as main_module
import code_review_graph.tools.community_tools as community_module
import code_review_graph.tools.flows_tools as flows_module
from code_review_graph import paging
from code_review_graph.communities import store_communities
from code_review_graph.flows import store_flows
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import (
    get_hub_nodes_func,
    list_communities_func,
    list_flows,
)

# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

SCOPE = paging.make_scope("t", sort_by="name")
STAMP = "stamp-1"


def walk(items, limit, hard_cap=1000, scope=SCOPE, stamp=STAMP):
    """Read a whole list page by page and return the pages."""
    pages = []
    cursor = None
    while True:
        page = paging.paginate(items, limit, hard_cap, cursor, scope=scope, stamp=stamp)
        pages.append(page)
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor
        assert len(pages) < 10_000


@pytest.mark.parametrize("limit", [1, 3, 7, 10, 25, 99])
def test_pages_cover_the_list_once_in_order(limit):
    items = list(range(25))
    pages = walk(items, limit)
    assert [i for p in pages for i in p.items] == items
    assert all(p.total == 25 for p in pages)
    assert [p.offset for p in pages] == list(range(0, 25, min(limit, 25)))
    assert all(len(p.items) <= limit for p in pages)
    assert [p.truncated for p in pages] == [True] * (len(pages) - 1) + [False]


def test_a_whole_list_has_no_cursor_and_adds_no_keys():
    page = paging.paginate([1, 2, 3], 10, 100, scope=SCOPE, stamp=STAMP)
    assert page.items == [1, 2, 3] and page.next_cursor is None and not page.truncated
    assert page.fields() == {}
    assert paging.shown_range(page) == ""


def test_the_ceiling_bounds_a_page_not_the_list():
    items = list(range(25))
    pages = walk(items, limit=500, hard_cap=10)
    assert [len(p.items) for p in pages] == [10, 10, 5]
    assert [i for p in pages for i in p.items] == items


def test_first_page_fields_and_summary_match_the_capped_form():
    first = paging.paginate(list(range(25)), 10, 100, scope=SCOPE, stamp=STAMP)
    assert set(first.fields()) == {"next_cursor"}  # no offset on the first page
    assert paging.shown_range(first) == ", showing 10 of 25"
    second = paging.paginate(
        list(range(25)), 10, 100, first.next_cursor, scope=SCOPE, stamp=STAMP,
    )
    assert second.fields()["offset"] == 10 and "next_cursor" in second.fields()
    assert paging.shown_range(second) == ", showing 11-20 of 25"
    last = paging.paginate(
        list(range(25)), 10, 100, second.next_cursor, scope=SCOPE, stamp=STAMP,
    )
    assert set(last.fields()) == {"offset"} and paging.shown_range(last) == ", showing 21-25 of 25"


def test_a_page_size_may_change_between_pages():
    items = list(range(25))
    first = paging.paginate(items, 4, 100, scope=SCOPE, stamp=STAMP)
    second = paging.paginate(items, 9, 100, first.next_cursor, scope=SCOPE, stamp=STAMP)
    assert second.items == items[4:13] and second.offset == 4


def test_a_cursor_from_another_query_is_refused():
    page = paging.paginate(list(range(10)), 3, 100, scope=SCOPE, stamp=STAMP)
    other = paging.make_scope("t", sort_by="size")
    with pytest.raises(paging.CursorError, match="different query"):
        paging.paginate(list(range(10)), 3, 100, page.next_cursor, scope=other, stamp=STAMP)
    assert paging.make_scope("t", sort_by="name") == SCOPE
    assert paging.make_scope("u", sort_by="name") != SCOPE


def test_a_cursor_from_an_older_graph_is_refused():
    page = paging.paginate(list(range(10)), 3, 100, scope=SCOPE, stamp=STAMP)
    with pytest.raises(paging.CursorError, match="graph changed"):
        paging.paginate(list(range(10)), 3, 100, page.next_cursor, scope=SCOPE, stamp="stamp-2")


def _cursor(**payload):
    raw = json.dumps(payload).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@pytest.mark.parametrize("bad", [
    "not a cursor",
    "////",
    "e30",  # {}
    _cursor(v=2, o=1, s=SCOPE, g=STAMP),
    _cursor(v=1, o=-1, s=SCOPE, g=STAMP),
    _cursor(v=1, o=True, s=SCOPE, g=STAMP),
    _cursor(v=1, o="3", s=SCOPE, g=STAMP),
    _cursor(v=1, s=SCOPE, g=STAMP),
    base64.urlsafe_b64encode(b"[1, 2]").decode("ascii"),
    base64.urlsafe_b64encode("é".encode("utf-8")).decode("ascii"),
    "A" * 5000,
])
def test_a_malformed_cursor_is_refused_with_a_restart_hint(bad):
    with pytest.raises(paging.CursorError, match="start over"):
        paging.paginate(list(range(10)), 3, 100, bad, scope=SCOPE, stamp=STAMP)


def test_a_cursor_past_the_end_is_refused():
    cursor = _cursor(v=1, o=11, s=SCOPE, g=STAMP)
    with pytest.raises(paging.CursorError, match="past the end"):
        paging.paginate(list(range(10)), 3, 100, cursor, scope=SCOPE, stamp=STAMP)
    at_end = _cursor(v=1, o=10, s=SCOPE, g=STAMP)
    page = paging.paginate(list(range(10)), 3, 100, at_end, scope=SCOPE, stamp=STAMP)
    assert page.items == [] and page.next_cursor is None
    assert paging.shown_range(page) == ", none shown (offset 10 of 10)"


def test_a_cursor_is_url_safe_and_small():
    cursor = paging.paginate(list(range(10)), 3, 100, scope=SCOPE, stamp=STAMP).next_cursor
    assert cursor is not None and len(cursor) < 140
    assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

COUNT = 60


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))
    src = (root / "m.py").as_posix()
    ids = []
    for i in range(COUNT):
        ids.append(store.upsert_node(NodeInfo(
            kind="Function", name=f"f{i:03d}", file_path=src,
            line_start=i * 3 + 1, line_end=i * 3 + 2, language="python",
        )))
    # Degrees tie in groups of five, so a ranking has many equal neighbours.
    for i in range(COUNT):
        for j in range(i % 5):
            store.upsert_edge(EdgeInfo(
                kind="CALLS", source=f"{src}::f{i:03d}",
                target=f"{src}::f{(i + j + 1) % COUNT:03d}", file_path=src, line=i,
            ))
    store.commit()
    store_flows(store, [
        {
            "name": f"flow{i:03d}", "entry_point_id": ids[i], "depth": i % 4,
            "node_count": 2, "file_count": 1, "criticality": (i % 6) / 10.0,
            "path": [ids[i]],
        }
        for i in range(COUNT)
    ])
    store_communities(store, [
        {"name": f"comm{i:03d}", "size": 1 + i % 5, "cohesion": (i % 7) / 10.0, "members": []}
        for i in range(COUNT)
    ])
    yield root, store
    store.close()
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


def error_of(call, **args) -> str:
    """The message of a refused call: an error reply, or a raised error for tools that raise."""
    try:
        reply = call(**args)
    except paging.CursorError as exc:
        return str(exc)
    assert reply["status"] == "error", reply
    return reply["error"]


def read_all(call, key, limit_name, limit, **args):
    """Page through a tool and return (rows, replies)."""
    rows: list = []
    replies: list = []
    cursor = None
    while True:
        reply = call(**{limit_name: limit}, cursor=cursor, **args)
        assert reply["status"] == "ok", reply
        replies.append(reply)
        rows.extend(reply[key])
        cursor = reply.get("next_cursor")
        if cursor is None:
            return rows, replies
        assert len(replies) < 1000


TOOLS = {
    "flows": dict(
        call=lambda root: (lambda **a: list_flows(repo_root=str(root), **a)),
        key="flows", limit_name="limit", id_key="name", args={"sort_by": "criticality"},
    ),
    "communities": dict(
        call=lambda root: (lambda **a: list_communities_func(repo_root=str(root), **a)),
        key="communities", limit_name="max_results", id_key="name", args={"sort_by": "size"},
    ),
    "hubs": dict(
        call=lambda root: (lambda **a: get_hub_nodes_func(repo_root=str(root), **a)),
        key="hub_nodes", limit_name="top_n", id_key="qualified_name", args={},
    ),
}


@pytest.mark.parametrize("tool", sorted(TOOLS))
@pytest.mark.parametrize("limit", [1, 7, 25])
def test_paging_a_tool_returns_every_row_once(repo, tool, limit):
    root, _ = repo
    spec = TOOLS[tool]
    call = spec["call"](root)
    whole = call(**{spec["limit_name"]: 200}, **spec["args"])
    assert whole["status"] == "ok" and whole["truncated"] is False
    assert "next_cursor" not in whole and "offset" not in whole
    expected = [r[spec["id_key"]] for r in whole[spec["key"]]]
    assert len(expected) > limit

    rows, replies = read_all(call, spec["key"], spec["limit_name"], limit, **spec["args"])
    assert [r[spec["id_key"]] for r in rows] == expected
    assert len(set(expected)) == len(expected)
    assert all(r["total"] == len(expected) for r in replies)
    assert [r["truncated"] for r in replies] == [True] * (len(replies) - 1) + [False]
    assert "next_cursor" not in replies[-1]


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_the_first_page_keeps_its_summary_and_adds_only_a_cursor(repo, tool):
    root, _ = repo
    spec = TOOLS[tool]
    call = spec["call"](root)
    first = call(**{spec["limit_name"]: 10}, **spec["args"])
    total = first["total"]
    assert first["truncated"] is True and "offset" not in first
    assert f", showing 10 of {total}" in first["summary"]
    second = call(**{spec["limit_name"]: 10}, cursor=first["next_cursor"], **spec["args"])
    assert second["offset"] == 10
    assert f", showing 11-20 of {total}" in second["summary"]


def test_the_ceiling_bounds_a_page_not_the_flow_list(repo, monkeypatch):
    root, _ = repo
    monkeypatch.setattr(flows_module, "_MAX_FLOWS", 25)
    rows, replies = read_all(
        lambda **a: list_flows(repo_root=str(root), **a), "flows", "limit", 500,
    )
    assert [len(r["flows"]) for r in replies] == [25, 25, 10]
    assert len({r["id"] for r in rows}) == COUNT


def test_the_ceiling_bounds_a_page_not_the_community_list(repo, monkeypatch):
    root, _ = repo
    monkeypatch.setattr(community_module, "_MAX_COMMUNITIES", 25)
    rows, replies = read_all(
        lambda **a: list_communities_func(repo_root=str(root), **a),
        "communities", "max_results", 500,
    )
    assert [len(r["communities"]) for r in replies] == [25, 25, 10]
    assert len({r["name"] for r in rows}) == COUNT


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_a_bad_cursor_is_an_error_reply(repo, tool):
    root, _ = repo
    spec = TOOLS[tool]
    message = error_of(spec["call"](root), **{spec["limit_name"]: 5}, cursor="garbage")
    assert "start over" in message


def test_a_cursor_does_not_cross_queries(repo):
    root, _ = repo
    first = list_flows(repo_root=str(root), limit=5, sort_by="criticality")
    flows = TOOLS["flows"]["call"](root)
    assert "different query" in error_of(
        flows, limit=5, sort_by="depth", cursor=first["next_cursor"],
    )
    communities = TOOLS["communities"]["call"](root)
    assert "different query" in error_of(communities, max_results=5, cursor=first["next_cursor"])


def test_a_cursor_does_not_survive_a_graph_change(repo):
    root, store = repo
    first = get_hub_nodes_func(repo_root=str(root), top_n=5)
    store.upsert_node(NodeInfo(
        kind="Function", name="late", file_path=(root / "m.py").as_posix(),
        line_start=900, line_end=901, language="python",
    ))
    store.commit()
    message = error_of(
        lambda **a: get_hub_nodes_func(repo_root=str(root), **a),
        top_n=5, cursor=first["next_cursor"],
    )
    assert "graph changed" in message
    again = get_hub_nodes_func(repo_root=str(root), top_n=5)
    assert again["status"] == "ok" and "next_cursor" in again


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_ranking_ties_are_ordered_the_same_on_every_read(repo, tool):
    root, _ = repo
    spec = TOOLS[tool]
    call = spec["call"](root)
    reads = [
        [r[spec["id_key"]] for r in call(**{spec["limit_name"]: 200}, **spec["args"])[spec["key"]]]
        for _ in range(3)
    ]
    assert reads[0] == reads[1] == reads[2]


def test_hub_ties_break_by_qualified_name(repo):
    root, _ = repo
    hubs = get_hub_nodes_func(repo_root=str(root), top_n=200)["hub_nodes"]
    keys = [(-h["total_degree"], h["qualified_name"]) for h in hubs]
    assert keys == sorted(keys)


@pytest.mark.parametrize("name", ["list_flows_tool", "list_communities_tool", "get_hub_nodes_tool"])
def test_the_mcp_tools_take_a_cursor(name):
    parameter = inspect.signature(getattr(main_module, name)).parameters["cursor"]
    assert parameter.default is None
