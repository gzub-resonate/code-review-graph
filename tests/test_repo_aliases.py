"""Optional ``repo-aliases.toml``: a second name for a registered repository."""

import logging
from pathlib import Path

import pytest

from code_review_graph import repo_aliases
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.registry import Registry, resolve_repo
from code_review_graph.repo_aliases import (
    MAX_REPO_ALIASES,
    REPO_ALIASES_FILENAME,
    RepoNameResolver,
    load_repo_aliases,
)
from code_review_graph.tools.registry_tools import _select_repos, cross_repo_search_func


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("CRG_HOME", str(path))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return path


def write_aliases(home: Path, text: str) -> Path:
    path = home / REPO_ALIASES_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


def indexed_repo(root, registry, *, alias=None):
    root = root.resolve()
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True)
    with GraphStore(directory / "graph.db") as store:
        store.store_file_nodes_edges(
            str(root / "service.py"),
            [
                NodeInfo(
                    kind="Function",
                    name="lookup_001",
                    file_path=str(root / "service.py"),
                    line_start=1,
                    line_end=1,
                    language="python",
                )
            ],
            [],
        )
    registry.register(str(root), alias=alias)
    return root


@pytest.fixture
def entries(tmp_path):
    return [
        {"path": str(tmp_path / "billing-service"), "alias": "billing"},
        {"path": str(tmp_path / "orders")},
    ]


# --- loader ---------------------------------------------------------------


def test_absent_file_is_a_no_op(home, caplog):
    with caplog.at_level(logging.WARNING):
        assert load_repo_aliases() == {}
    assert caplog.records == []


def test_valid_file_loads_in_order(home):
    write_aliases(home, '[aliases]\nold-billing = "billing"\n"v1.orders" = "orders"\n')
    assert list(load_repo_aliases().items()) == [
        ("old-billing", "billing"),
        ("v1.orders", "orders"),
    ]


def test_crg_home_override_is_honoured(tmp_path, monkeypatch):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    write_aliases(first, '[aliases]\nx = "billing"\n')
    monkeypatch.setenv("CRG_HOME", str(first))
    assert load_repo_aliases() == {"x": "billing"}
    monkeypatch.setenv("CRG_HOME", str(second))
    assert load_repo_aliases() == {}
    assert load_repo_aliases(first) == {"x": "billing"}


def test_malformed_toml_is_ignored_with_one_warning(home, caplog):
    write_aliases(home, "[aliases\nx = ")
    with caplog.at_level(logging.WARNING):
        assert load_repo_aliases() == {}
    assert len(caplog.records) == 1


def test_duplicate_keys_are_handled_like_malformed_toml(home, caplog):
    write_aliases(home, '[aliases]\nx = "billing"\nx = "orders"\n')
    with caplog.at_level(logging.WARNING):
        assert load_repo_aliases() == {}
    assert len(caplog.records) == 1


def test_aliases_not_a_table_is_ignored(home, caplog):
    write_aliases(home, 'aliases = "billing"\n')
    with caplog.at_level(logging.WARNING):
        assert load_repo_aliases() == {}
    assert len(caplog.records) == 1


@pytest.mark.parametrize("value", ["1", "true", "[]", '""', '"   "', "{ a = 1 }"])
def test_wrong_value_types_are_skipped(home, caplog, value):
    write_aliases(home, f'[aliases]\nbad = {value}\ngood = "billing"\n')
    with caplog.at_level(logging.WARNING):
        assert load_repo_aliases() == {"good": "billing"}
    assert len(caplog.records) == 1


@pytest.mark.parametrize("name", ['"has space"', '"a/b"', '"café"', '"' + "x" * 101 + '"', '""'])
def test_invalid_names_are_skipped(home, caplog, name):
    write_aliases(home, f'[aliases]\n{name} = "billing"\ngood = "billing"\n')
    with caplog.at_level(logging.WARNING):
        assert load_repo_aliases() == {"good": "billing"}
    assert len(caplog.records) == 1


def test_name_of_maximum_length_is_accepted(home):
    name = "x" * 100
    write_aliases(home, f'[aliases]\n{name} = "billing"\n')
    assert load_repo_aliases() == {name: "billing"}


def test_cap_is_enforced(home, caplog):
    lines = "\n".join(f'a{i} = "billing"' for i in range(MAX_REPO_ALIASES + 5))
    write_aliases(home, f"[aliases]\n{lines}\n")
    with caplog.at_level(logging.WARNING):
        result = load_repo_aliases()
    assert len(result) == MAX_REPO_ALIASES
    assert "a0" in result and f"a{MAX_REPO_ALIASES}" not in result
    assert len(caplog.records) == 1


def test_names_are_case_sensitive(home):
    write_aliases(home, '[aliases]\nOldBilling = "billing"\n')
    assert load_repo_aliases() == {"OldBilling": "billing"}


def test_toml_parser_missing_is_a_no_op(home, monkeypatch, caplog):
    write_aliases(home, '[aliases]\nx = "billing"\n')
    monkeypatch.setattr(repo_aliases, "tomllib", None)
    with caplog.at_level(logging.WARNING):
        assert load_repo_aliases() == {}
    assert len(caplog.records) == 1


# --- resolution -----------------------------------------------------------


def test_file_name_resolves_to_alias_target(home, entries):
    write_aliases(home, '[aliases]\nold-billing = "billing"\n')
    assert RepoNameResolver(entries).lookup("old-billing") == [0]


def test_file_name_resolves_to_folder_target(home, entries):
    write_aliases(home, '[aliases]\nold-billing = "billing-service"\nold-orders = "orders"\n')
    resolver = RepoNameResolver(entries)
    assert resolver.lookup("old-billing") == [0]
    assert resolver.lookup("old-orders") == [1]


def test_real_alias_beats_file(home, entries):
    write_aliases(home, '[aliases]\nbilling = "orders"\n')
    assert RepoNameResolver(entries).lookup("billing") == [0]


def test_folder_name_beats_file(home, entries):
    write_aliases(home, '[aliases]\norders = "billing"\n')
    assert RepoNameResolver(entries).lookup("orders") == [1]


def test_unresolvable_target_is_skipped_with_warning(home, entries, caplog):
    write_aliases(home, '[aliases]\nold = "missing"\n')
    with caplog.at_level(logging.WARNING):
        assert RepoNameResolver(entries).lookup("old") == []
    assert len(caplog.records) == 1


def test_no_chaining(home, entries, caplog):
    write_aliases(home, '[aliases]\nfirst = "second"\nsecond = "billing"\n')
    with caplog.at_level(logging.WARNING):
        resolver = RepoNameResolver(entries)
        assert resolver.lookup("first") == []
        assert resolver.lookup("second") == [0]


def test_cycle_does_not_resolve_or_loop(home, entries):
    write_aliases(home, '[aliases]\nping = "pong"\npong = "ping"\n')
    resolver = RepoNameResolver(entries)
    assert resolver.lookup("ping") == []
    assert resolver.lookup("pong") == []


def test_resolution_is_case_sensitive(home, entries):
    write_aliases(home, '[aliases]\nOldBilling = "billing"\n')
    resolver = RepoNameResolver(entries)
    assert resolver.lookup("OldBilling") == [0]
    assert resolver.lookup("oldbilling") == []


def test_file_is_read_once_per_resolver_and_only_when_needed(home, entries, monkeypatch):
    calls = []
    real = repo_aliases.load_repo_aliases
    monkeypatch.setattr(
        repo_aliases, "load_repo_aliases", lambda h=None: calls.append(h) or real(h)
    )
    resolver = RepoNameResolver(entries)
    resolver.lookup("billing")
    resolver.lookup("orders")
    assert calls == []
    resolver.lookup("x")
    resolver.lookup("y")
    assert len(calls) == 1


def test_select_repos_uses_file_and_keeps_semantics(home, entries):
    write_aliases(home, '[aliases]\nold-billing = "billing"\nold-gone = "missing"\n')
    selected, unknown, ambiguous = _select_repos(
        entries, ["old-billing", "billing", "old-gone", "nope"]
    )
    assert selected == [entries[0]]
    assert unknown == ["old-gone", "nope"]
    assert ambiguous == []


def test_resolve_repo_falls_back_to_file(home, tmp_path):
    registry = Registry()
    repo = tmp_path / "billing-service"
    (repo / ".git").mkdir(parents=True)
    registry.register(str(repo), alias="billing")
    write_aliases(home, '[aliases]\nold-billing = "billing"\n')
    assert resolve_repo(registry, "old-billing") == str(repo.resolve())
    assert resolve_repo(registry, "unlisted-name") is None


# --- end to end -----------------------------------------------------------


def test_cross_repo_search_accepts_file_name_end_to_end(home, tmp_path):
    registry = Registry()
    chosen = indexed_repo(tmp_path / "billing-service", registry, alias="billing")
    indexed_repo(tmp_path / "orders", registry)
    write_aliases(home, '[aliases]\nold-billing = "billing"\n')
    result = cross_repo_search_func("lookup", repos=["old-billing"], kind="Function")
    assert result["status"] == "ok"
    assert result["repos_searched"] == ["billing"]
    assert {row["repo_path"] for row in result["results"]} == {str(chosen)}
    assert result["unknown"] == result["ambiguous"] == []


def test_cross_repo_search_reports_unresolved_file_name_as_unknown(home, tmp_path):
    registry = Registry()
    indexed_repo(tmp_path / "orders", registry)
    write_aliases(home, '[aliases]\nold-billing = "billing"\n')
    result = cross_repo_search_func("lookup", repos=["old-billing"], kind="Function")
    assert result["unknown"] == ["old-billing"]
    assert result["results"] == []


def test_register_and_unregister_never_touch_the_file(home, tmp_path):
    path = write_aliases(home, '[aliases]\nold-billing = "billing"\n')
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    registry = Registry()
    repo = tmp_path / "billing-service"
    (repo / ".git").mkdir(parents=True)
    registry.register(str(repo), alias="billing")
    registry.register(str(repo), alias="renamed")
    assert registry.unregister("renamed")
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
