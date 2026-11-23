"""Cursor paging for refactor_tool: rename edits, dead code and suggestions."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

import code_review_graph.refactor as refactor_module
import code_review_graph.tools.refactor_tools as refactor_tools_module
from code_review_graph import paging
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools import apply_refactor_func, refactor_func

COUNT = 40
CALLS = 40


@pytest.fixture
def repo():
    tmp = tempfile.mkdtemp()
    root = Path(tmp).resolve()
    (root / ".git").mkdir()
    (root / ".code-review-graph").mkdir()
    store = GraphStore(str(root / ".code-review-graph" / "graph.db"))

    # Dead code: COUNT functions nothing calls, spread over two files so
    # the order is not simply insertion order of one file.
    for i in range(COUNT):
        path = (root / f"dead{i % 2}.py").as_posix()
        store.upsert_node(NodeInfo(
            kind="Function", name=f"unused{i:03d}", file_path=path,
            line_start=i * 3 + 1, line_end=i * 3 + 2, language="python",
        ))

    # Rename: one real source file with a definition and CALLS call lines.
    lines = ["def target():", "    return 1", ""]
    lines += [f"x{i} = target()" for i in range(CALLS)]
    src = root / "lib.py"
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    lib = src.as_posix()
    store.upsert_node(NodeInfo(
        kind="Function", name="target", file_path=lib,
        line_start=1, line_end=2, language="python",
    ))
    store.upsert_node(NodeInfo(
        kind="Function", name="user", file_path=lib,
        line_start=4, line_end=4 + CALLS, language="python",
    ))
    for i in range(CALLS):
        store.upsert_edge(EdgeInfo(
            kind="CALLS", source=f"{lib}::user", target=f"{lib}::target",
            file_path=lib, line=4 + i,
        ))
    store.commit()
    yield root, store, src
    store.close()
    shutil.rmtree(tmp, ignore_errors=True)


def call(root, **args):
    return refactor_func(repo_root=str(root), **args)


RENAME = {"mode": "rename", "old_name": "target", "new_name": "renamed"}
DEAD = {"mode": "dead_code"}
SUGGEST = {"mode": "suggest"}
MODES = {
    "rename": {"args": RENAME, "key": "edits", "id": lambda r: (r["file"], r["line"])},
    "dead_code": {"args": DEAD, "key": "dead_code", "id": lambda r: r["qualified_name"]},
    "suggest": {"args": SUGGEST, "key": "suggestions", "id": lambda r: r["symbols"][0]},
}


def read_all(root, spec, size, **extra):
    rows, replies, cursor = [], [], None
    while True:
        reply = call(root, **spec["args"], max_results=size, cursor=cursor, **extra)
        assert reply["status"] == "ok", reply
        replies.append(reply)
        rows.extend(reply[spec["key"]])
        cursor = reply.get("next_cursor")
        if cursor is None:
            return rows, replies
        assert len(replies) < 1000


def error_of(root, **args) -> str:
    reply = call(root, **args)
    assert reply["status"] == "error", reply
    return reply["error"]


@pytest.fixture(autouse=True)
def _clean_pending():
    yield
    with refactor_module._refactor_lock:
        refactor_module._pending_refactors.clear()


@pytest.mark.parametrize("mode", sorted(MODES))
def test_first_page_is_the_old_output_plus_a_cursor(repo, mode):
    root, _, _ = repo
    spec = MODES[mode]
    whole = call(root, **spec["args"], max_results=150)
    assert whole["status"] == "ok" and not whole["truncated"]
    assert "next_cursor" not in whole and "offset" not in whole
    first = call(root, **spec["args"], max_results=7)
    assert first[spec["key"]] == whole[spec["key"]][:7]
    assert first["total"] == whole["total"] > 7
    assert first["truncated"] is True and "next_cursor" in first
    assert "offset" not in first
    assert f"showing 7 of {whole['total']}" in first["summary"]


@pytest.mark.parametrize("mode", sorted(MODES))
@pytest.mark.parametrize("size", [1, 3, 7, 16, 150])
def test_paging_reads_every_row_once_in_order(repo, mode, size):
    root, _, _ = repo
    spec = MODES[mode]
    whole = call(root, **spec["args"], max_results=150)[spec["key"]]
    rows, replies = read_all(root, spec, size)
    assert [spec["id"](r) for r in rows] == [spec["id"](r) for r in whole]
    assert len({spec["id"](r) for r in rows}) == len(rows)
    assert [r.get("offset", 0) for r in replies] == list(range(0, len(whole), size))
    assert [r["truncated"] for r in replies] == [True] * (len(replies) - 1) + [False]
    assert all(r["total"] == len(whole) for r in replies)


@pytest.mark.parametrize("mode", sorted(MODES))
def test_the_ceiling_bounds_a_page_not_the_list(repo, mode, monkeypatch):
    root, _, _ = repo
    monkeypatch.setattr(refactor_tools_module, "_MAX_REFACTOR_RESULTS", 9)
    spec = MODES[mode]
    rows, replies = read_all(root, spec, 500)
    assert len(rows) == replies[0]["total"] > 9
    assert all(len(r[spec["key"]]) <= 9 for r in replies)


def test_minimal_detail_pages_the_same_rows(repo):
    root, _, _ = repo
    spec = MODES["dead_code"]
    full, _ = read_all(root, spec, 6)
    mini, _ = read_all(root, spec, 6, detail_level="minimal")
    assert [r["name"] for r in mini] == [r["name"] for r in full]
    assert set(mini[0]) <= {"name", "kind", "relative_path", "line"}


@pytest.mark.parametrize("mode", sorted(MODES))
def test_malformed_cursor_is_refused(repo, mode):
    root, _, _ = repo
    message = error_of(root, **MODES[mode]["args"], max_results=5, cursor="garbage")
    assert "start over" in message


@pytest.mark.parametrize("mode", sorted(MODES))
def test_a_cursor_does_not_cross_modes(repo, mode):
    root, _, _ = repo
    for other in sorted(MODES):
        if other == mode:
            continue
        cursor = call(root, **MODES[other]["args"], max_results=5)["next_cursor"]
        message = error_of(root, **MODES[mode]["args"], max_results=5, cursor=cursor)
        assert "different query" in message


def test_a_cursor_does_not_cross_arguments(repo):
    root, _, _ = repo
    cursor = call(root, **DEAD, kind="Function", max_results=5)["next_cursor"]
    assert "different query" in error_of(
        root, **DEAD, kind="Class", max_results=5, cursor=cursor,
    )
    assert "different query" in error_of(
        root, **DEAD, kind="Function", file_pattern="dead0", max_results=5, cursor=cursor,
    )
    cursor = call(root, **RENAME, max_results=5)["next_cursor"]
    assert "different query" in error_of(
        root, mode="rename", old_name="target", new_name="other", max_results=5, cursor=cursor,
    )


def test_a_cursor_is_not_reusable_by_the_tool_that_pages_other_lists(repo):
    root, _, _ = repo
    foreign = paging.make_scope("list_flows_tool", sort_by="criticality")
    stamp = paging.graph_stamp(GraphStore(str(root / ".code-review-graph" / "graph.db")))
    cursor = paging.paginate(list(range(50)), 5, 5, scope=foreign, stamp=stamp).next_cursor
    assert "different query" in error_of(root, **DEAD, max_results=5, cursor=cursor)


def test_page_size_may_change_between_pages(repo):
    root, _, _ = repo
    first = call(root, **DEAD, max_results=5)
    second = call(root, **DEAD, max_results=11, cursor=first["next_cursor"])
    assert second["offset"] == 5 and len(second["dead_code"]) == 11


@pytest.mark.parametrize("mode", ["dead_code", "suggest"])
def test_a_cursor_does_not_survive_a_graph_change(repo, mode):
    root, store, _ = repo
    cursor = call(root, **MODES[mode]["args"], max_results=5)["next_cursor"]
    store.upsert_node(NodeInfo(
        kind="Function", name="late", file_path=(root / "dead0.py").as_posix(),
        line_start=900, line_end=901, language="python",
    ))
    store.commit()
    assert "graph changed" in error_of(
        root, **MODES[mode]["args"], max_results=5, cursor=cursor,
    )
    assert "next_cursor" in call(root, **MODES[mode]["args"], max_results=5)


def test_a_rename_cursor_does_not_survive_a_source_edit(repo):
    root, _, src = repo
    cursor = call(root, **RENAME, max_results=5)["next_cursor"]
    src.write_text(src.read_text(encoding="utf-8").replace("x3 = ", "x3 = 0 + "),
                   encoding="utf-8")
    message = error_of(root, **RENAME, max_results=5, cursor=cursor)
    assert "graph changed" in message


def test_a_hand_edited_cursor_cannot_leave_the_list(repo):
    root, _, _ = repo
    cursor = call(root, **DEAD, max_results=5)["next_cursor"]
    tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    reply = call(root, **DEAD, max_results=5, cursor=tampered)
    assert reply["status"] == "error"


def test_dead_code_and_suggestion_order_is_the_same_on_every_read(repo):
    root, _, _ = repo
    for mode in ("dead_code", "suggest", "rename"):
        spec = MODES[mode]
        reads = [
            [spec["id"](r) for r in call(root, **spec["args"], max_results=150)[spec["key"]]]
            for _ in range(3)
        ]
        assert reads[0] == reads[1] == reads[2]


def test_rename_edits_are_ordered_by_file_line_column(repo):
    root, _, _ = repo
    edits, _ = read_all(root, MODES["rename"], 6)
    keys = [(e["file"], e["line"], e.get("column", 0)) for e in edits]
    assert keys == sorted(keys)
    assert len(set(keys)) == len(keys)


# --- rename previews: expiry and applying ------------------------------------------------


def test_later_rename_pages_issue_no_refactor_id_and_register_nothing(repo):
    root, _, _ = repo
    first = call(root, **RENAME, max_results=5)
    assert len(first["refactor_id"]) == 8
    with refactor_module._refactor_lock:
        before = dict(refactor_module._pending_refactors)
    assert list(before) == [first["refactor_id"]]
    _, replies = read_all(root, MODES["rename"], 5)
    later = replies[1:]
    assert later and all("refactor_id" not in r for r in later)
    assert all("apply_refactor_tool(refactor_id" not in r["summary"] for r in later)
    with refactor_module._refactor_lock:
        after = dict(refactor_module._pending_refactors)
    # Only the explicit first-page call above and the one read_all started with a
    # fresh preview exist; no page after the first added one.
    assert set(after) - set(before) == {replies[0]["refactor_id"]}


def test_paging_does_not_refresh_an_expired_preview(repo):
    root, _, _ = repo
    first = call(root, **RENAME, max_results=5)
    rid = first["refactor_id"]
    with refactor_module._refactor_lock:
        refactor_module._pending_refactors[rid]["created_at"] -= (
            refactor_module.REFACTOR_EXPIRY_SECONDS + 5
        )
    second = call(root, **RENAME, max_results=5, cursor=first["next_cursor"])
    assert second["status"] == "ok" and "refactor_id" not in second
    applied = apply_refactor_func(rid, repo_root=str(root))
    assert applied["status"] == "error"
    assert "expired" in str(applied).lower() or "not found" in str(applied).lower()


def test_paging_never_applies_a_rename(repo):
    root, _, src = repo
    before = src.read_text(encoding="utf-8")
    first = call(root, **RENAME, max_results=5)
    _, _ = read_all(root, MODES["rename"], 5)
    assert src.read_text(encoding="utf-8") == before
    assert "renamed" not in before
    # The stored preview still holds every edit, and applying is a separate call.
    with refactor_module._refactor_lock:
        stored = refactor_module._pending_refactors[first["refactor_id"]]
        assert len(stored["edits"]) == first["total"] > 5
    applied = apply_refactor_func(first["refactor_id"], repo_root=str(root))
    assert applied["status"] == "ok"
    assert "renamed" in src.read_text(encoding="utf-8")


def test_a_cursor_is_not_a_refactor_id(repo):
    root, _, src = repo
    before = src.read_text(encoding="utf-8")
    cursor = call(root, **RENAME, max_results=5)["next_cursor"]
    assert apply_refactor_func(cursor, repo_root=str(root))["status"] == "error"
    assert src.read_text(encoding="utf-8") == before


def test_rename_not_found_with_a_cursor_is_not_found(repo):
    root, _, _ = repo
    cursor = call(root, **RENAME, max_results=5)["next_cursor"]
    reply = call(root, mode="rename", old_name="nothing_here", new_name="x",
                 max_results=5, cursor=cursor)
    assert reply["status"] == "not_found"
