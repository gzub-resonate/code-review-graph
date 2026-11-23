"""Routes on a FastAPI or Flask application become server endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import py_api_extract as pae
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.registry import Registry
from code_review_graph.tools.registry_tools import cross_repo_requests_func


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def parse(root: Path, rel: str, text: str):
    return CodeParser(root).parse_file(write(root, rel, text))


def routes(nodes):
    return sorted(
        (n.extra["http_method"], n.extra["route"])
        for n in nodes if n.kind == "Endpoint" and n.extra.get("py_route")
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


def make_repo(tmp_path, registry, name, files):
    root = (tmp_path / name).resolve()
    for rel, text in files.items():
        write(root, rel, text)
    directory = root / ".code-review-graph"
    directory.mkdir(parents=True, exist_ok=True)
    parser = CodeParser(root)
    with GraphStore(directory / "graph.db") as store:
        for rel in files:
            nodes, edges = parser.parse_file(root / rel)
            store.store_file_nodes_edges(str(root / rel), nodes, edges)
    registry.register(str(root), alias=name)
    return root


def test_fastapi_app_verbs(tmp_path):
    nodes, edges = parse(tmp_path, "svc/main.py", '''
from fastapi import FastAPI
app = FastAPI(title="x")

@app.get("/list")
async def list_all(): ...

@app.post(
    "/{name}/invoke",
    response_model=dict,
)
def invoke(name: str): ...

@app.delete("/items/")
def drop(): ...
''')
    assert routes(nodes) == [
        ("DELETE", "/items"), ("GET", "/list"), ("POST", "/{name}/invoke"),
    ]
    handles = [e for e in edges if e.kind == "HANDLES"]
    assert {e.source.rsplit("::", 1)[1] for e in handles} == {"list_all", "invoke", "drop"}
    assert all(e.extra["framework"] == "fastapi" for e in handles)


def test_flask_app_route_methods_and_converters(tmp_path):
    nodes, _ = parse(tmp_path, "web/views.py", '''
from flask import Flask
app = Flask(__name__)

@app.route("/health")
def health(): ...

@app.route("/items/<int:item_id>", methods=["GET", "PUT"])
def item(item_id): ...

@app.get("/search")
def search(): ...

@app.route("/files/<path:name>", methods=["GET"])
def files(name): ...
''')
    assert routes(nodes) == [
        ("GET", "/files/{...name}"), ("GET", "/health"), ("GET", "/items/{item_id}"),
        ("GET", "/search"), ("PUT", "/items/{item_id}"),
    ]
    catch = [n for n in nodes if n.extra.get("catch_all")]
    assert [n.extra["route"] for n in catch] == ["/files/{...name}"]
    assert all(n.extra["framework"] == "flask" for n in nodes if n.extra.get("py_route"))


def test_api_route_methods(tmp_path):
    nodes, _ = parse(tmp_path, "svc/multi.py", '''
from fastapi import FastAPI
app = FastAPI()

@app.api_route("/both", methods=["GET", "POST"])
def both(): ...

@app.api_route("/default")
def default(): ...
''')
    assert routes(nodes) == [
        ("GET", "/both"), ("GET", "/default"), ("POST", "/both"),
    ]


def test_routes_that_are_not_literal_are_not_guessed(tmp_path):
    nodes, _ = parse(tmp_path, "svc/dyn.py", '''
from fastapi import FastAPI
BASE = "/x"
app = FastAPI()

@app.get(BASE)
def a(): ...

@app.get(f"/{BASE}/b")
def b(): ...

@app.api_route("/c", methods=METHODS)
def c(): ...

@app.get("/fine")
def d(): ...
''')
    assert routes(nodes) == [("GET", "/fine")]


def test_routes_that_need_a_prefix_from_elsewhere_are_left_out(tmp_path):
    """A router's own path is not a served route until its prefix is known."""
    nodes, _ = parse(tmp_path, "svc/routes.py", '''
from fastapi import APIRouter
from flask import Blueprint
router = APIRouter(prefix="/agent")
bp = Blueprint("items", __name__, url_prefix="/v1/items")

@router.get("/list")
def a(): ...

@bp.route("/search")
def b(): ...
''')
    assert routes(nodes) == []
    nodes, _ = parse(tmp_path, "svc/other.py", '''
from main import app

@app.get("/imported")
def c(): ...
''')
    assert routes(nodes) == []


def test_only_an_application_built_in_the_file_counts(tmp_path):
    nodes, _ = parse(tmp_path, "svc/two.py", '''
from fastapi import APIRouter, FastAPI
app = FastAPI()
router = APIRouter()

@app.get("/on-the-app")
def a(): ...

@router.get("/on-the-router")
def b(): ...
''')
    assert routes(nodes) == [("GET", "/on-the-app")]


def test_test_files_and_lookalike_decorators_are_ignored(tmp_path):
    nodes, _ = parse(tmp_path, "tests/test_api.py", '''
from fastapi import FastAPI
app = FastAPI()
@app.get("/only-in-tests")
def t(): ...
''')
    assert routes(nodes) == []
    nodes, _ = parse(tmp_path, "svc/other.py", '''
import mock
@mock.get("/nope")
def f(): ...
cache = {}
@cache.get("/nope")
def g(): ...
''')
    assert routes(nodes) == []


def test_convert_path_and_join_path():
    assert pae.convert_path("/a/<int:id>/b") == ("/a/{id}/b", False)
    assert pae.convert_path("/a/<id>") == ("/a/{id}", False)
    assert pae.convert_path("/f/<path:p>") == ("/f/{...p}", True)
    assert pae.convert_path("/f/{p:path}") == ("/f/{...p}", True)
    assert pae.convert_path("/f/<path:p>/x") == ("/f/{p}/x", False)
    assert pae.join_path("x/") == "/x"
    assert pae.join_path("") == "/"
    assert pae.join_path("/a", "b/", "/c") == "/a/b/c"


def test_a_caller_in_another_repository_reaches_an_app_route(tmp_path, registry):
    make_repo(tmp_path, registry, "ui", {
        "lib/a.ts": (
            "import axios from 'axios';\n"
            "export const a = (id: string) => axios.get(`/orders/${id}`);\n"
            "export const b = () => axios.get('/missing');\n"
        ),
    })
    make_repo(tmp_path, registry, "orders", {
        "main.py": (
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n\n"
            "@app.get('/orders/{order_id}')\n"
            "def get_order(order_id: str): ...\n"
        ),
    })
    result = cross_repo_requests_func(repos=["ui"], origin="js")
    by_line = {r["client_line"]: r for r in result["results"]}
    assert by_line[2]["status"] == "matched"
    assert by_line[2]["candidates"][0]["repo"] == "orders"
    assert by_line[2]["candidates"][0]["route"] == "/orders/{order_id}"
    assert by_line[3]["status"] == "no_route_match"
