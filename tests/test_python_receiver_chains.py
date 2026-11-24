"""Python receivers written as an attribute chain, a call result or a shared instance.

``clients.jira.get_issue()``, ``_run_budget(run_id).recorder()`` and
``SPEC.session_manager()`` name no local variable, so the receiver reader used
to leave each one ``unknown`` and the call unresolved. Each is typed here only
from what the source writes (a class-body annotation, ``self.x = Klass()``, a
return annotation, ``NAME = Klass(...)``), and only a class the repository
defines, with the method on it, becomes a direct edge.

The graph also reports calls that can never be in the repository (builtins and
methods of imported third-party names) as ``external``, computed at read time
from the stored edges without changing ``target_resolution``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.postprocessing import run_post_processing

_MODELS = '''\
class JiraClient:
    def get_issue(self, key):
        return key


class Recorder:
    def record(self):
        return 1


class Budget:
    def recorder(self) -> "Recorder":
        return Recorder()


class Decoy:
    def get_issue(self, key):
        return None

    def recorder(self):
        return None


class BotSpec:
    def __init__(self, name):
        self.name = name

    def session_manager(self):
        return self.name
'''

_SPECS = '''\
from .models import BotSpec

SPEC = BotSpec("bot")
'''

_CLIENTS = '''\
from .models import Decoy, JiraClient


class Clients:
    jira: JiraClient

    def __init__(self):
        self.decoy = Decoy()
'''

_USE = '''\
import json

from .clients import Clients
from .models import Budget
from .specs import SPEC


def budget_for(run_id) -> Budget:
    return Budget()


def attribute_chain(clients: Clients):
    clients.jira.get_issue("A")


def constructed_attribute(clients: Clients):
    clients.decoy.get_issue("B")


def return_annotated(run_id):
    budget_for(run_id).recorder()


def module_instance():
    SPEC.session_manager()


def missing_method_on_known_class(clients: Clients):
    clients.jira.not_a_method()


def unknown_receiver(anything):
    anything.jira.get_issue("C")


def builtins_and_third_party(payload):
    len(payload)
    json.dumps(payload)
    options = {}
    options.get("key")
'''


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    root = tmp_path_factory.mktemp("chains")
    package = root / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, body in (
        ("models", _MODELS), ("specs", _SPECS), ("clients", _CLIENTS), ("use", _USE),
    ):
        (package / f"{name}.py").write_text(body, encoding="utf-8")
    store = GraphStore(root / ".code-review-graph" / "graph.db")
    try:
        full_build(root, store)
        run_post_processing(store)
        rows = store._conn.execute(
            "SELECT source_qualified, target_qualified, target_resolution "
            "FROM edges WHERE kind = 'CALLS' AND file_path LIKE '%use.py'"
        ).fetchall()
        calls: dict[str, list[tuple[str, str]]] = {}
        for source, target, resolution in rows:
            caller = source.rsplit("::", 1)[-1]
            calls.setdefault(caller, []).append(
                (target.rsplit("::", 1)[-1], resolution),
            )
        yield {"calls": calls, "split": store.count_calls_by_classification(),
               "store": store, "root": Path(root)}
    finally:
        store.close()


def _targets(repo, caller: str) -> set[str]:
    return {t for t, r in repo["calls"][caller] if r == "direct"}


def test_attribute_chain_on_a_class_body_annotation(repo):
    assert "JiraClient.get_issue" in _targets(repo, "attribute_chain")


def test_attribute_chain_on_a_field_built_in_init(repo):
    targets = _targets(repo, "constructed_attribute")
    assert "Decoy.get_issue" in targets
    assert "JiraClient.get_issue" not in targets


def test_return_annotated_call_receiver(repo):
    targets = _targets(repo, "return_annotated")
    assert "Budget.recorder" in targets
    assert "Decoy.recorder" not in targets


def test_module_level_instance_imported_from_another_module(repo):
    assert "BotSpec.session_manager" in _targets(repo, "module_instance")


def test_a_method_the_class_lacks_stays_unresolved(repo):
    assert ("not_a_method", "unresolved") in repo["calls"][
        "missing_method_on_known_class"
    ]


def test_an_unknown_receiver_stays_unresolved(repo):
    assert repo["calls"]["unknown_receiver"] == [("get_issue", "unresolved")]


def test_builtins_and_third_party_calls_are_external_not_unresolved(repo):
    store = repo["store"]
    external = store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS' AND file_path LIKE '%use.py' "
        "AND target_qualified IN ('len', 'dumps', 'get')"
    ).fetchone()[0]
    assert external == 3
    split = repo["split"]
    assert split["external"] >= 3
    total = store._conn.execute(
        "SELECT COUNT(*) FROM edges WHERE kind = 'CALLS'"
    ).fetchone()[0]
    assert split["direct"] + split["unresolved"] + split["external"] == total
    # The stored classification is untouched: external edges stay 'unresolved'.
    stored = store.count_edges_by_resolution("CALLS")
    assert stored["unresolved"] == split["unresolved"] + split["external"]


def test_unknown_receiver_is_not_counted_external(repo):
    store = repo["store"]
    unknown = store._conn.execute(
        "SELECT json_extract(extra, '$.receiver_binding') FROM edges "
        "WHERE kind = 'CALLS' AND file_path LIKE '%use.py' "
        "AND target_qualified = 'get_issue' AND target_resolution = 'unresolved'"
    ).fetchall()
    assert unknown
    # ``anything.jira.get_issue`` binds to nothing, so it is neither direct nor external.
    assert all(row[0] in (None, "unknown") for row in unknown)
