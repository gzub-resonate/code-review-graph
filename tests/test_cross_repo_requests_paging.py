"""``cross_repo_requests_tool`` reads past its row ceilings with a cursor."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import code_review_graph.main as main_module
from code_review_graph import paging
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func

PER_REPO = 30


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_repo(tmp_path, registry, name, files):
    root = (tmp_path / name).resolve()
    for rel, text in files.items():
        write(root, rel, text)
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True, exist_ok=True)
    parser = CodeParser(root)
    with GraphStore(directory / "graph.db") as store:
        for rel in files:
            nodes, edges = parser.parse_file(root / rel)
            store.store_file_nodes_edges(str(root / rel), nodes, edges)
    registry.register(str(root), alias=name)
    return root


def client_file(prefix: str, count: int = PER_REPO) -> str:
    lines = ["import axios from 'axios';"]
    lines += [f"export const c{i} = () => axios.get('/{prefix}/n{i:03d}');" for i in range(count)]
    return "\n".join(lines) + "\n"


SERVER = (
    "const express = require('express');\nconst app = express();\n"
    + "".join(f"app.get('/ui1/n{i:03d}', (req, res) => res.end());\n" for i in range(0, 30, 3))
)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


@pytest.fixture
def repos(tmp_path, registry):
    make_repo(tmp_path, registry, "ui1", {"lib/a.ts": client_file("ui1")})
    make_repo(tmp_path, registry, "ui2", {"lib/a.ts": client_file("ui2")})
    make_repo(tmp_path, registry, "api", {"server.js": SERVER})
    return tmp_path


def call(**args):
    args.setdefault("origin", "js")
    return cross_repo_requests_func(repos=["ui1", "ui2"], **args)


def read_all(**args):
    rows, replies, cursor = [], [], None
    while True:
        reply = call(cursor=cursor, **args)
        assert reply["status"] == "ok", reply
        replies.append(reply)
        rows.extend(reply["results"])
        cursor = reply.get("next_cursor")
        if cursor is None:
            return rows, replies
        assert len(replies) < 200


def ident(row):
    return (row["client_repo"], row["client_line"])


def test_the_first_page_is_what_the_row_caps_always_gave(repos):
    reply = call()
    assert reply["total"] == 2 * PER_REPO
    assert [r["client_repo"] for r in reply["results"]].count("ui1") == 20
    assert [r["client_repo"] for r in reply["results"]].count("ui2") == 20
    assert reply["truncated"] is True and "next_cursor" in reply and "offset" not in reply
    assert ", showing 40 of 60" in reply["summary"]


def test_paging_reads_every_call_once_in_order(repos):
    whole = call(limit=100, max_results=100)
    assert whole["truncated"] is False and "next_cursor" not in whole
    expected = [ident(r) for r in whole["results"]]
    assert len(expected) == 2 * PER_REPO

    for limit, max_results in [(20, 50), (7, 10), (3, 100), (100, 11), (1, 1)]:
        rows, replies = read_all(limit=limit, max_results=max_results)
        assert sorted(ident(r) for r in rows) == sorted(expected), (limit, max_results)
        assert len({ident(r) for r in rows}) == len(rows)
        assert all(r["total"] == 2 * PER_REPO and r["counts"] == whole["counts"] for r in replies)
        assert all(len(r["results"]) <= max_results for r in replies)
        assert [r["truncated"] for r in replies] == [True] * (len(replies) - 1) + [False]


def test_each_page_takes_a_fair_share_from_every_repo(repos):
    first = call(limit=5, max_results=100)
    assert [r["client_repo"] for r in first["results"]] == ["ui1"] * 5 + ["ui2"] * 5
    second = call(limit=5, max_results=100, cursor=first["next_cursor"])
    assert [r["client_line"] for r in second["results"] if r["client_repo"] == "ui1"] == [
        r["client_line"] + 5 for r in first["results"] if r["client_repo"] == "ui1"
    ]
    assert second["offset"] == 10


def test_a_repo_cut_by_the_overall_cap_continues_where_it_stopped(repos):
    first = call(limit=20, max_results=25)
    assert [r["client_repo"] for r in first["results"]] == ["ui1"] * 20 + ["ui2"] * 5
    second = call(limit=20, max_results=25, cursor=first["next_cursor"])
    assert [r["client_repo"] for r in second["results"]] == ["ui1"] * 10 + ["ui2"] * 15
    lines = [r["client_line"] for r in second["results"] if r["client_repo"] == "ui2"]
    assert lines[0] > max(r["client_line"] for r in first["results"] if r["client_repo"] == "ui2")


def test_matched_rows_are_reachable_on_a_later_page(repos):
    rows, _ = read_all(limit=4, max_results=6)
    matched = [r for r in rows if r["status"] == "matched"]
    assert len(matched) == 10 and {r["client_repo"] for r in matched} == {"ui1"}


def test_a_cursor_is_refused_when_the_query_changed(repos):
    first = call(limit=5, max_results=5)
    other = call(limit=5, max_results=5, status="matched", cursor=first["next_cursor"])
    assert other["status"] == "error" and "different query" in other["error"]
    other = cross_repo_requests_func(
        repos=["ui1"], origin="js", limit=5, max_results=5, cursor=first["next_cursor"],
    )
    assert other["status"] == "error" and "different query" in other["error"]


def test_a_cursor_is_refused_when_the_calls_changed(repos, tmp_path):
    first = call(limit=5, max_results=5)
    registry = Registry()
    make_repo(tmp_path, registry, "ui1", {"lib/a.ts": client_file("ui1", PER_REPO + 1)})
    reply = call(limit=5, max_results=5, cursor=first["next_cursor"])
    assert reply["status"] == "error" and "graph changed" in reply["error"]
    assert call(limit=5, max_results=5)["status"] == "ok"


def test_a_bad_cursor_is_an_error_reply(repos):
    reply = call(cursor="garbage")
    assert reply["status"] == "error" and "start over" in reply["error"]
    assert issubclass(paging.CursorError, ValueError)


def test_a_reply_that_fits_carries_no_paging_keys(repos):
    reply = call(limit=100, max_results=100)
    assert not {"next_cursor", "offset"} & set(reply)


def test_the_mcp_tool_takes_a_cursor():
    parameter = inspect.signature(main_module.cross_repo_requests_tool).parameters["cursor"]
    assert parameter.default is None
