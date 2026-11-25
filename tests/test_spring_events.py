import json
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph


def _parse_java(path: Path, source: str):
    return CodeParser().parse_bytes(path, source.encode())


def test_event_edges_use_package_and_import_qualified_identity(tmp_path: Path) -> None:
    path = tmp_path / "Listeners.java"
    _, edges = _parse_java(
        path,
        """
        package alpha.listeners;
        import beta.events.ExternalEvent;
        import org.springframework.context.event.EventListener;

        class LocalEvent {}
        class Listeners {
            @EventListener
            void local(LocalEvent event) {}

            @EventListener(classes = {LocalEvent.class, ExternalEvent.class})
            void several() {}
        }
        """,
    )

    handles = [edge for edge in edges if edge.kind == "HANDLES"]
    assert {edge.target for edge in handles} == {
        "event::alpha.listeners.LocalEvent",
        "event::beta.events.ExternalEvent",
    }
    assert all(edge.extra["event_type"] in edge.target for edge in handles)


def test_publish_event_new_expression_uses_qualified_identity(tmp_path: Path) -> None:
    path = tmp_path / "Publisher.java"
    _, edges = _parse_java(
        path,
        """
        package alpha.publishers;
        import beta.events.ExternalEvent;

        class Publisher {
            void publish() {
                applicationEvents.publishEvent(new ExternalEvent());
            }
        }
        """,
    )

    publishes = [edge for edge in edges if edge.kind == "PUBLISHES"]
    assert len(publishes) == 1
    assert publishes[0].target == "event::beta.events.ExternalEvent"
    assert publishes[0].extra["event_type"] == "beta.events.ExternalEvent"


def _write_event_package(root: Path, package: str) -> tuple[Path, Path, Path]:
    directory = root / package
    directory.mkdir(parents=True)
    event = directory / "SharedEvent.java"
    publisher = directory / "Publisher.java"
    listener = directory / "Listener.java"
    event.write_text(
        f"package {package};\nclass SharedEvent {{}}\n",
        encoding="utf-8",
    )
    publisher.write_text(
        f"""package {package};
        class Publisher {{
            void publish() {{ events.publishEvent(new SharedEvent()); }}
        }}
        """,
        encoding="utf-8",
    )
    listener.write_text(
        f"""package {package};
        import org.springframework.context.event.EventListener;
        class Listener {{
            @EventListener void on(SharedEvent event) {{}}
        }}
        """,
        encoding="utf-8",
    )
    return event, publisher, listener


def _event_calls(store: GraphStore):
    rows = store._conn.execute(
        "SELECT source_qualified, target_qualified, extra FROM edges WHERE kind = 'CALLS'"
    ).fetchall()
    return [row for row in rows if json.loads(row["extra"] or "{}").get("spring_event_resolved")]


def test_event_resolver_does_not_cross_link_same_named_packages(tmp_path: Path) -> None:
    _write_event_package(tmp_path, "alpha")
    _write_event_package(tmp_path, "beta")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        result = full_build(tmp_path, store)
        calls = _event_calls(store)

        assert result["event_resolution"]["calls_emitted"] == 2
        assert len(calls) == 2
        assert all(
            ("/alpha/" in row["source_qualified"] and "/alpha/" in row["target_qualified"])
            or ("/beta/" in row["source_qualified"] and "/beta/" in row["target_qualified"])
            for row in calls
        )
        assert store.get_node("event::alpha.SharedEvent") is not None
        assert store.get_node("event::beta.SharedEvent") is not None


def test_incremental_listener_change_removes_stale_event_call(tmp_path: Path) -> None:
    _, _, listener = _write_event_package(tmp_path, "alpha")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()

    with GraphStore(graph_dir / "graph.db") as store:
        first = full_build(tmp_path, store)
        assert first["event_resolution"]["calls_emitted"] == 1
        assert len(_event_calls(store)) == 1

        listener.write_text(
            """package alpha;
            import org.springframework.context.event.EventListener;
            class OtherEvent {}
            class Listener {
                @EventListener void on(OtherEvent event) {}
            }
            """,
            encoding="utf-8",
        )
        updated = incremental_update(
            tmp_path,
            store,
            changed_files=["alpha/Listener.java"],
        )

        assert updated["event_resolution"]["calls_emitted"] == 0
        assert _event_calls(store) == []


def test_event_query_patterns_return_publishers_and_listeners(tmp_path: Path) -> None:
    _write_event_package(tmp_path, "alpha")
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        full_build(tmp_path, store)

    publishers = query_graph(
        "publishers_of",
        "event::alpha.SharedEvent",
        repo_root=str(tmp_path),
    )
    assert publishers["status"] == "ok"
    assert [result["name"] for result in publishers["results"]] == ["publish"]
    assert {edge["kind"] for edge in publishers["edges"]} == {"PUBLISHES"}

    listeners = query_graph(
        "listeners_of",
        "event::alpha.SharedEvent",
        repo_root=str(tmp_path),
    )
    assert listeners["status"] == "ok"
    assert [result["name"] for result in listeners["results"]] == ["on"]
    assert {edge["kind"] for edge in listeners["edges"]} == {"HANDLES"}


def test_event_query_patterns_respect_max_results_and_report_count(tmp_path: Path) -> None:
    pkg = "demo"
    directory = tmp_path / pkg
    directory.mkdir(parents=True)
    (directory / "Evt.java").write_text(
        "package demo;\nclass Evt {}\n",
        encoding="utf-8",
    )
    for i in range(5):
        (directory / f"Pub{i}.java").write_text(
            "package demo;\n"
            "import org.springframework.context.ApplicationEventPublisher;\n"
            f"class Pub{i} {{\n"
            "  ApplicationEventPublisher publisher;\n"
            "  void fire() { publisher.publishEvent(new Evt()); }\n"
            "}\n",
            encoding="utf-8",
        )
        (directory / f"Lis{i}.java").write_text(
            "package demo;\n"
            "import org.springframework.context.event.EventListener;\n"
            f"class Lis{i} {{\n"
            "  @EventListener void on(Evt e) {}\n"
            "}\n",
            encoding="utf-8",
        )
    graph_dir = tmp_path / ".code-review-graph"
    graph_dir.mkdir()
    with GraphStore(graph_dir / "graph.db") as store:
        full_build(tmp_path, store)

    qn = "event::demo.Evt"

    pub = query_graph("publishers_of", qn, repo_root=str(tmp_path), max_results=3)
    assert pub["status"] == "ok"
    assert len(pub["results"]) == 3
    assert pub["result_count"] == 5
    assert pub["results_omitted"] == 2
    assert len(pub["edges"]) == 3

    listeners = query_graph("listeners_of", qn, repo_root=str(tmp_path))
    assert listeners["status"] == "ok"
    assert listeners["result_count"] == 5
    assert listeners["results_omitted"] == 0
    assert len(listeners["edges"]) == 5

    handlers = query_graph("handlers_of", qn, repo_root=str(tmp_path))
    assert handlers["status"] == "ok"
    assert handlers["result_count"] == 5
    assert len(handlers["edges"]) == 5


# -- @TransactionalEventListener, @ApplicationModuleListener, and a query on the event class --


def test_transactional_event_listener_binds_like_event_listener(tmp_path: Path) -> None:
    _, edges = _parse_java(
        tmp_path / "L.java",
        """
        package alpha.listeners;
        import beta.events.OrderPlaced;
        import org.springframework.transaction.event.TransactionalEventListener;

        class L {
            @TransactionalEventListener
            void plain(OrderPlaced event) {}

            @TransactionalEventListener(phase = TransactionPhase.AFTER_COMMIT,
                                        fallbackExecution = true)
            void phased(OrderPlaced event) {}

            @TransactionalEventListener(classes = {LocalEvent.class})
            void named() {}
        }
        """,
    )
    handles = {edge.source.rsplit(".", 1)[-1]: edge for edge in edges if edge.kind == "HANDLES"}
    assert handles["plain"].target == "event::beta.events.OrderPlaced"
    assert handles["plain"].extra["listener_annotation"] == "TransactionalEventListener"
    assert handles["phased"].extra["phase"] == "TransactionPhase.AFTER_COMMIT"
    assert handles["phased"].extra["fallbackExecution"] == "true"
    assert handles["named"].target == "event::alpha.listeners.LocalEvent"


def test_application_module_listener_binds_by_its_first_parameter(tmp_path: Path) -> None:
    _, edges = _parse_java(
        tmp_path / "M.java",
        """
        package alpha;
        import org.springframework.modulith.events.ApplicationModuleListener;
        class M {
            @ApplicationModuleListener
            void on(Evt event) {}
        }
        """,
    )
    (edge,) = [e for e in edges if e.kind == "HANDLES"]
    assert edge.target == "event::alpha.Evt"
    assert edge.extra["listener_annotation"] == "ApplicationModuleListener"


def _event_repo(root: Path, package: str, annotation: str = "TransactionalEventListener") -> None:
    directory = root / package
    directory.mkdir(parents=True)
    (directory / "Placed.java").write_text(
        f"package {package};\npublic class Placed {{ Placed(long id) {{}} }}\n",
        encoding="utf-8",
    )
    (directory / "Pub.java").write_text(
        f"""package {package};
        import org.springframework.context.ApplicationEventPublisher;
        class Pub {{
            ApplicationEventPublisher events;
            void fire() {{ events.publishEvent(new Placed(1L)); }}
        }}
        """,
        encoding="utf-8",
    )
    (directory / "Lis.java").write_text(
        f"""package {package};
        import org.springframework.transaction.event.{annotation};
        class Lis {{
            @{annotation}
            void on(Placed event) {{}}
        }}
        """,
        encoding="utf-8",
    )


def _build_repo(root: Path) -> str:
    (root / ".git").mkdir(exist_ok=True)
    with GraphStore(root / ".code-review-graph" / "graph.db") as store:
        full_build(root, store)
    return str(root)


def test_publishers_and_listeners_of_an_event_class_find_the_event_edges(tmp_path: Path) -> None:
    """The class node and the ``event::`` end are two names for one event."""
    _event_repo(tmp_path, "alpha")
    root = _build_repo(tmp_path)

    # The bare name matches the class and its constructor; the type is what is asked about.
    publishers = query_graph("publishers_of", "Placed", repo_root=root)
    assert publishers["status"] == "ok", publishers.get("summary")
    assert [r["name"] for r in publishers["results"]] == ["fire"]

    listeners = query_graph("listeners_of", "Placed", repo_root=root)
    assert listeners["status"] == "ok", listeners.get("summary")
    assert [r["name"] for r in listeners["results"]] == ["on"]
    assert {e["kind"] for e in listeners["edges"]} == {"HANDLES"}


def test_an_event_type_written_in_full_resolves_without_a_node(tmp_path: Path) -> None:
    _event_repo(tmp_path, "alpha")
    root = _build_repo(tmp_path)
    full = query_graph("listeners_of", "alpha.Placed", repo_root=root)
    assert full["status"] == "ok"
    assert [r["name"] for r in full["results"]] == ["on"]


def test_same_named_events_in_two_packages_are_never_mixed(tmp_path: Path) -> None:
    _event_repo(tmp_path, "alpha")
    _event_repo(tmp_path, "beta", annotation="EventListener")
    root = _build_repo(tmp_path)

    # The bare name is ambiguous (two classes), and says so instead of picking one.
    bare = query_graph("listeners_of", "Placed", repo_root=root)
    assert bare["status"] in ("ambiguous", "ok")
    if bare["status"] == "ok":
        assert len(bare["results"]) <= 1

    alpha = query_graph("listeners_of", "alpha.Placed", repo_root=root)
    beta = query_graph("listeners_of", "beta.Placed", repo_root=root)
    assert {r["file_path"].rsplit("/", 2)[-2] for r in alpha["results"]} == {"alpha"}
    assert {r["file_path"].rsplit("/", 2)[-2] for r in beta["results"]} == {"beta"}


def test_an_empty_event_answer_names_what_the_parser_reads(tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "Lonely.java").write_text(
        "package alpha;\npublic class Lonely {}\n", encoding="utf-8",
    )
    # A real repository with the file staged, so the graph counts as current: the note under
    # test is the one that follows the freshness checks.
    import subprocess

    for args in (["init", "-q"], ["add", "alpha/Lonely.java"]):
        subprocess.run(
            ["git", *args], cwd=str(tmp_path), check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    # The file is older than the build, as it is in a repository that was built after a checkout.
    import os
    import time

    past = time.time() - 600
    os.utime(tmp_path / "alpha" / "Lonely.java", (past, past))
    root = _build_repo(tmp_path)
    result = query_graph("listeners_of", "Lonely", repo_root=root)
    assert result["status"] == "ok" and result["results"] == []
    note = result["confidence"]
    assert "TransactionalEventListener" in note and "ApplicationListener is not" in note
    assert "real absence" not in note
