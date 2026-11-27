"""Python ``requests`` / ``httpx`` calls become client ``Endpoint`` nodes (host only).

The host of a literal URL is kept; a path, a query string and credentials never are, and a URL
that is not a literal keeps only the name of the variable it comes from.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from code_review_graph import external_hosts as eh
from code_review_graph.parser import CodeParser

ROOT = Path("/work/svc")
# Built from pieces so a secret scanner does not mistake the fixture for a real webhook.
SLACK = "https://hooks." + "slack.com/services/" + "/".join(
    ["T0AAAAAAA", "B0BBBBBBB", "Xx1Yy2Zz3Ww4Vv5Uu6Tt7Ss8"],
)
TOKEN_URL = "https://api.example.com/v1/items?api_key=sk-live-9f8e7d6c5b4a3f2e1d0c&x=1"
USERINFO = "https://admin:hunter2pass@internal.example.org:8443/private/path"


def clients(source: str, name: str = "app.py") -> list[Any]:
    nodes, _edges = CodeParser().parse_bytes(ROOT / name, source.encode())
    return [n for n in nodes if n.kind == "Endpoint" and n.extra.get("client") == "py_http"]


def everything(source: str) -> str:
    nodes, edges = CodeParser().parse_bytes(ROOT / "app.py", source.encode())
    keep = [
        (n.name, n.extra) for n in nodes if n.kind == "Endpoint"
    ] + [(e.target, e.extra) for e in edges if e.kind == "REQUESTS"]
    return json.dumps(keep, default=str)


def test_requests_verbs_and_request_with_literal_hosts() -> None:
    src = (
        "import requests\n"
        "def sync(i):\n"
        "    requests.get('https://API.Vendor.com/v2/items')\n"
        "    requests.post('https://www.other.io:8443/x', json={})\n"
        "    requests.request('PUT', 'https://put.example.net/a')\n"
        "    requests.request(method, 'https://dyn.example.net/a')\n"
    )
    got = {(n.extra["http_method"], n.extra["host"]) for n in clients(src)}
    assert got == {
        ("GET", "api.vendor.com"), ("POST", "other.io"), ("PUT", "put.example.net"),
        ("ANY", "dyn.example.net"),
    }
    node = clients(src)[0]
    assert node.extra["role"] == "client" and node.extra["route"] == "/"
    assert node.extra["absolute_url"] is True


def test_httpx_aliases_from_imports_and_sessions() -> None:
    src = (
        "import httpx as hx\n"
        "import requests as rq\n"
        "from requests import post as send, Session\n"
        "from httpx import AsyncClient\n"
        "s = Session()\n"
        "def f():\n"
        "    hx.get('https://a.example.com/1')\n"
        "    send('https://b.example.com/2')\n"
        "    s.delete('https://c.example.com/3')\n"
        "    with rq.Session() as t:\n"
        "        t.put('https://d.example.com/4')\n"
        "    Session().get('https://e.example.com/5')\n"
        "    c = AsyncClient(base_url='https://base.example.com')\n"
        "    c.get('/relative')\n"
    )
    got = {n.extra["host"] for n in clients(src)}
    assert got == {
        "a.example.com", "b.example.com", "c.example.com", "d.example.com", "e.example.com",
        "base.example.com",
    }
    assert {n.extra["client_kind"] for n in clients(src)} == {"httpx", "requests"}


def test_no_node_without_the_import_or_for_other_objects() -> None:
    src = (
        "def f(requests, client):\n"
        "    requests.get('https://a.example.com')\n"
        "    client.get('https://b.example.com')\n"
    )
    assert clients(src) == []
    other = (
        "import requests\n"
        "def f(cache):\n"
        "    cache.get('https://b.example.com')\n"
        "    requests.get_it('https://c.example.com')\n"
    )
    assert clients(other) == []


def test_tests_are_skipped() -> None:
    src = "import requests\nrequests.get('https://a.example.com')\n"
    assert clients(src, "tests/test_a.py") == []


def test_url_from_constant_fstring_prefix_and_variables() -> None:
    src = (
        "import requests, os\n"
        "BASE = 'https://const.example.com/api'\n"
        "def f(url, item, cfg):\n"
        "    requests.get(BASE + '/x')\n"
        "    requests.get(f'https://lit.example.com/items/{item}')\n"
        "    requests.get(url)\n"
        "    requests.get(cfg.endpoint)\n"
        "    requests.get(f'{BASE_URL}/x')\n"
        "    requests.get(os.environ['VENDOR_URL'])\n"
        "    requests.get(os.getenv('OTHER_URL') + '/p')\n"
        "    requests.get(f'https://{tenant}.example.com/x')\n"
    )
    nodes = clients(src)
    hosts = {n.extra.get("host") for n in nodes} - {None}
    assert hosts == {"const.example.com", "lit.example.com"}
    unresolved = sorted(n.extra["url_variable"] for n in nodes if n.extra.get("host_unresolved")
                        and "url_variable" in n.extra)
    assert unresolved == ["BASE_URL", "OTHER_URL", "VENDOR_URL", "cfg.endpoint", "url"]
    assert any(n.extra.get("host_unresolved") and "url_variable" not in n.extra for n in nodes)
    assert not any("tenant" in json.dumps(n.extra) for n in nodes)


def test_only_the_host_is_stored_for_secret_bearing_urls() -> None:
    src = (
        "import requests, httpx\n"
        f"HOOK = '{SLACK}'\n"
        "def f(msg, tok):\n"
        f"    requests.post('{SLACK}', json=msg)\n"
        "    requests.post(HOOK, json=msg)\n"
        f"    httpx.get('{TOKEN_URL}')\n"
        f"    requests.get('{USERINFO}')\n"
        f"    requests.get(f'{TOKEN_URL}&t={{tok}}')\n"
    )
    stored = everything(src)
    for secret in (
        "T0AAAAAAA", "B0BBBBBBB", "Xx1Yy2Zz3Ww4Vv5Uu6Tt7Ss8", "services", "sk-live", "api_key",
        "9f8e7d6c5b4a3f2e1d0c", "hunter2pass", "admin", "8443", "private", "/v1/items",
    ):
        assert secret not in stored, secret
    hosts = {n.extra["host"] for n in clients(src) if "host" in n.extra}
    assert hosts == {"hooks.slack.com", "api.example.com", "internal.example.org"}
    flagged = {n.extra["host"] for n in clients(src) if n.extra.get("sensitive_url")}
    assert flagged == {"hooks.slack.com"}
    assert all(n.extra["route"] == "/" for n in clients(src))


def test_host_that_looks_like_a_credential_is_not_stored() -> None:
    assert eh_host("https://AKIAIOSFODNN7EXAMPLE1234567890abcdef0123456789abcd.example.com/x") \
        is None
    assert eh_host("https://ok.example.com") == "ok.example.com"
    assert eh_host("ftp://ok.example.com") is None
    assert eh_host("https://bad host/x") is None


def eh_host(url: str) -> Any:
    from code_review_graph.py_http_clients import host_of

    return host_of(url)


def test_calling_function_owns_the_edge() -> None:
    src = (
        "import requests\n"
        "def notify():\n"
        "    requests.post('https://a.example.com/x')\n"
    )
    _nodes, edges = CodeParser().parse_bytes(ROOT / "app.py", src.encode())
    edge = next(e for e in edges if e.kind == "REQUESTS")
    assert edge.source == str(ROOT / "app.py") + "::notify"
    assert edge.extra["confidence"] == 1.0


def _call_rows(source: str) -> list[dict[str, Any]]:
    nodes = [n for n in CodeParser().parse_bytes(ROOT / "app.py", source.encode())[0]]
    store = SimpleNamespace(get_nodes_by_kind=lambda kinds: [
        SimpleNamespace(
            extra=n.extra, file_path=n.file_path, line_start=n.line_start, line_end=n.line_end,
        ) for n in nodes if n.kind in kinds
    ])
    return eh.read_client_calls(store, ROOT)  # type: ignore[arg-type]


def test_external_origin_groups_python_calls_by_host_and_unresolved_bucket() -> None:
    src = (
        "import requests\n"
        "def f(url):\n"
        f"    requests.post('{SLACK}')\n"
        "    requests.get('https://api.stripe.com/v1/charges')\n"
        "    requests.get('https://unknown.example.org/a')\n"
        "    requests.get(url)\n"
    )
    groups = [eh.classify_py(c) for c in _call_rows(src)]
    by_key = {g.key: g for g in groups}
    assert by_key["hooks.slack.com"].vendor == "Slack"
    assert by_key["hooks.slack.com"].status == eh.STATUS_KNOWN
    assert by_key["api.stripe.com"].source == "python_http_literal"
    assert by_key["unknown.example.org"].status == eh.STATUS_UNKNOWN
    unresolved = by_key["${url}"]
    assert unresolved.status == eh.STATUS_UNRESOLVED
    assert unresolved.source == "python_http_variable"
    assert unresolved.unresolved_key == "url"
    assert all(g.path == "/" for g in groups)
    assert "T0AAAAAAA" not in repr(groups)


# Imitates resonate/axon-gateway modules/bonusly.py (f"{CONFIG[...]}/path") and
# resonate/constellation-ai-agent src/app/client/services_api_analytics.py
# (f"{_BASE_URL}/v1/analytics" with a module-level name).


def test_fstring_with_same_module_constant_resolves_host() -> None:
    src = (
        "import requests\n"
        "BASE = 'https://api.vendor.com/v1'\n"
        "def f():\n"
        "    requests.get(f'{BASE}/users/me')\n"
    )
    node = clients(src)[0]
    assert node.extra["host"] == "api.vendor.com"
    assert "host_unresolved" not in node.extra


def test_fstring_with_unknown_name_stays_unresolved() -> None:
    src = (
        "import httpx\n"
        "_CONFIG = get_config()\n"
        "_BASE_URL = _CONFIG.services_api_analytics_domain\n"
        "def f():\n"
        "    httpx.post(f'{_BASE_URL}/v1/analytics')\n"
    )
    node = clients(src)[0]
    assert node.extra["host_unresolved"] is True
    assert node.extra["url_variable"] == "_BASE_URL"


def test_environment_default_is_not_resolved() -> None:
    src = (
        "import os, requests\n"
        "BASE = os.getenv('API_BASE', 'https://bonus.ly/api/v1')\n"
        "def f():\n"
        "    requests.get(f'{BASE}/users/me')\n"
    )
    node = clients(src)[0]
    assert node.extra["host_unresolved"] is True


def _parse_file(path: Path) -> list[Any]:
    nodes, _edges = CodeParser().parse_bytes(path, path.read_bytes())
    return [n for n in nodes if n.kind == "Endpoint" and n.extra.get("client") == "py_http"]


def test_constant_imported_from_a_sibling_module_resolves(tmp_path: Path) -> None:
    (tmp_path / "settings.py").write_text("VENDOR_URL = 'https://api.vendor.com/v2'\n")
    app = tmp_path / "client.py"
    app.write_text(
        "import requests\nfrom settings import VENDOR_URL\n"
        "def f():\n    requests.get(f'{VENDOR_URL}/items')\n    requests.get(VENDOR_URL)\n",
    )
    assert {n.extra.get("host") for n in _parse_file(app)} == {"api.vendor.com"}


def test_relative_import_and_reassigned_constant(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "conf.py").write_text(
        "A = 'https://a.example.net'\nB = 'https://b.example.net'\nB = 'https://c.example.net'\n",
    )
    app = pkg / "client.py"
    app.write_text(
        "import httpx\nfrom .conf import A, B\n"
        "def f():\n    httpx.get(f'{A}/x')\n    httpx.get(f'{B}/x')\n",
    )
    hosts = [n.extra.get("host") for n in _parse_file(app)]
    assert hosts == ["a.example.net", None]
