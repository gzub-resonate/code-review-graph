"""The ``infrastructure`` block reads each repo's code names once and only what it needs.

The per-repo summaries must not depend on which other repos were selected, and the work for a
one-repo selection out of many must not grow with the number of repos that cannot matter.
"""

from __future__ import annotations

import pytest

from code_review_graph import infra_join as ij
from code_review_graph import service_aliases as sa
from code_review_graph.registry import Registry
from code_review_graph.repo_cache import MEMO
from code_review_graph.tools import registry_tools as rt

from .test_asl_cross_repo_links import OTHER, make_repo
from .test_asl_cross_repo_links import registry as registry  # noqa: F401
from .test_service_aliases_infra_join import infra_world as infra_world  # noqa: F401


@pytest.fixture
def crowded(infra_world, tmp_path):  # noqa: F811
    for n in range(4):
        make_repo(tmp_path, Registry(), f"bystander-{n}", OTHER)
    return tmp_path


def build(selected_labels, monkeypatch=None):
    repos = Registry().list_repos()
    labels = [sa.repo_names(e)[0] for e in repos]
    chosen = [labels.index(name) for name in selected_labels]
    return ij.build_infrastructure(
        repos, chosen, sa.FactsCache(repos), sa.load_alias_rules(), lambda _t: set(), 0, 2,
    )


def counter(monkeypatch, owner, name):
    calls: list[int] = []
    original = getattr(owner, name)

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, counted)
    return calls


def test_a_summary_does_not_depend_on_the_rest_of_the_selection(crowded):
    everyone = [sa.repo_names(e)[0] for e in Registry().list_repos()]
    full = build(everyone)
    assert "inventory-svc" in full["repos"]
    for name in everyone:
        alone = build([name])
        assert alone["repos"].get(name) == full["repos"].get(name)
    both = build(["inventory-svc", "infra-live"])
    assert both["repos"] == {k: v for k, v in full["repos"].items() if k in both["repos"]}


def test_code_names_are_read_once_per_repo_and_graph(crowded, monkeypatch):
    reads = counter(monkeypatch, ij, "_code_names")
    first = build(["inventory-svc", "infra-live"])
    assert len(reads) == 2
    again = build(["inventory-svc", "infra-live"])
    assert len(reads) == 2 and again == first


def test_declarations_are_read_only_where_a_name_can_match(crowded, monkeypatch):
    decl_reads = counter(monkeypatch, ij, "read_declarations")
    # No code names in the repo that only holds infrastructure: only its own declarations.
    block = build(["infra-live"])
    assert len(decl_reads) == 1
    assert block["repos"].get("infra-live") is None or "resources" in block["repos"]["infra-live"]
    decl_reads.clear()
    MEMO.clear()
    build(["inventory-svc"])
    # The code repo names resources, so every repo's declarations are needed.
    assert len(decl_reads) == len(Registry().list_repos())


def test_a_failed_read_of_code_names_is_not_kept(crowded, monkeypatch):
    real = ij._code_names
    state = {"fail": True}

    def flaky(store):
        if state["fail"]:
            raise ij.sqlite3.Error("busy")
        return real(store)

    monkeypatch.setattr(ij, "_code_names", flaky)
    broken = build(["inventory-svc"])
    assert "resources" not in broken["repos"].get("inventory-svc", {})
    state["fail"] = False
    assert build(["inventory-svc"])["repos"]["inventory-svc"]["resources"]


def test_the_tool_still_carries_the_block(crowded):
    rt.INFRASTRUCTURE_RESULTS.clear()
    reply = rt.cross_repo_requests_func(origin="terraform", repos=["inventory-svc"])
    assert reply["infrastructure"]["repos"]["inventory-svc"]["counts"]["resources"] == 2
