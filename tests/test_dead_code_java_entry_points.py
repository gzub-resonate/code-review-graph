"""Dead code: Java methods and tables a framework or migration runner drives."""

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.refactor import find_dead_code


def _dead(tmp_path: Path, files: dict[str, str]) -> set[str]:
    repo = tmp_path / "repo"
    paths = []
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    (repo / ".git").mkdir(exist_ok=True)
    parser = CodeParser(repo)
    store = GraphStore(tmp_path / "graph.db")
    try:
        for path in paths:
            nodes, edges = parser.parse_file(path)
            store.store_file_nodes_edges(str(path), nodes, edges, "")
        return {d["name"] for d in find_dead_code(store, root=repo)}
    finally:
        store.close()

CLI = """package a;
import picocli.CommandLine.Command;
@Command(name = "x")
public class ConnectCommand implements Runnable {
  public void connect() {}
  public void run() {}
}
class Plain {
  public void orphan() {}
}
"""

ADVICE = """package a;
@ControllerAdvice
class VendorApiExceptionHandler {
  @ExceptionHandler(IllegalStateException.class)
  public String handleUnsupported(IllegalStateException e) { return ""; }
  public String helperInAdvice() { return ""; }
}
class Beans {
  @PostConstruct void init() {}
  @PreDestroy void close() {}
  @RabbitListener(queues = "q") void onRabbit(String m) {}
  @JmsListener(destination = "d") void onJms(String m) {}
  void neverCalled() {}
}
"""


def test_picocli_command_class_members_are_reachable(tmp_path):
    dead = _dead(tmp_path, {"ConnectCommand.java": CLI})
    assert "connect" not in dead
    assert "ConnectCommand" not in dead
    assert "orphan" in dead


def test_spring_handlers_and_lifecycle_callbacks_are_reachable(tmp_path):
    dead = _dead(tmp_path, {"Advice.java": ADVICE})
    for name in ("handleUnsupported", "helperInAdvice", "init", "close", "onRabbit", "onJms"):
        assert name not in dead
    assert "neverCalled" in dead


def test_migration_tables_are_not_dead_but_other_tables_stay(tmp_path):
    ddl = "CREATE TABLE vendors (id int);\n"
    dead = _dead(tmp_path, {
        "db/migration/V1__init.sql": ddl,
        "schema/other.sql": "CREATE TABLE loose (id int);\n",
    })
    assert "vendors" not in dead
    assert "loose" in dead
