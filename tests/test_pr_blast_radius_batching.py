"""The batched reads of pr_blast_radius answer exactly as the one-by-one reads did."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any, Optional

import pytest

from code_review_graph import pr_blast_radius as pbr
from code_review_graph.constants import (
    IMPACT_DEFAULT_EDGE_DIRECTION,
    IMPACT_DIRECTION_INCOMING,
    IMPACT_EDGE_DIRECTIONS,
)
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import get_db_path
from code_review_graph.tools.pr_tools import pr_blast_radius_func

from .test_pr_blast_radius import git, registry, write  # noqa: F401

BENCH = Path(__file__).resolve().parent.parent / "scripts" / "bench_pr_blast_radius.py"


def _bench() -> Any:
    spec = importlib.util.spec_from_file_location("bench_pr_blast_radius", BENCH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    """A small synthetic Python repo on ``main`` (graph built) and a PR on ``pr``."""
    home = tmp_path_factory.mktemp("home")
    root = tmp_path_factory.mktemp("repo") / "repo"
    import os

    old = os.environ.get("CRG_HOME")
    os.environ["CRG_HOME"] = str(home)
    try:
        _bench().build_repo(root, 80, 16)
        yield root
    finally:
        if old is None:
            os.environ.pop("CRG_HOME", None)
        else:
            os.environ["CRG_HOME"] = old


def _legacy_reach(
    store: GraphStore, seeds: list[str], depth: int, max_nodes: int,
    budget: Optional[list[int]],
) -> dict[str, int]:
    """The walk as it was written before the reads were batched, one query per node."""
    hops = {qn: 0 for qn in seeds}
    frontier = list(seeds)
    for hop in range(1, max(0, depth) + 1):
        following: list[str] = []
        for qn in frontier:
            for edge in store.get_edges_by_target(qn):
                direction = IMPACT_EDGE_DIRECTIONS.get(edge.kind, IMPACT_DEFAULT_EDGE_DIRECTION)
                if direction != IMPACT_DIRECTION_INCOMING:
                    continue
                if budget is not None and budget[0] <= 0:
                    return hops
                if edge.source_qualified not in hops and len(hops) < max_nodes:
                    hops[edge.source_qualified] = hop
                    following.append(edge.source_qualified)
                    if budget is not None:
                        budget[0] -= 1
        frontier = following
        if not frontier:
            break
    return hops


@pytest.mark.parametrize("depth", [1, 2, 3, 6])
@pytest.mark.parametrize("max_nodes", [7, 200, 5_000])
@pytest.mark.parametrize("budget", [None, 5, 60, 50_000])
def test_batched_reach_matches_the_one_by_one_walk(synthetic, depth, max_nodes, budget):
    with GraphStore(get_db_path(synthetic), read_only=True) as store:
        seeds = [
            n.qualified_name for n in store.get_nodes_by_kind(["Function"])
            if "/mod_1" in n.qualified_name or "/mod_2" in n.qualified_name
        ][:40]
        assert seeds
        old = _legacy_reach(
            store, seeds, depth, max_nodes, None if budget is None else [budget],
        )
        new = pbr.reach_from(
            store, seeds, depth, max_nodes, None if budget is None else [budget],
        )
    # Same nodes, same hops, same discovery order (a capped walk depends on it).
    assert list(new.items()) == list(old.items())


def _run_pr(root: Path) -> str:
    result = pr_blast_radius_func(
        repo_root=str(root), head="pr", base="main", include_external=False, files_limit=200,
    )
    return json.dumps(result, sort_keys=True, default=str)


def test_graph_reads_match_per_node_queries(synthetic, monkeypatch):
    class OneByOne(pbr._GraphReads):
        """Every read goes to the store on its own, as it did before batching."""

        def prefetch_incoming(self, names):
            return None

        def prefetch_nodes(self, names):
            return None

        def prefetch_out(self, names):
            return None

        def incoming(self, name):
            return [(e.kind, e.source_qualified) for e in self._store.get_edges_by_target(name)]

        def node(self, name):
            n = self._store.get_node(name)
            return None if n is None else (n.file_path, bool(n.is_test), n.kind, n.name)

        def outgoing(self, name):
            return [
                (e.kind, e.target_qualified) for e in self._store.get_edges_by_source(name)
                if e.kind in ("HANDLES", "TESTED_BY")
            ]

    batched = _run_pr(synthetic)
    monkeypatch.setattr(pbr, "_GraphReads", OneByOne)
    one_by_one = _run_pr(synthetic)
    assert json.loads(batched)["status"] == "ok"
    assert batched == one_by_one


def test_primed_files_give_the_same_result_with_fewer_git_calls(synthetic, monkeypatch):
    counts = {"git": 0}
    real = pbr._git

    def counting(*args, **kwargs):
        counts["git"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pbr, "_git", counting)
    primed = _run_pr(synthetic)
    primed_calls = counts["git"]

    monkeypatch.setattr(pbr.BlobReader, "prime", lambda self, paths, max_bytes=0: None)
    counts["git"] = 0
    unprimed = _run_pr(synthetic)
    assert primed == unprimed
    # One ``git show`` per changed file before; a handful of batch calls now.
    assert primed_calls < counts["git"] // 2


@pytest.fixture
def blob_repo(tmp_path):
    root = tmp_path / "blobs"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    write(root, "plain.txt", "line one\nline two\n")
    write(root, "sp ace/ünï.txt", "unicode path\n")
    write(root, "empty.txt", "")
    (root / "binary.bin").write_bytes(b"\x00\x01\n\n\xff" * 100 + b"\n")
    (root / "big.bin").write_bytes(b"x" * (pbr.MAX_FILE_BYTES + 2))
    (root / "edge.bin").write_bytes(b"y" * pbr.MAX_FILE_BYTES)
    write(root, "dir/inner.txt", "inside\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    return root


def test_blob_reader_matches_git_show(blob_repo):
    head = git(blob_repo, "rev-parse", "HEAD")
    paths = [
        "plain.txt", "sp ace/ünï.txt", "empty.txt", "binary.bin", "big.bin", "edge.bin",
        "dir", "dir/inner.txt", "absent.txt", "plain.txt",
    ]
    show = pbr.git_file_reader(str(blob_repo), head)
    expected = [show(p) for p in paths]
    batched = pbr.git_file_reader(str(blob_repo), head)
    batched.prime(paths)
    assert [batched(p) for p in paths] == expected
    assert expected[4] is None
    assert expected[8] is None


def test_blob_reader_respects_the_byte_budget(blob_repo):
    head = git(blob_repo, "rev-parse", "HEAD")
    reader = pbr.git_file_reader(str(blob_repo), head)
    reader.prime(["plain.txt", "edge.bin"], max_bytes=100)
    # Past the budget a file is read on demand, with the same bytes.
    assert reader("edge.bin") == b"y" * pbr.MAX_FILE_BYTES
    assert reader("plain.txt") == b"line one\nline two\n"


def test_a_confirmed_sha_is_not_resolved_again(blob_repo, monkeypatch):
    head = git(blob_repo, "rev-parse", "HEAD")
    calls = {"n": 0}
    real = pbr._git

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(pbr, "_git", counting)
    with pbr.scratch_session(str(blob_repo)):
        first = pbr.resolve_commit(str(blob_repo), head, "head")
        before = calls["n"]
        second = pbr.resolve_commit(str(blob_repo), first, "head")
        assert calls["n"] == before
        assert second == first
    # Outside a session nothing is remembered.
    pbr.resolve_commit(str(blob_repo), head, "head")
    after_one = calls["n"]
    pbr.resolve_commit(str(blob_repo), head, "head")
    assert calls["n"] == after_one + 1


def test_unknown_sha_still_fails_in_a_session(blob_repo):
    with pbr.scratch_session(str(blob_repo)):
        with pytest.raises(pbr.PullRequestError):
            pbr.resolve_commit(str(blob_repo), "0" * 40, "head")

