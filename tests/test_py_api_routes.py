"""FastAPI and Flask routes become server endpoints, with prefixes joined across files."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_review_graph import py_api_extract as pae
from code_review_graph import py_route_prefixes as prefixes
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


# ---------------------------------------------------------------------------
# One file
# ---------------------------------------------------------------------------


def test_fastapi_verbs_and_router_prefix(tmp_path):
    nodes, edges = parse(tmp_path, "svc/routes.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/agent", tags=["x"])

@router.get("/list")
async def list_all(): ...

@router.post(
    "/{name}/invoke",
    response_model=dict,
)
def invoke(name: str): ...

@router.delete("")
def drop(): ...
''')
    assert routes(nodes) == [
        ("DELETE", "/agent"), ("GET", "/agent/list"), ("POST", "/agent/{name}/invoke"),
    ]
    handles = [e for e in edges if e.kind == "HANDLES"]
    assert {e.source.rsplit("::", 1)[1] for e in handles} == {"list_all", "invoke", "drop"}
    assert all(e.extra["framework"] == "fastapi" for e in handles)


def test_flask_route_methods_converters_and_blueprint_prefix(tmp_path):
    nodes, _ = parse(tmp_path, "web/views.py", '''
from flask import Blueprint, Flask
app = Flask(__name__)
bp = Blueprint("items", __name__, url_prefix="/v1/items")

@app.route("/health")
def health(): ...

@bp.route("/<int:item_id>", methods=["GET", "PUT"])
def item(item_id): ...

@bp.get("/search")
def search(): ...

@bp.route("/files/<path:name>", methods=["GET"])
def files(name): ...
''')
    assert routes(nodes) == [
        ("GET", "/health"), ("GET", "/v1/items/files/{...name}"),
        ("GET", "/v1/items/search"), ("GET", "/v1/items/{item_id}"),
        ("PUT", "/v1/items/{item_id}"),
    ]
    catch = [n for n in nodes if n.extra.get("catch_all")]
    assert [n.extra["route"] for n in catch] == ["/v1/items/files/{...name}"]


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
from fastapi import APIRouter
BASE = "/x"
router = APIRouter(prefix=BASE)
other = APIRouter(prefix="/ok")

@router.get("/a")
def a(): ...

@other.get(f"/{BASE}/b")
def b(): ...

@other.get(BASE)
def c(): ...

@other.get("/fine")
def d(): ...
''')
    found = [n for n in nodes if n.kind == "Endpoint" and n.extra.get("py_route")]
    assert [(n.extra["route"], bool(n.extra.get("prefix_dynamic"))) for n in found] == [
        ("/a", True), ("/ok/fine", False),
    ]


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


def test_convert_path():
    assert pae.convert_path("/a/<int:id>/b") == ("/a/{id}/b", False)
    assert pae.convert_path("/a/<id>") == ("/a/{id}", False)
    assert pae.convert_path("/f/<path:p>") == ("/f/{...p}", True)
    assert pae.convert_path("/f/{p:path}") == ("/f/{...p}", True)
    assert pae.convert_path("/f/<path:p>/x") == ("/f/{p}/x", False)


# ---------------------------------------------------------------------------
# Prefixes across files
# ---------------------------------------------------------------------------

MAIN = '''
from fastapi import APIRouter, FastAPI
from src.app.routes.agent_stream import router as agent_stream_router
from .routes import agent_testing

app = FastAPI()
base_router_v1 = APIRouter(prefix="/v1/constellation-agents", tags=["v1"])
base_router_v1.include_router(agent_stream_router)
base_router_v1.include_router(agent_testing.router)
app.include_router(base_router_v1)
'''

STREAM = '''
from fastapi import APIRouter
router = APIRouter(prefix="/agent/stream")

@router.get("/{session_id}")
def stream(session_id: str): ...

@router.post("/start")
def start(): ...
'''

TESTING = '''
from fastapi import APIRouter
router = APIRouter()

@router.get("/list")
def list_agents(): ...
'''


def agent_repo(tmp_path, registry):
    return make_repo(tmp_path, registry, "agent", {
        "src/main.py": MAIN,
        "src/app/routes/agent_stream.py": STREAM,
        "src/app/routes/agent_testing.py": TESTING,
        "src/routes/__init__.py": "",
    })


def test_prefixes_are_joined_through_include_router_chains(tmp_path, registry):
    make_repo(tmp_path, registry, "ui", {
        "lib/a.ts": (
            "import axios from 'axios';\n"
            "export const a = (id: string) =>"
            " axios.get(`/v1/constellation-agents/agent/stream/${id}`);\n"
            "export const b = () => axios.post('/v1/constellation-agents/agent/stream/start');\n"
            "export const wrong = () => axios.get('/agent/stream/x');\n"
        ),
    })
    agent_repo(tmp_path, registry)
    result = cross_repo_requests_func(repos=["ui"], origin="js")
    by_line = {r["client_line"]: r for r in result["results"]}
    assert by_line[2]["status"] == "matched"
    assert by_line[2]["candidates"][0]["repo"] == "agent"
    assert by_line[2]["candidates"][0]["route"] == (
        "/v1/constellation-agents/agent/stream/{session_id}"
    )
    assert by_line[3]["status"] == "matched"
    # The un-prefixed path is the router's own, not a served route.
    assert by_line[4]["status"] == "no_route_match"


def test_an_empty_route_path_is_the_router_prefix_itself(tmp_path, registry):
    """``@router.post("")`` under ``APIRouter(prefix=...)`` must not repeat the prefix."""
    make_repo(tmp_path, registry, "agent", {
        "main.py": (
            "from fastapi import APIRouter, FastAPI\n"
            "from segment import router as segment_router\n"
            "app = FastAPI()\n"
            "base = APIRouter(prefix='/v1/agents')\n"
            "base.include_router(segment_router)\n"
            "app.include_router(base)\n"
        ),
        "segment.py": (
            "from fastapi import APIRouter\n"
            "router = APIRouter(prefix='/agent/segment')\n"
            "@router.post('')\n"
            "def run(): ...\n"
        ),
    })
    make_repo(tmp_path, registry, "ui", {
        "a.ts": (
            "import axios from 'axios';\n"
            "export const a = () => axios.post('/v1/agents/agent/segment');\n"
        ),
    })
    (row,) = cross_repo_requests_func(repos=["ui"])["results"]
    assert row["status"] == "matched"
    assert row["candidates"][0]["route"] == "/v1/agents/agent/segment"


def test_a_mount_with_a_dynamic_prefix_leaves_the_route_out(tmp_path, registry):
    make_repo(tmp_path, registry, "agent", {
        "main.py": (
            "from fastapi import FastAPI\n"
            "from routes import router\n"
            "app = FastAPI()\n"
            "PREFIX = '/x'\n"
            "app.include_router(router, prefix=PREFIX)\n"
        ),
        "routes.py": (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/thing')\n"
            "def thing(): ...\n"
        ),
    })
    make_repo(tmp_path, registry, "ui", {
        "a.ts": "import axios from 'axios';\nexport const a = () => axios.get('/thing');\n",
    })
    (row,) = cross_repo_requests_func(repos=["ui"])["results"]
    assert row["status"] == "no_route_match"


def test_a_router_mounted_twice_gives_a_route_per_mount(tmp_path, registry):
    make_repo(tmp_path, registry, "agent", {
        "main.py": (
            "from fastapi import FastAPI\n"
            "from routes import router\n"
            "app = FastAPI()\n"
            "app.include_router(router, prefix='/v1')\n"
            "app.include_router(router, prefix='/v2')\n"
        ),
        "routes.py": (
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/thing')\n"
            "def thing(): ...\n"
        ),
    })
    make_repo(tmp_path, registry, "ui", {
        "a.ts": (
            "import axios from 'axios';\n"
            "export const a = () => axios.get('/v1/thing');\n"
            "export const b = () => axios.get('/v2/thing');\n"
        ),
    })
    statuses = [r["status"] for r in cross_repo_requests_func(repos=["ui"])["results"]]
    assert statuses == ["matched", "matched"]


def test_flask_blueprint_registered_with_a_url_prefix(tmp_path, registry):
    make_repo(tmp_path, registry, "api", {
        "app.py": (
            "from flask import Flask\n"
            "from views import bp\n"
            "app = Flask(__name__)\n"
            "app.register_blueprint(bp, url_prefix='/api')\n"
        ),
        "views.py": (
            "from flask import Blueprint\n"
            "bp = Blueprint('v', __name__, url_prefix='/items')\n"
            "@bp.route('/<int:i>', methods=['DELETE'])\n"
            "def remove(i): ...\n"
        ),
    })
    make_repo(tmp_path, registry, "ui", {
        "a.ts": (
            "import axios from 'axios';\n"
            "export const a = (i: number) => axios.delete(`/api/items/${i}`);\n"
        ),
    })
    (row,) = cross_repo_requests_func(repos=["ui"])["results"]
    assert row["status"] == "matched"
    assert row["candidates"][0]["route"] == "/api/items/{i}"


def test_resolver_stops_on_a_mount_cycle():
    a = prefixes.RouterMap(
        "a.py", {"ra": ["fastapi", "/a"]},
        [{"parent": "ra", "child": {"var": "rb"}, "prefix": ""}],
        {"rb": ["b", "rb", 0]},
    )
    b = prefixes.RouterMap(
        "b.py", {"rb": ["fastapi", "/b"]},
        [{"parent": "rb", "child": {"var": "ra"}, "prefix": ""}],
        {"ra": ["a", "ra", 0]},
    )
    resolver = prefixes.PrefixResolver({"a.py": a, "b.py": b}, ["a.py", "b.py"])
    assert resolver.prefixes("a.py", "ra") == [None]


def test_tool_returns_unresolved_marker_unescaped(tmp_path, registry):
    """The server sends ``<unresolved>`` as is; only a transport layer may escape it."""
    make_repo(tmp_path, registry, "ui", {
        "a.ts": "import axios from 'axios';\nexport const a = (u: string) => axios.get(u);\n",
    })
    (row,) = cross_repo_requests_func(repos=["ui"])["results"]
    assert row["status"] == "unmatchable"
    assert row["route"] == "<unresolved>"


def test_resolver_work_is_bounded_on_a_fan_out_of_mounts():
    """Many routers mounted in many places must not blow up the join."""
    width, depth = 6, 12
    maps = {}
    for level in range(depth):
        file = f"l{level}.py"
        routers = {f"r{k}": ["fastapi", f"/p{level}{k}"] for k in range(width)}
        mounts = []
        if level + 1 < depth:
            for k in range(width):
                for j in range(width):
                    mounts.append({
                        "parent": f"r{k}",
                        "child": {"obj": "nxt", "attr": f"r{j}"},
                        "prefix": f"/m{k}{j}",
                    })
        maps[file] = prefixes.RouterMap(
            file, routers, mounts, {"nxt": [f"l{level + 1}", None, 0]},
        )
    resolver = prefixes.PrefixResolver(maps, list(maps))
    result = resolver.prefixes(f"l{depth - 1}.py", "r0")
    assert len(result) <= prefixes.MAX_VARIANTS
    assert resolver._visits <= 2_000


def test_resolver_memoises_a_shared_parent():
    shared = prefixes.RouterMap(
        "top.py", {"top": ["fastapi", "/v1"]},
        [
            {"parent": "top", "child": {"var": "mid"}, "prefix": "/a"},
            {"parent": "top", "child": {"var": "mid"}, "prefix": "/a"},
        ],
        {},
    )
    mid = prefixes.RouterMap("top.py", {"top": ["fastapi", "/v1"], "mid": ["fastapi", ""]},
                             shared.mounts, {})
    resolver = prefixes.PrefixResolver({"top.py": mid}, ["top.py"])
    assert resolver.prefixes("top.py", "mid") == ["/v1/a"]
    assert resolver.parents[("top.py", "mid")] == [("top.py", "top", "/a")]
