"""``cross_repo_search_tool`` ranks exact names first; both cross-repo tools have a time budget."""

from __future__ import annotations

import pytest

from code_review_graph import constants
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.registry import Registry
from code_review_graph.repo_cache import _reset_shared_pool
from code_review_graph.tools import registry_tools
from code_review_graph.tools.registry_tools import (
    _match_tier,
    cross_repo_requests_func,
    cross_repo_search_func,
)

from .test_cross_repo_requests_paging import SERVER, client_file, make_repo


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    monkeypatch.delenv(constants.CROSS_REPO_BUDGET_ENV, raising=False)
    return Registry()


def named_repo(tmp_path, registry, alias, names, kind="Function"):
    root = (tmp_path / alias).resolve()
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True)
    with GraphStore(directory / "graph.db") as store:
        store.store_file_nodes_edges(
            str(root / "src.py"),
            [
                NodeInfo(
                    kind=kind, name=name, file_path=str(root / "src.py"),
                    line_start=i + 1, line_end=i + 1, language="python",
                )
                for i, name in enumerate(names)
            ],
            [],
        )
    registry.register(str(root), alias=alias)
    return root


def search(**args):
    args.setdefault("max_results", 100)
    reply = cross_repo_search_func(**args)
    assert reply["status"] == "ok", reply
    return reply


WEAK = ["getMetricsBatchV1", "getMetricsBatchV2", "getMetricsBatchV3"]


def test_exact_match_in_a_late_repo_ranks_first(tmp_path, registry):
    for alias in ("a", "b", "c"):
        named_repo(tmp_path, registry, alias, WEAK)
    named_repo(tmp_path, registry, "late", [*WEAK, "getMetricsBatch"])
    rows = search(query="getMetricsBatch")["results"]
    assert rows[0]["name"] == "getMetricsBatch"
    assert rows[0]["repo"] == "late"
    assert rows[0]["match"] == "exact"
    assert all(row["match"] != "exact" for row in rows[1:])


def test_same_exact_name_in_several_repos_precedes_fuzzy_hits(tmp_path, registry):
    named_repo(tmp_path, registry, "a", WEAK)
    named_repo(tmp_path, registry, "b", ["getMetricsBatch", *WEAK])
    named_repo(tmp_path, registry, "c", WEAK)
    named_repo(tmp_path, registry, "d", ["getMetricsBatch"])
    rows = search(query="getMetricsBatch")["results"]
    exact = [i for i, row in enumerate(rows) if row["match"] == "exact"]
    assert [rows[i]["repo"] for i in exact] == ["b", "d"]
    assert exact == [0, 1]


def test_limit_does_not_starve_an_exact_match(tmp_path, registry):
    # Many prefix hits outrank the exact name in the repo's own search; limit=1 cuts there.
    named_repo(tmp_path, registry, "a", [f"getMetricsBatch{i}" for i in range(30)]
               + ["getMetricsBatch"])
    rows = search(query="getMetricsBatch", limit=1)["results"]
    assert [row["name"] for row in rows] == ["getMetricsBatch"]
    assert rows[0]["match"] == "exact"


def test_kind_filter_applies_to_the_exact_lookup(tmp_path, registry):
    named_repo(tmp_path, registry, "a", ["Widget"], kind="Class")
    named_repo(tmp_path, registry, "b", ["Widget"], kind="Function")
    rows = search(query="Widget", kind="Class", limit=1)["results"]
    assert [(row["repo"], row["kind"]) for row in rows] == [("a", "Class")]


def test_paging_walk_returns_each_row_once_in_ranked_order(tmp_path, registry):
    named_repo(tmp_path, registry, "a", WEAK)
    named_repo(tmp_path, registry, "b", ["getMetricsBatch", *WEAK])
    named_repo(tmp_path, registry, "c", ["GETMETRICSBATCH", "batchGetMetricsBatch"])
    whole = search(query="getMetricsBatch")["results"]
    assert len(whole) > 4
    walked, cursor = [], None
    for _ in range(50):
        reply = search(query="getMetricsBatch", max_results=2, cursor=cursor)
        walked.extend(reply["results"])
        cursor = reply.get("next_cursor")
        if not cursor:
            break
    assert [(r["repo"], r["qualified_name"]) for r in walked] == [
        (r["repo"], r["qualified_name"]) for r in whole
    ]
    assert len({(r["repo"], r["qualified_name"]) for r in walked}) == len(walked)
    order = [registry_tools._TIER_ORDER[r["match"]] for r in walked]
    assert order == sorted(order)


@pytest.mark.parametrize(
    ("query", "name", "qualified", "tier"),
    [
        ("getMetricsBatch", "getMetricsBatch", "f.py::getMetricsBatch", "exact"),
        ("getMetricsBatch", "GETMETRICSBATCH", "f.py::GETMETRICSBATCH", "exact_ci"),
        ("Class.method", "method", "f.py::pkg.Class.method", "qualified"),
        ("Class.method", "method", "f.py::Class.method", "qualified"),
        ("Class.method", "method", "f.py::OtherClass.method", "fuzzy"),
        ("getMetrics", "getMetricsBatch", "f.py::getMetricsBatch", "prefix"),
        ("metrics batch", "getMetricsBatchV2", "f.py::getMetricsBatchV2", "tokens"),
        ("get_metrics", "fetchGetMetrics", "f.py::fetchGetMetrics", "tokens"),
        ("getMetricsBatch", "batchMetrics", "f.py::batchMetrics", "fuzzy"),
        ("getMetricsBatch", "unrelated", "f.py::unrelated", "fuzzy"),
    ],
)
def test_match_tiers(query, name, qualified, tier):
    assert _match_tier(query, {"name": name, "qualified_name": qualified}) == tier


def test_every_row_names_its_tier(tmp_path, registry):
    named_repo(tmp_path, registry, "a", ["getMetricsBatch", "getMetricsBatchV1"])
    rows = search(query="getMetricsBatch")["results"]
    assert {row["match"] for row in rows} <= set(registry_tools._MATCH_TIERS)
    assert {r["name"]: r["match"] for r in rows} == {
        "getMetricsBatch": "exact", "getMetricsBatchV1": "prefix",
    }


class Clock:
    """A clock that moves ``step`` seconds on every read."""

    def __init__(self, step):
        self.now = 0.0
        self.step = step

    def __call__(self):
        value = self.now
        self.now += self.step
        return value


@pytest.fixture
def five_repos(tmp_path, registry):
    for alias in "abcde":
        named_repo(tmp_path, registry, alias, ["getMetricsBatch", *WEAK])


def test_spent_budget_skips_and_reports_remaining_repos(five_repos, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == ["a", "b"]
    assert reply["skipped_repos"] == ["c", "d", "e"]
    assert reply["skipped_repos_total"] == 3
    assert reply["truncated"] is True
    assert "not searched" in reply["summary"]
    assert "CRG_CROSS_REPO_BUDGET_SECONDS" in reply["summary"]


def test_a_tiny_budget_still_searches_the_first_repo(five_repos, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "0.001")
    monkeypatch.setattr(registry_tools, "_now", Clock(1))
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == ["a"]
    assert reply["skipped_repos"] == ["b", "c", "d", "e"]
    assert reply["results"]


def test_budget_zero_is_unlimited(five_repos, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "0")
    monkeypatch.setattr(registry_tools, "_now", Clock(1e9))
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == list("abcde")
    assert reply["skipped_repos"] == []
    assert reply["skipped_repos_total"] == 0
    assert reply["truncated"] is False


def test_default_budget_does_not_skip_on_a_fast_call(five_repos):
    reply = search(query="getMetricsBatch")
    assert reply["skipped_repos_total"] == 0
    assert reply["truncated"] is False


def test_cursor_from_a_partial_answer_is_tied_to_the_same_skipped_repos(five_repos, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    first = search(query="getMetricsBatch", max_results=1)
    cursor = first["next_cursor"]
    again = search(query="getMetricsBatch", max_results=1, cursor=cursor)
    assert again["skipped_repos"] == first["skipped_repos"]
    assert again["offset"] == 1
    # The same cursor against a full (unbudgeted) answer is refused, not misread.
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "0")
    refused = cross_repo_search_func(query="getMetricsBatch", max_results=1, cursor=cursor)
    assert refused["status"] == "error"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 300.0), ("", 300.0), ("5", 5.0), ("2.5", 2.5), ("0", 0.0), (" 30 ", 30.0),
        ("abc", 300.0), ("-1", 300.0), ("nan", 300.0), ("inf", 300.0),
    ],
)
def test_budget_env_validation(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(constants.CROSS_REPO_BUDGET_ENV, raising=False)
    else:
        monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, raw)
    assert constants.cross_repo_budget_seconds() == expected


@pytest.fixture
def clients(tmp_path, registry):
    for name in ("ui1", "ui2", "ui3"):
        make_repo(tmp_path, registry, name, {"lib/a.ts": client_file(name, 3)})
    make_repo(tmp_path, registry, "api", {"server.js": SERVER})


def test_requests_tool_skips_client_repos_when_the_budget_is_spent(clients, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    reply = cross_repo_requests_func(origin="js", max_results=100)
    assert reply["status"] == "ok"
    assert reply["repos_searched"] == ["ui1", "ui2"]
    assert reply["skipped_repos"] == ["ui3", "api"]
    assert reply["skipped_repos_total"] == 2
    assert reply["truncated"] is True
    assert "not read" in reply["summary"]


def test_requests_tool_budget_zero_reads_everything(clients, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "0")
    monkeypatch.setattr(registry_tools, "_now", Clock(1e9))
    reply = cross_repo_requests_func(origin="js", max_results=100)
    assert reply["skipped_repos"] == []
    assert reply["skipped_repos_total"] == 0
    assert len(reply["repos_searched"]) == 4


def test_requests_cursor_on_a_partial_answer_is_refused_for_a_full_one(clients, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    first = cross_repo_requests_func(origin="js", limit=1, max_results=1)
    assert first["skipped_repos_total"] == 2
    cursor = first["next_cursor"]
    again = cross_repo_requests_func(origin="js", limit=1, max_results=1, cursor=cursor)
    assert again["status"] == "ok"
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "0")
    refused = cross_repo_requests_func(origin="js", limit=1, max_results=1, cursor=cursor)
    assert refused["status"] == "error"


def test_default_budget_is_five_minutes(registry):
    assert constants.cross_repo_budget_seconds() == 300.0


@pytest.fixture
def late_exact_repos(tmp_path, registry, monkeypatch):
    """Two searched repos with only fuzzy hits, then three the budget keeps out of the search."""
    named_repo(tmp_path, registry, "a", WEAK)
    named_repo(tmp_path, registry, "b", WEAK)
    named_repo(tmp_path, registry, "c", [*WEAK, "getMetricsBatch"])
    named_repo(tmp_path, registry, "d", ["getMetricsBatchV9"])
    named_repo(tmp_path, registry, "e", ["getMetricsBatch"])
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    monkeypatch.setattr(registry_tools, "_EXACT_PASS_SECONDS", 1e9)


def test_an_exact_name_in_a_repo_the_budget_skipped_is_still_found(late_exact_repos):
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == ["a", "b"]
    assert reply["skipped_repos"] == ["c", "d", "e"]
    assert reply["exact_lookup_repos_total"] == 3
    exact = [(row["repo"], row["name"], row["match"]) for row in reply["results"]
             if row["match"] == "exact"]
    assert exact == [("c", "getMetricsBatch", "exact"), ("e", "getMetricsBatch", "exact")]
    # The exact rows lead, ahead of the fuzzier hits of the repos that were searched.
    assert [row["match"] for row in reply["results"][:2]] == ["exact", "exact"]


def test_a_skipped_repo_contributes_only_its_exact_rows(late_exact_repos):
    reply = search(query="getMetricsBatch")
    from_skipped = [row for row in reply["results"] if row["repo"] in {"c", "d", "e"}]
    assert {row["repo"] for row in from_skipped} == {"c", "e"}
    assert {row["match"] for row in from_skipped} == {"exact"}


def test_the_exact_lookup_respects_the_kind_filter(late_exact_repos):
    reply = search(query="getMetricsBatch", kind="Class")
    assert [row for row in reply["results"] if row["repo"] in {"c", "d", "e"}] == []


def test_the_exact_pass_is_bounded_by_its_own_time_limit(late_exact_repos, monkeypatch):
    monkeypatch.setattr(registry_tools, "_EXACT_PASS_SECONDS", 15)
    reply = search(query="getMetricsBatch")
    assert reply["exact_lookup_repos_total"] == 1
    assert {row["repo"] for row in reply["results"] if row["match"] == "exact"} == {"c"}
    assert reply["skipped_repos"] == ["c", "d", "e"]


def test_no_exact_lookup_when_nothing_was_skipped(five_repos):
    reply = search(query="getMetricsBatch")
    assert reply["exact_lookup_repos_total"] == 0
    assert "exact names were still looked up" not in reply["summary"]


def test_the_summary_says_how_many_skipped_repos_got_the_exact_lookup(late_exact_repos):
    reply = search(query="getMetricsBatch")
    assert "exact names were still looked up in 3 of them" in reply["summary"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 4), ("1", 1), ("8", 8), (" 6 ", 6), ("32", 32), ("500", 32),
        ("0", 4), ("-3", 4), ("many", 4), ("2.5", 4), ("", 4),
    ],
)
def test_worker_count_setting(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv(constants.CROSS_REPO_WORKERS_ENV, raising=False)
    else:
        monkeypatch.setenv(constants.CROSS_REPO_WORKERS_ENV, raw)
    assert constants.cross_repo_workers() == expected


def _watch_concurrency(monkeypatch, delay=0.05):
    """Wrap the per-repo search to record how many run at once and in which threads."""
    import threading
    import time

    original = registry_tools._search_repo
    state = {"now": 0, "peak": 0, "threads": set()}
    lock = threading.Lock()

    def wrapped(store, query, kind, limit):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            state["threads"].add(threading.get_ident())
        time.sleep(delay)
        try:
            return original(store, query, kind, limit)
        finally:
            with lock:
                state["now"] -= 1

    monkeypatch.setattr(registry_tools, "_search_repo", wrapped)
    return state


def test_repos_are_searched_in_parallel_up_to_the_worker_count(tmp_path, registry, monkeypatch):
    for alias in "abcdefgh":
        named_repo(tmp_path, registry, alias, ["getMetricsBatch", *WEAK])
    monkeypatch.setenv(constants.CROSS_REPO_WORKERS_ENV, "4")
    _reset_shared_pool()  # the pool's size is read when it is first used
    state = _watch_concurrency(monkeypatch)
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == list("abcdefgh")
    assert 2 <= state["peak"] <= 4
    assert len(state["threads"]) > 1


def test_one_worker_searches_one_repo_at_a_time(tmp_path, registry, monkeypatch):
    for alias in "abcd":
        named_repo(tmp_path, registry, alias, ["getMetricsBatch", *WEAK])
    monkeypatch.setenv(constants.CROSS_REPO_WORKERS_ENV, "1")
    state = _watch_concurrency(monkeypatch)
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == list("abcd")
    assert state["peak"] == 1


def test_the_answer_does_not_depend_on_which_search_finishes_first(tmp_path, registry, monkeypatch):
    import time

    for alias in "abcde":
        named_repo(tmp_path, registry, alias, ["getMetricsBatch", *WEAK, f"{alias}Only"])
    original = registry_tools._search_repo
    lag = {"a": 0.25, "b": 0.2, "c": 0.1, "d": 0.05, "e": 0.0}  # the first repos finish last

    def slow_first(store, query, kind, limit):
        time.sleep(lag[str(store.db_path).split("/")[-3]])
        return original(store, query, kind, limit)

    monkeypatch.setenv(constants.CROSS_REPO_WORKERS_ENV, "1")
    sequential = search(query="getMetricsBatch")
    monkeypatch.setattr(registry_tools, "_search_repo", slow_first)
    monkeypatch.setenv(constants.CROSS_REPO_WORKERS_ENV, "5")
    parallel = search(query="getMetricsBatch")
    assert parallel["repos_searched"] == sequential["repos_searched"] == list("abcde")
    assert [(r["repo"], r["qualified_name"], r["match"]) for r in parallel["results"]] == [
        (r["repo"], r["qualified_name"], r["match"]) for r in sequential["results"]
    ]


@pytest.mark.parametrize("workers", ["1", "2", "8"])
def test_the_budget_skips_the_same_repos_for_any_worker_count(five_repos, monkeypatch, workers):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setenv(constants.CROSS_REPO_WORKERS_ENV, workers)
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == ["a", "b"]
    assert reply["skipped_repos"] == ["c", "d", "e"]


def test_one_failing_repo_does_not_stop_the_others(tmp_path, registry, monkeypatch):
    for alias in "abc":
        named_repo(tmp_path, registry, alias, ["getMetricsBatch"])
    original = registry_tools._search_repo

    def fails_for_b(store, query, kind, limit):
        if "/b/" in str(store.db_path):
            raise RuntimeError("corrupt graph")
        return original(store, query, kind, limit)

    monkeypatch.setattr(registry_tools, "_search_repo", fails_for_b)
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched"] == ["a", "c"]
    assert {row["repo"] for row in reply["results"]} == {"a", "c"}


def test_a_search_cut_by_the_per_repo_limit_says_more_exist(tmp_path, registry):
    many = [f"loadWidget{i:02d}" for i in range(30)]
    named_repo(tmp_path, registry, "wide", many)
    named_repo(tmp_path, registry, "narrow", ["loadWidgetOnly"])
    capped = search(query="loadWidget", limit=12, max_results=12)
    assert len(capped["results"]) <= 12
    # 12 rows is not the number of matches: the reply must not read as complete.
    assert capped["truncated"] is True
    assert capped["total_is_lower_bound"] is True
    assert capped["repos_capped"] == ["wide"] and capped["repos_capped_total"] == 1
    assert "more matches than limit=12" in capped["summary"]
    # Raising limit past the matches shows the whole list and drops the markers.
    whole = search(query="loadWidget", limit=50, max_results=100, repos=["narrow"])
    assert "total_is_lower_bound" not in whole and whole["repos_capped_total"] == 0
    assert whole["truncated"] is False


# --- loud budget skip, retry order, read-only opens -------------------------------------------


def test_a_budget_skip_is_loud_in_the_summary_and_the_reply(five_repos, monkeypatch):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    reply = search(query="getMetricsBatch")
    assert reply["summary"].startswith(
        "INCOMPLETE: searched 2 of 5 repos; 3 skipped by the time budget."
    )
    assert reply["truncated_reason"].startswith("searched 2 of 5 repos; 3 skipped")
    assert reply["repos_total"] == 5
    assert reply["skipped_repos"] == ["c", "d", "e"] and reply["truncated"] is True


def test_a_complete_search_has_no_incomplete_marker(five_repos):
    reply = search(query="getMetricsBatch")
    assert "INCOMPLETE" not in reply["summary"]
    assert "truncated_reason" not in reply


def test_a_repeat_after_a_budget_skip_starts_with_the_repos_it_did_not_reach(
    five_repos, monkeypatch,
):
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    first = search(query="getMetricsBatch")
    assert first["skipped_repos"] == ["c", "d", "e"]
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    second = search(query="getMetricsBatch")
    assert second["repos_searched"] == ["c", "d"]
    assert second["skipped_repos"] == ["a", "b", "e"]
    # Once everything was searched the memory is dropped: the order is the registry's again.
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "0")
    assert search(query="getMetricsBatch")["skipped_repos"] == []
    monkeypatch.setenv(constants.CROSS_REPO_BUDGET_ENV, "15")
    monkeypatch.setattr(registry_tools, "_now", Clock(10))
    assert search(query="getMetricsBatch")["skipped_repos"] == ["c", "d", "e"]


def test_the_search_opens_graphs_without_writing_unless_they_hold_vectors(
    five_repos, monkeypatch,
):
    opened: list[bool] = []
    original = registry_tools.GraphStore

    def spy(*args, **kwargs):
        opened.append(bool(kwargs.get("read_only")))
        return original(*args, **kwargs)

    monkeypatch.setattr(registry_tools, "GraphStore", spy)
    reply = search(query="getMetricsBatch")
    assert reply["repos_searched_total"] == 5
    assert opened and all(opened), "a graph without vectors must be opened read-only"


def test_a_graph_with_vectors_is_still_opened_writable_for_the_embedding_search(
    tmp_path, registry, monkeypatch,
):
    root = named_repo(tmp_path, registry, "vec", ["getMetricsBatch"])
    import sqlite3

    db = root / ".code-review-graph" / "graph.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS embeddings (qualified_name TEXT PRIMARY KEY, "
            "vector BLOB NOT NULL, text_hash TEXT NOT NULL, provider TEXT NOT NULL "
            "DEFAULT 'unknown')"
        )
        conn.execute("INSERT INTO embeddings VALUES ('q', x'00', 'h', 'p')")
    opened: list[bool] = []
    original = registry_tools.GraphStore

    def spy(*args, **kwargs):
        opened.append(bool(kwargs.get("read_only")))
        return original(*args, **kwargs)

    monkeypatch.setattr(registry_tools, "GraphStore", spy)
    assert search(query="getMetricsBatch")["repos_searched"] == ["vec"]
    assert opened == [True, False]


def test_search_tasks_run_on_the_shared_pool(tmp_path, registry, monkeypatch):
    import threading

    for alias in "abcd":
        named_repo(tmp_path, registry, alias, ["getMetricsBatch"])
    names: set[str] = set()
    original = registry_tools._search_repo

    def spy(store, query, kind, limit):
        names.add(threading.current_thread().name)
        return original(store, query, kind, limit)

    monkeypatch.setattr(registry_tools, "_search_repo", spy)
    search(query="getMetricsBatch")
    assert names and all(n.startswith("crg-xrepo") for n in names)
