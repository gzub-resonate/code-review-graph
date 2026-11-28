"""The cold path of the registry-wide cross-repo calls: same answers, fewer page reads.

The first call after a start or a reindex reads every repo's graph from the file. These tests
pin what was changed to make that cheaper and show that no answer changed:

* the graph stamp no longer counts rows (``COUNT(*)`` read every page of an edge index);
* the AWS declaration and edge readers go through the ``target_qualified`` / ``name`` indexes
  (each is compared with the query it replaced, on rows chosen to tell them apart);
* ``consumers`` and ``external`` read the repos ahead on the shared workers;
* the warm-up fills the context a call scoped with ``repos`` reads from the other repos.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import code_review_graph.repo_cache as rc
from code_review_graph import aws_sdk_links as al
from code_review_graph import consumer_join as cj
from code_review_graph import cross_repo_warmup as cw
from code_review_graph import infra_references as ir
from code_review_graph import stepfunction_links as sl
from code_review_graph.graph import GraphStore
from code_review_graph.parser import NodeInfo
from code_review_graph.repo_cache import MEMO, graph_stamp
from code_review_graph.tools import cross_repo_origins as cro
from code_review_graph.tools import registry_tools as rt
from code_review_graph.tools.registry_tools import cross_repo_requests_func

from .test_external_and_consumers import world as world  # noqa: F401
from .test_service_aliases_infra_join import registry as registry  # noqa: F401


def graph_file(root: Path) -> Path:
    return root / ".code-review-graph" / "graph.db"


# -- the stamp -----------------------------------------------------------------------------------


class _Recording(sqlite3.Connection):
    statements: list[str] = []

    def execute(self, sql, *args, **kwargs):  # type: ignore[no-untyped-def]
        type(self).statements.append(sql)
        return super().execute(sql, *args, **kwargs)


class TestStampWithoutRowCounts:
    def test_the_marker_reads_no_row_count(self, world, monkeypatch):  # noqa: F811
        entry = {"path": str(world.parent / "ledger-svc")}
        _Recording.statements = []
        real = sqlite3.connect
        monkeypatch.setattr(
            rc.sqlite3, "connect",
            lambda *a, **k: real(*a, **{**k, "factory": _Recording}),
        )
        assert graph_stamp(entry) is not None
        assert _Recording.statements, "the marker was not read"
        assert not [s for s in _Recording.statements if "count(" in s.lower().replace(" ", "")
                    and "pragma" not in s.lower()], _Recording.statements

    def test_an_inserted_edge_moves_the_stamp(self, world):  # noqa: F811
        root = world.parent / "ledger-svc"
        entry = {"path": str(root)}
        first = graph_stamp(entry)
        with GraphStore(graph_file(root)) as store:
            store._conn.execute(
                "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, "
                "updated_at) VALUES ('CALLS', 'a', 'b', 'f.py', 1.0)"
            )
            store._conn.commit()
        assert graph_stamp(entry) != first

    def test_a_deletion_that_frees_pages_moves_the_stamp(self, tmp_path, registry):  # noqa: F811
        root = (tmp_path / "big").resolve()
        (root / ".code-review-graph").mkdir(parents=True)
        nodes = [
            NodeInfo(
                kind="Function", name=f"fn_{n}", file_path=str(root / "big.py"),
                line_start=n, line_end=n, language="python",
                extra={"pad": "x" * 400},
            )
            for n in range(1, 1500)
        ]
        with GraphStore(graph_file(root)) as store:
            store.store_file_nodes_edges(str(root / "big.py"), nodes, [])
            store.set_metadata("last_updated", "2099-01-01T00:00:00")
        registry.register(str(root), alias="big")
        entry = {"path": str(root)}
        first = graph_stamp(entry)
        with GraphStore(graph_file(root)) as store:
            store.remove_file_data(str(root / "big.py"))  # no metadata written
        assert graph_stamp(entry) != first

    def test_a_metadata_write_that_changes_nothing_keeps_the_stamp(self, world):  # noqa: F811
        root = world.parent / "ledger-svc"
        entry = {"path": str(root)}
        first = graph_stamp(entry)
        with GraphStore(graph_file(root)) as store:
            store._conn.execute("INSERT INTO metadata (key, value) VALUES ('scratch', '1')")
            store._conn.execute("DELETE FROM metadata WHERE key = 'scratch'")
            store._conn.commit()
        assert graph_stamp(entry) == first


# -- the indexed readers -------------------------------------------------------------------------

OLD_DECLARE_SQL = (
    "SELECT target_qualified, file_path, line, extra FROM edges "
    "WHERE kind = 'REFERENCES' AND json_valid(extra) "
    "AND json_extract(extra, '$.resolution') = ? "
    "AND json_extract(extra, '$.sdk') = 'terraform' "
    "AND json_extract(extra, '$.operation') = 'declare' "
    "AND COALESCE(json_extract(extra, '$.unresolved'), 0) = 0 "
    "ORDER BY file_path, line, id"
)
OLD_EDGES_SQL = (
    "SELECT source_qualified, file_path, line, target_qualified, extra, kind FROM edges "
    "WHERE json_valid(extra) AND ((kind = 'REFERENCES' "
    "AND json_extract(extra, '$.resolution') = ?) OR (kind IN ('CONSUMES', 'PRODUCES') "
    "AND (target_qualified LIKE 'external:aws-sqs:%' "
    "OR target_qualified LIKE 'external:aws-sns:%'))) ORDER BY file_path, line, id"
)
OLD_DECLARED_SQL = (
    "SELECT target_qualified, json_extract(extra, '$.target_kind') FROM edges "
    "WHERE kind = 'REFERENCES' AND json_valid(extra) "
    "AND json_extract(extra, '$.resolution') = 'aws_sdk' "
    "AND json_extract(extra, '$.sdk') = 'terraform' "
    "AND json_extract(extra, '$.operation') = 'declare' "
    "AND COALESCE(json_extract(extra, '$.unresolved'), 0) = 0 "
    "AND target_qualified LIKE 'external:aws-%'"
)
OLD_RESOURCE_NODES_SQL = (
    "SELECT name FROM nodes WHERE language = 'hcl' AND kind = 'Class' "
    "AND name LIKE 'resource.aws\\_%' ESCAPE '\\'"
)
OLD_FILE_DECLARED_SQL = (
    "SELECT name, file_path, line_start, line_end FROM nodes "
    "WHERE kind = 'Class' AND language = 'hcl' AND (name LIKE ? OR name LIKE ?) "
    "ORDER BY file_path, line_start"
)

_EXTRA = '{"resolution": "aws_sdk", "sdk": "%s", "operation": "%s", "unresolved": %s}'


def _x(sdk, operation, unresolved="false"):
    return _EXTRA % (sdk, operation, unresolved)


@pytest.fixture
def graph(tmp_path):
    """A graph holding rows that tell the old queries from the new ones."""
    root = (tmp_path / "mixed").resolve()
    (root / ".code-review-graph").mkdir(parents=True)
    store = GraphStore(graph_file(root))
    conn = store._conn
    edge = (
        "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line, extra, "
        "updated_at) VALUES (?, ?, ?, ?, ?, ?, 1.0)"
    )
    rows = [
        ("REFERENCES", "s1", "external:aws-s3:bucket-a", "a.py", 3, _x("boto3", "get")),
        ("REFERENCES", "s2", "external:aws-sqs:q1", "main.tf", 5, _x("terraform", "declare")),
        ("REFERENCES", "s3", "external:aws-sqs:q2", "main.tf", 9, _x("terraform", "declare")),
        ("REFERENCES", "s4", "external:aws-sqs:?", "main.tf", 12,
         _x("terraform", "declare", "true")),
        ("REFERENCES", "s5", "external:aws-dynamodb:t1", "b.tf", 1, _x("terraform", "declare")),
        ("REFERENCES", "s6", "external:aws-lambda:fn", "asl.json", 2, '{"resolution": "asl_x"}'),
        ("REFERENCES", "s7", "external:rabbitmq:ex", "r.py", 2, _x("boto3", "get")),
        ("CONSUMES", "c1", "external:aws-sqs:inbox", "w.py", 4, "{}"),
        ("PRODUCES", "c2", "external:aws-sns:topic", "w.py", 6, "{}"),
        ("PRODUCES", "c3", "external:aws-s3:out", "w.py", 8, "{}"),
        ("CONSUMES", "c4", "external:kafka:events", "w.py", 9, "{}"),
        ("CONSUMES", "c5", "external:aws-sqs:inbox2", "a.py", 1, "not json"),
        ("CALLS", "x", "y", "a.py", 1, "{}"),
    ]
    for kind, src, target, file, line, extra in rows:
        conn.execute(edge, (kind, src, target, file, line, extra))
    node = (
        "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, "
        "language, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1.0)"
    )
    for n, (kind, name, language) in enumerate([
        ("Class", "resource.aws_sqs_queue.q1", "hcl"),
        ("Class", "resource.aws_ssm_parameter.p1", "hcl"),
        ("Class", "resource.aws_secretsmanager_secret.s1", "hcl"),
        ("Class", "resource.aws_ssm_parameter_extra.nope", "hcl"),
        ("Class", "resource.google_pubsub_topic.t", "hcl"),
        ("Class", "data.aws_ssm_parameter.d", "hcl"),
        ("Class", "resource.aws_sqs_queue.q9", "python"),
        ("Function", "resource.aws_sqs_queue.q8", "hcl"),
        ("Class", "resource.aws_ssm_parameter.p2", "hcl"),
    ]):
        conn.execute(node, (kind, name, f"q{n}", f"f{n % 3}.tf", n + 1, n + 3, language))
    conn.commit()
    yield store
    store.close()


def _plan(conn, sql, params=()):
    return " ".join(
        row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    )


class TestIndexedReadersAnswerAsBefore:
    def test_declare_edges(self, graph):
        conn = graph._conn
        old = [
            row for row in conn.execute(OLD_DECLARE_SQL, (al.RESOLUTION,)).fetchall()
            if row[0].startswith("external:aws-")
        ]
        new = conn.execute(al._DECLARE_SQL, (al.RESOLUTION,)).fetchall()
        assert [tuple(r) for r in new] == [tuple(r) for r in old] and len(new) == 3
        assert "idx_edges_target_kind" in _plan(conn, al._DECLARE_SQL, (al.RESOLUTION,))

    def test_edges_of_a_graph(self, graph):
        conn = graph._conn
        old = [tuple(r) for r in conn.execute(OLD_EDGES_SQL, (al.RESOLUTION,)).fetchall()]
        new = [tuple(r) for r in conn.execute(al._EDGES_SQL, (al.RESOLUTION,)).fetchall()]
        # The old query also returns an ``aws_sdk`` reference whose target is no AWS target
        # (``external:rabbitmq:ex``); no writer makes one (see the next test).
        old_aws = [r for r in old if r[3] != "external:rabbitmq:ex"]
        assert new == old_aws
        assert {r[5] for r in new} == {"REFERENCES", "CONSUMES", "PRODUCES"}
        assert "idx_edges_target_kind" in _plan(conn, al._EDGES_SQL, (al.RESOLUTION,))
        assert "SCAN edges" not in _plan(conn, al._EDGES_SQL, (al.RESOLUTION,))

    def test_every_aws_target_a_writer_builds_is_inside_the_range(self):
        for service in ("s3", "sqs", "sns", "dynamodb", "states", "ssm", "x-y"):
            for name in ("bucket", "?", "a:b", "Mixed-Case", "é", ""):
                target = ir.aws_external_target(service, name)
                assert al._AWS_TARGET_FROM <= target < al._AWS_TARGET_BELOW, target

    def test_declared_names(self, graph):
        conn = graph._conn
        old = [tuple(r) for r in conn.execute(OLD_DECLARED_SQL).fetchall()]
        new = [tuple(r) for r in conn.execute(sl._DECLARED_SQL).fetchall()]
        assert new == old and len(new) == 3
        assert "idx_edges_target_kind" in _plan(conn, sl._DECLARED_SQL)

    def test_resource_nodes_of_the_definitions(self, graph):
        found = sl.Definitions()._read(graph)
        old_names = [r[0] for r in graph._conn.execute(OLD_RESOURCE_NODES_SQL).fetchall()]
        assert "resource.aws_sqs_queue.q1" in old_names
        # The ranges find the same nodes: ``Definitions`` reads the same keys either way.
        by_type = {resource: key for key, resource in sl.RESOURCE_TYPES.items()}
        expected = set()
        for name in old_names:
            match = sl._RESOURCE_NAME.match(name)
            key = by_type.get(match["type"]) if match else None
            if key is not None:
                expected.add((*key, "~" + sl._fold(match["label"])))
        assert expected <= set(found)
        assert "idx_nodes_name" in _plan(
            graph._conn,
            "SELECT name FROM nodes WHERE +language = 'hcl' AND +kind = 'Class' "
            "AND name >= 'resource.aws_' AND name < 'resource.aws`'",
        )

    def test_file_declared_nodes(self, graph, tmp_path):
        conn = graph._conn
        old = conn.execute(
            OLD_FILE_DECLARED_SQL, tuple(f"resource.{t}.%" for t in cj.FILE_DECLARED),
        ).fetchall()
        root = Path(tmp_path / "mixed")
        read = []
        original = cj._block_name

        def spy(repo_path, file, start, end):
            read.append((file, start))
            return original(repo_path, file, start, end)

        cj._block_name = spy  # type: ignore[assignment]
        try:
            cj.read_declarations(graph, root)
        finally:
            cj._block_name = original  # type: ignore[assignment]
        assert sorted(read) == sorted((str(Path(r[1])), r[2]) for r in old)
        assert len(old) == 3


# -- reading ahead -------------------------------------------------------------------------------


def _clear():
    MEMO.clear()
    from code_review_graph.repo_cache import CONSUMER_RESULTS, INFRASTRUCTURE_RESULTS

    CONSUMER_RESULTS.clear()
    INFRASTRUCTURE_RESULTS.clear()


class TestReadAhead:
    @pytest.mark.parametrize("kwargs", [
        {"origin": "consumers", "service": "orders-intake-queue"},
        {"origin": "consumers", "service": "/prod/orders/db/password"},
        {"origin": "consumers"},
        {"origin": "consumers", "service": "acme-export-files", "repos": ["infra"]},
        {"origin": "external"},
    ])
    def test_the_answer_equals_the_one_read_one_repo_at_a_time(
        self, world, monkeypatch, kwargs,  # noqa: F811
    ):
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "1")
        rc._reset_shared_pool()
        _clear()
        serial = cross_repo_requests_func(max_results=100, **kwargs)
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "4")
        rc._reset_shared_pool()
        _clear()
        ahead = cross_repo_requests_func(max_results=100, **kwargs)
        rc._reset_shared_pool()
        assert serial["total"] > 0 or kwargs == {"origin": "consumers"}
        assert ahead == serial

    def test_every_repo_is_read_before_the_join_loop_needs_it(
        self, world, monkeypatch,  # noqa: F811
    ):
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "4")
        rc._reset_shared_pool()
        _clear()
        order: list[str] = []
        original = cro._read_ahead

        def watch(jobs, budget, started):
            original(jobs, budget, started)
            order.append("ahead:" + str(len(jobs)))

        monkeypatch.setattr(cro, "_read_ahead", watch)
        reads: list[str] = []
        real_get = MEMO.get

        def counting(namespace, entry, compute, cacheable=None):
            def counted():
                reads.append(namespace)
                order.append(namespace)
                return compute()

            return real_get(namespace, entry, counted, cacheable)

        monkeypatch.setattr(MEMO, "get", counting)
        cross_repo_requests_func(origin="consumers", service="orders-intake-queue")
        rc._reset_shared_pool()
        assert order[: len(reads)] and order.index("ahead:12") > 0
        # Each namespace and repo is computed once: the loop found the read-ahead values.
        assert len(reads) == len(set(zip(reads, range(len(reads)))))
        assert reads.count("consumer-declarations") == 6
        assert reads.count("consumer-uses") == 6

    def test_a_spent_budget_still_skips_as_before(self, world, monkeypatch):  # noqa: F811
        monkeypatch.setenv("CRG_CROSS_REPO_WORKERS", "4")
        monkeypatch.setenv("CRG_CROSS_REPO_BUDGET_SECONDS", "0.000001")
        rc._reset_shared_pool()
        _clear()
        time.sleep(0.01)
        reply = cross_repo_requests_func(origin="consumers", service="orders-intake-queue")
        rc._reset_shared_pool()
        assert reply["truncated"] is True and reply["skipped_repos"]

    def test_a_search_reads_the_stamps_side_by_side(self, world, monkeypatch):  # noqa: F811
        seen: list[int] = []
        monkeypatch.setattr(
            rt.xf, "prefetch_stamps", lambda entries, workers: seen.append(len(entries)),
        )
        from code_review_graph.tools.registry_tools import cross_repo_search_func

        cross_repo_search_func(query="run")
        assert seen == [6]


# -- the warm-up ---------------------------------------------------------------------------------


def _count_computes(monkeypatch):
    computed: list[str] = []
    real_get = MEMO.get

    def counting(namespace, entry, compute, cacheable=None):
        def counted():
            computed.append(namespace)
            return compute()

        return real_get(namespace, entry, counted, cacheable)

    monkeypatch.setattr(MEMO, "get", counting)
    return computed


class TestWarmUpFillsTheContext:
    def test_a_scoped_call_after_an_unscoped_one_reads_the_context_of_the_others(
        self, world, monkeypatch,  # noqa: F811
    ):
        """The gap: the unscoped call reads no ``awssdk-context``, a scoped one reads all."""
        _clear()
        cross_repo_requests_func(origin="awssdk")
        computed = _count_computes(monkeypatch)
        cross_repo_requests_func(origin="awssdk", repos=["web-app"])
        assert computed.count("awssdk-context") == 5

    def test_the_warm_up_call_leaves_nothing_for_the_scoped_call_to_read(
        self, world, monkeypatch,  # noqa: F811
    ):
        _clear()
        cw._default_call("awssdk")
        computed = _count_computes(monkeypatch)
        warmed = cross_repo_requests_func(origin="awssdk", repos=["web-app"])
        assert [name for name in computed if name.startswith("awssdk")] == []
        _clear()
        cold = cross_repo_requests_func(origin="awssdk", repos=["web-app"])
        assert warmed == cold

    def test_every_repo_is_warmed_and_other_origins_are_left_alone(self, world):  # noqa: F811
        _clear()
        assert rt.warm_origin_context("awssdk") == 6
        assert rt.warm_origin_context("feign") == 0
        before = len(MEMO)
        assert rt.warm_origin_context("awssdk") == 6 and len(MEMO) == before

    def test_the_default_call_still_returns_the_reply_of_the_origin_call(
        self, monkeypatch,
    ):
        captured = {}
        monkeypatch.setattr(
            rt, "cross_repo_requests_func",
            lambda **kw: captured.update(kw) or {"status": "ok"},
        )
        monkeypatch.setattr(rt, "warm_origin_context", lambda origin: captured.update(w=origin))
        assert cw._default_call("awssdk") == {"status": "ok"}
        assert captured == {"origin": "awssdk", "w": "awssdk"}
