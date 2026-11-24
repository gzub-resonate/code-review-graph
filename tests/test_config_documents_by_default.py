"""Config documents stay out of community lists and hub rankings unless asked for.

A repository of data files (state machine inputs, workflow YAML) has hundreds of
JSON and YAML document nodes. They made most of the community list, the top of the
hub ranking (the ``$`` root of a file touches every key beneath it), and their
slices were named by a counter. Each is now left out by default behind an
``include_config`` flag, and a slice is named after what it holds.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from code_review_graph import communities as cm
from code_review_graph.analysis import find_hub_nodes
from code_review_graph.graph import GraphStore
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.tools.analysis_tools import get_hub_nodes_func
from code_review_graph.tools.community_tools import (
    get_architecture_overview_func,
    list_communities_func,
)


def _add(store: GraphStore, kind: str, name: str, path: str, line: int, lang: str) -> str:
    store.upsert_node(NodeInfo(
        kind=kind, name=name, file_path=path, line_start=line, line_end=line, language=lang,
    ))
    return f"{path}::{name}"


def _chain(store: GraphStore, path: str, names: list[str]) -> None:
    previous = None
    for qn in names:
        if previous:
            store.upsert_edge(EdgeInfo(
                kind="CONTAINS", source=previous, target=qn, file_path=path, line=1,
            ))
        previous = qn


def _repo(tmp_path: Path, files: dict[str, int]) -> Path:
    """A repository with two Python modules calling each other and config files.

    *files* is ``{config path: rows}``; each row is a JsonPath node chained to the last.
    """
    (tmp_path / ".git").mkdir()
    data = tmp_path / ".code-review-graph"
    data.mkdir()
    store = GraphStore(str(data / "graph.db"))
    for module in ("a", "b"):
        path = f"src/{module}.py"
        funcs = [_add(store, "Function", f"{module}{i}", path, i + 1, "python") for i in range(4)]
        _chain(store, path, funcs)
    store.upsert_edge(EdgeInfo(
        kind="CALLS", source="src/a.py::a0", target="src/b.py::b0", file_path="src/a.py", line=1,
    ))
    for path, rows in files.items():
        names = [_add(store, "JsonPath", f"$.k{i}", path, i + 1, "json") for i in range(rows)]
        _chain(store, path, names)
        # A document root that everything beneath it hangs from.
        root = _add(store, "JsonPath", "$", path, 0, "json")
        for qn in names:
            store.upsert_edge(EdgeInfo(
                kind="CONTAINS", source=root, target=qn, file_path=path, line=1,
            ))
    store.commit()
    cm.detect_communities(store, min_size=2)
    cm.store_communities(store, cm.detect_communities(store, min_size=2))
    store.commit()
    store.close()
    return tmp_path


class TestSliceNames:
    def _slices(self, layout: dict[str, int]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            store = GraphStore(str(Path(tmp) / "g.db"))
            try:
                for path, rows in layout.items():
                    previous = None
                    for i in range(rows):
                        store.upsert_node(NodeInfo(
                            kind="JsonPath", name=f"$.k{i}", file_path=path,
                            line_start=i + 1, line_end=i + 1, language="json",
                        ))
                        qn = f"{path}::$.k{i}"
                        if previous:
                            store.upsert_edge(EdgeInfo(
                                kind="CONTAINS", source=previous, target=qn,
                                file_path=path, line=i + 1,
                            ))
                        previous = qn
                store.commit()
                return sorted(c["name"] for c in cm.detect_communities(store, min_size=2))
            finally:
                store.close()

    def test_a_slice_inside_one_file_is_named_by_its_first_and_last_key(self):
        names = self._slices({"data/votes.json": 1000})
        assert names[0] == "data-json-votes-k0-to-k199"
        assert "data-json-votes-k800-to-k999" in names
        assert not any(n.rsplit("-", 1)[-1].isdigit() and "-to-" not in n for n in names)

    def test_a_slice_across_files_is_named_by_its_first_and_last_file(self):
        names = self._slices({f"svc/{stem}.json": 100 for stem in ("alpha", "beta", "gamma",
                                                                    "delta", "omega")})
        assert all("-to-" in n or n.startswith("svc-json-") for n in names)
        assert any(n.startswith("svc-json-alpha-to-") for n in names)
        assert any(n.endswith("-to-omega") for n in names)

    def test_a_group_that_fits_in_one_community_keeps_its_plain_name(self):
        assert self._slices({"ecspresso/taskdefs/taskdef-dev.json": 6}) == ["taskdefs-json"]


class TestConfigCommunitiesAreHiddenByDefault:
    @pytest.fixture()
    def repo(self, tmp_path):
        return _repo(tmp_path, {"data/votes.json": 30, "data/other.json": 30})

    def test_default_lists_only_code_and_counts_the_rest(self, repo):
        result = list_communities_func(repo_root=str(repo))
        names = [c["name"] for c in result["communities"]]
        assert names and not any("json" in n for n in names)
        assert result["config_communities_hidden"] >= 1

    def test_include_config_lists_them_and_reports_no_hidden_count(self, repo):
        result = list_communities_func(repo_root=str(repo), include_config=True)
        assert any("json" in c["name"] for c in result["communities"])
        assert "config_communities_hidden" not in result

    def test_total_and_paging_describe_the_filtered_list(self, repo):
        everything = list_communities_func(
            repo_root=str(repo), include_config=True, detail_level="minimal",
        )["total"]
        code_only = list_communities_func(
            repo_root=str(repo), detail_level="minimal", max_results=1,
        )
        assert code_only["total"] < everything
        assert code_only["next_cursor"]
        refused = list_communities_func(
            repo_root=str(repo), include_config=True, max_results=1,
            cursor=code_only["next_cursor"],
        )
        assert refused["status"] == "error"

    def test_overview_leaves_config_communities_and_their_coupling_out(self, repo):
        hidden = get_architecture_overview_func(repo_root=str(repo))
        shown = get_architecture_overview_func(repo_root=str(repo), include_config=True)
        assert not any("json" in c["name"] for c in hidden["communities"])
        assert any("json" in c["name"] for c in shown["communities"])
        assert hidden["config_communities_hidden"] >= 1
        assert "config_communities_hidden" not in shown

    def test_a_repo_without_config_has_no_hidden_count(self, tmp_path):
        repo = _repo(tmp_path, {})
        assert "config_communities_hidden" not in list_communities_func(repo_root=str(repo))


class TestHubsLeaveConfigOut:
    def test_json_roots_do_not_lead_the_ranking_by_default(self, tmp_path):
        repo = _repo(tmp_path, {"data/votes.json": 30})
        default = get_hub_nodes_func(repo_root=str(repo), top_n=5)["hub_nodes"]
        assert default and all(h["kind"] != "JsonPath" for h in default)
        everything = get_hub_nodes_func(
            repo_root=str(repo), top_n=5, include_config=True,
        )["hub_nodes"]
        assert everything[0]["kind"] == "JsonPath" and everything[0]["name"] == "$"

    def test_the_function_keeps_config_unless_told_otherwise(self, tmp_path):
        repo = _repo(tmp_path, {"data/votes.json": 30})
        store = GraphStore(str(repo / ".code-review-graph" / "graph.db"))
        try:
            assert find_hub_nodes(store, top_n=1)[0]["kind"] == "JsonPath"
            assert find_hub_nodes(store, top_n=1, include_config=False)[0]["kind"] == "Function"
        finally:
            store.close()


class TestARepoOfOnlyConfigIsNotEmpty:
    """Hiding config must not leave a repository of data files with nothing to show."""

    @pytest.fixture()
    def repo(self, tmp_path):
        (tmp_path / ".git").mkdir()
        data = tmp_path / ".code-review-graph"
        data.mkdir()
        store = GraphStore(str(data / "graph.db"))
        for path in ("data/votes.json", "data/other.json"):
            names = [_add(store, "JsonPath", f"$.k{i}", path, i + 1, "json") for i in range(30)]
            _chain(store, path, names)
            root = _add(store, "JsonPath", "$", path, 0, "json")
            for qn in names:
                store.upsert_edge(EdgeInfo(
                    kind="CONTAINS", source=root, target=qn, file_path=path, line=1,
                ))
        store.commit()
        cm.store_communities(store, cm.detect_communities(store, min_size=2))
        store.commit()
        store.close()
        return tmp_path

    def test_communities_are_listed_with_a_note(self, repo):
        result = list_communities_func(repo_root=str(repo))
        assert result["total"] >= 1 and "json" in result["communities"][0]["name"]
        assert "no code communities" in result["note"]
        assert "config_communities_hidden" not in result

    def test_the_overview_shows_them_with_a_note(self, repo):
        result = get_architecture_overview_func(repo_root=str(repo))
        assert result["communities"] and "no code communities" in result["note"]

    def test_hubs_are_ranked_with_a_note(self, repo):
        result = get_hub_nodes_func(repo_root=str(repo), top_n=3)
        assert result["hub_nodes"] and result["hub_nodes"][0]["name"] == "$"
        assert "no hub but config" in result["note"]

    def test_asking_for_config_adds_no_note(self, repo):
        assert "note" not in list_communities_func(repo_root=str(repo), include_config=True)
        assert "note" not in get_hub_nodes_func(repo_root=str(repo), include_config=True)

    def test_a_repo_with_code_gets_no_note(self, tmp_path):
        repo = _repo(tmp_path, {"data/votes.json": 30})
        assert "note" not in list_communities_func(repo_root=str(repo))
        assert "note" not in get_hub_nodes_func(repo_root=str(repo))
        assert "note" not in get_architecture_overview_func(repo_root=str(repo))
