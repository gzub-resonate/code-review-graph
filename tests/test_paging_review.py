"""Cursor paging for the review tools: get_review_context and detect_changes."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import code_review_graph.main as main_module
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.tools import detect_changes_func, get_review_context

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

N_FILES = 12
FUNCS_PER_FILE = 3
N_FUNCS = N_FILES * FUNCS_PER_FILE


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, timeout=30)


def _module(i: int, version: int) -> str:
    # Every module has the same shape, so the risk of each changed file ties.
    return "".join(
        f"def fn_{i:02d}_{j}(x):\n    return x + {version}\n\n\n" for j in range(FUNCS_PER_FILE)
    )


def _names() -> list[str]:
    return [f"mod{i:02d}.py" for i in range(N_FILES)]


@pytest.fixture(scope="module")
def repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    import os

    root = tmp_path_factory.mktemp("paging-review-repo")
    os.environ["CRG_SERIAL_PARSE"] = "1"
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text(".code-review-graph/\n", encoding="utf-8")
    for i, name in enumerate(_names()):
        (root / name).write_text(_module(i, 1), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    for i, name in enumerate(_names()):
        (root / name).write_text(_module(i, 2), encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        full_build(root, store)
    return root


def _context(repo: Path, **kw):
    kw.setdefault("repo_root", str(repo))
    kw.setdefault("include_source", True)
    return get_review_context(**kw)


def _detect(repo: Path, **kw):
    kw.setdefault("repo_root", str(repo))
    return detect_changes_func(**kw)


def _walk(call, key, size_arg, size, **kw):
    """Read every page; return (pages, rows)."""
    pages, rows, cursor = [], [], None
    while True:
        out = call(cursor=cursor, **{size_arg: size}, **kw)
        assert out["status"] == "ok", out
        pages.append(out)
        body = out["context"] if "context" in out else out
        rows.extend(body[key])
        cursor = body.get("next_cursor")
        if cursor is None:
            return pages, rows
        assert len(pages) < 1000


def _forge(cursor: str, **changes) -> str:
    raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
    payload = json.loads(raw)
    payload.update(changes)
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# get_review_context
# ---------------------------------------------------------------------------


def test_review_context_first_page_is_unchanged_plus_cursor(repo):
    whole = _context(repo, max_files=100)
    first = _context(repo, max_files=5)
    assert "next_cursor" not in whole["context"] and "offset" not in whole["context"]
    assert whole["context"]["truncated"] is False
    ctx = first["context"]
    assert ctx["changed_files"] == whole["context"]["changed_files"][:5]
    assert ctx["changed_files_total"] == N_FILES
    assert ctx["truncated"] is True and "next_cursor" in ctx and "offset" not in ctx
    assert ctx["graph"] == whole["context"]["graph"]
    assert ctx["impacted_files"] == whole["context"]["impacted_files"]
    assert ctx["review_guidance"] == whole["context"]["review_guidance"]
    assert ", showing 5 of 12" in first["summary"]


@pytest.mark.parametrize("size", [1, 3, 5, 7, 12, 50])
def test_review_context_pages_read_every_file_once_in_order(repo, size):
    whole = _context(repo, max_files=100)["context"]["changed_files"]
    pages, rows = _walk(lambda **kw: _context(repo, **kw), "changed_files", "max_files", size)
    assert rows == whole and len(set(rows)) == N_FILES
    for n, page in enumerate(pages):
        ctx = page["context"]
        assert ctx["truncated"] is (n < len(pages) - 1)
        assert ("next_cursor" in ctx) is ctx["truncated"]
        assert ctx.get("offset", 0) == n * min(size, N_FILES)
        assert set(ctx["file_risk"]) == set(ctx["changed_files"])
        assert set(ctx["source_snippets"]) <= set(ctx["changed_files"])
        assert ("graph" in ctx) is (n == 0)


def test_review_context_ties_rank_in_the_given_order(repo):
    names = _names()
    shuffled = names[5:] + names[:5]
    full = _context(repo, changed_files=shuffled, max_files=100)["context"]["changed_files"]
    pages, rows = _walk(
        lambda **kw: _context(repo, changed_files=shuffled, **kw), "changed_files", "max_files", 4,
    )
    assert rows == full
    # Equal risk is ordered by the position in the caller's list, not by chance.
    risks = _context(repo, changed_files=shuffled, max_files=100)["context"]["file_risk"]
    tied = [r for r in full if risks[r] == risks[full[0]]]
    assert tied == [r for r in shuffled if r in tied]


def test_review_context_refuses_foreign_stale_and_tampered_cursors(repo):
    cursor = _context(repo, max_files=5)["context"]["next_cursor"]
    names = _names()
    for kw in (
        {"changed_files": names[:-1]},   # a different changed-file set
        {"changed_files": names[::-1]},  # the same set in another order
        {"max_depth": 3},
        {"detail_level": "minimal"},
    ):
        with pytest.raises(ValueError):
            _context(repo, cursor=cursor, max_files=5, **kw)
    explicit = _context(repo, changed_files=names, max_files=5)["context"]["next_cursor"]
    with pytest.raises(ValueError, match="different query"):
        _context(repo, changed_files=names, cursor=explicit, max_files=5, base="HEAD")
    with pytest.raises(ValueError, match="not valid"):
        _context(repo, cursor="not-a-cursor", max_files=5)
    with pytest.raises(ValueError, match="different query"):
        _context(repo, cursor=_forge(cursor, s="0" * 16), max_files=5)
    with pytest.raises(ValueError, match="graph changed"):
        _context(repo, cursor=_forge(cursor, g="0" * 16), max_files=5)
    with pytest.raises(ValueError, match="past the end"):
        _context(repo, cursor=_forge(cursor, o=999), max_files=5)


def test_review_context_cursor_is_refused_when_the_file_set_differs(repo):
    names = _names()
    cursor = _context(repo, changed_files=names, max_files=5)["context"]["next_cursor"]
    with pytest.raises(ValueError):
        _context(repo, changed_files=names[:-1], max_files=5, cursor=cursor)


def test_review_context_page_size_may_change_between_pages(repo):
    first = _context(repo, max_files=5)["context"]
    second = _context(repo, max_files=7, cursor=first["next_cursor"])["context"]
    whole = _context(repo, max_files=100)["context"]["changed_files"]
    assert first["changed_files"] + second["changed_files"] == whole[:12]
    assert second["offset"] == 5 and "next_cursor" not in second


# ---------------------------------------------------------------------------
# detect_changes
# ---------------------------------------------------------------------------


def test_detect_changes_first_page_is_unchanged_plus_cursor(repo):
    whole = _detect(repo, max_results=100)
    first = _detect(repo, max_results=10)
    assert whole["changed_functions_total"] == N_FUNCS
    assert "next_cursor" not in whole and "offset" not in whole
    assert first["changed_functions"] == whole["changed_functions"][:10]
    assert first["truncated"] is True and "next_cursor" in first and "offset" not in first
    for key in ("changed_functions_total", "test_gaps_total", "affected_flows_total",
                "risk_score", "review_priorities"):
        assert first[key] == whole[key]
    assert first["test_gaps"] == whole["test_gaps"][:10]


@pytest.mark.parametrize("size", [1, 5, 10, 36, 100])
def test_detect_changes_pages_read_every_function_once_in_order(repo, size):
    whole = _detect(repo, max_results=100)["changed_functions"]
    pages, rows = _walk(lambda **kw: _detect(repo, **kw), "changed_functions", "max_results", size)
    assert rows == whole and len({r["qualified_name"] for r in rows}) == N_FUNCS
    for n, page in enumerate(pages):
        assert page["truncated"] is (n < len(pages) - 1)
        assert ("next_cursor" in page) is page["truncated"]
        assert page.get("offset", 0) == n * min(size, N_FUNCS)
        assert page["changed_functions_total"] == N_FUNCS
        if n:
            assert "test_gaps" not in page and "affected_flows" not in page


def test_detect_changes_source_budget_follows_the_page(repo):
    first = _detect(repo, max_results=10, include_source=True)
    later = _detect(repo, max_results=10, include_source=True, cursor=first["next_cursor"])
    assert all("source" in f for f in first["changed_functions"])
    assert all("source" in f for f in later["changed_functions"])


def test_detect_changes_refuses_foreign_stale_and_tampered_cursors(repo):
    cursor = _detect(repo, max_results=10)["next_cursor"]
    names = _names()
    for kw in (
        {"changed_files": names[:-1]},
        {"changed_files": names[::-1]},
        {"max_depth": 3},
    ):
        out = _detect(repo, max_results=10, cursor=cursor, **kw)
        assert out["status"] == "error" and "cursor" in out["error"], kw
    names_cursor = _detect(repo, changed_files=names, max_results=10)["next_cursor"]
    out = _detect(repo, changed_files=names, max_results=10, cursor=names_cursor, base="HEAD")
    assert out["status"] == "error" and "different query" in out["error"]
    out = _detect(repo, max_results=10, cursor="not-a-cursor")
    assert out["status"] == "error" and "not valid" in out["error"]
    out = _detect(repo, max_results=10, cursor=_forge(cursor, g="0" * 16))
    assert out["status"] == "error" and "graph changed" in out["error"]
    out = _detect(repo, max_results=10, cursor=_forge(cursor, s="0" * 16))
    assert out["status"] == "error" and "different query" in out["error"]
    out = _detect(repo, max_results=10, cursor=_forge(cursor, o=9999))
    assert out["status"] == "error" and "past the end" in out["error"]
    out = _detect(repo, max_results=10, cursor=cursor, detail_level="minimal")
    assert out["status"] == "error" and "no pages" in out["error"]


def test_detect_changes_cursor_is_refused_when_the_file_set_differs(repo):
    names = _names()
    cursor = _detect(repo, changed_files=names, max_results=10)["next_cursor"]
    out = _detect(repo, changed_files=names[:6], max_results=10, cursor=cursor)
    assert out["status"] == "error"
    again = _detect(repo, changed_files=names, max_results=10, cursor=cursor)
    assert again["status"] == "ok" and again["offset"] == 10


def test_detect_changes_order_is_deterministic(repo):
    a = _detect(repo, max_results=100)["changed_functions"]
    b = _detect(repo, max_results=100)["changed_functions"]
    assert [f["qualified_name"] for f in a] == [f["qualified_name"] for f in b]


# ---------------------------------------------------------------------------
# Wrappers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["get_review_context_tool", "detect_changes_tool"])
def test_wrappers_expose_cursor(name):
    import inspect

    tool = getattr(main_module, name)
    fn = getattr(tool, "fn", tool)
    sig = inspect.signature(fn)
    assert "cursor" in sig.parameters and sig.parameters["cursor"].default is None
    assert "next_cursor" in (fn.__doc__ or "")
