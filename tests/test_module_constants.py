"""URL allow-lists held in Python module constants (``module_constants``).

``ALLOWED_REFRESH_URLS = frozenset({...})`` decides where a lambda may send a token, but it is
neither a function nor a call. It becomes a ``Variable`` node with its URLs in
``extra.url_values``, and the functions that use it get ``REFERENCES`` edges to it.
"""

from __future__ import annotations

from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser
from code_review_graph.tools.query import query_graph

LAMBDA = """\
import os

ALLOWED_SIGNIN_URLS = frozenset({'https://signin.example.net', 'https://signin.example.com'})
ALLOWED_REFRESH_URLS = ('https://api.example.net/v2/refresh',)
SINGLE_URL = "https://one.example.net/x"
BY_ENV = {'qa': 'https://qa.example.net/a', 'prod': 'https://prod.example.net/a'}

# Not kept: a secret-looking name, credentials in the URL, a query string, a non-URL, lowercase.
SIGNIN_TOKEN_URLS = ['https://tok.example.net/t']
WITH_USERINFO = ['https://user:pw@host.example.net/x']
WITH_QUERY = ['https://host.example.net/x?token=abc']
NOT_URLS = ['plain', 'ftp://host/x']
lower_urls = ['https://lower.example.net']
FSTRING = [f'https://{os.environ["H"]}/x']


def _check_url(url, allowed_urls, field):
    if url not in allowed_urls:
        raise ValueError(field)


def handler(event):
    _check_url(event['signin'], ALLOWED_SIGNIN_URLS, 'signin')
    _check_url(event['refresh'], ALLOWED_REFRESH_URLS, 'refresh')


def shadowed(ALLOWED_SIGNIN_URLS):
    return ALLOWED_SIGNIN_URLS


def rebinds():
    ALLOWED_REFRESH_URLS = ['x']
    return ALLOWED_REFRESH_URLS
"""


def _parse(source: str, name: str = "lambda_fn.py"):
    return CodeParser().parse_bytes(Path("/work/svc") / name, source.encode())


def _constants(source: str = LAMBDA) -> dict[str, list[str]]:
    nodes, _ = _parse(source)
    return {n.name: n.extra["url_values"] for n in nodes if n.kind == "Variable"}


def test_url_constants_keep_their_values() -> None:
    assert _constants() == {
        "ALLOWED_SIGNIN_URLS": ["https://signin.example.net", "https://signin.example.com"],
        "ALLOWED_REFRESH_URLS": ["https://api.example.net/v2/refresh"],
        "SINGLE_URL": ["https://one.example.net/x"],
        "BY_ENV": ["https://qa.example.net/a", "https://prod.example.net/a"],
    }


def test_secrets_credentials_queries_and_non_urls_are_left_out() -> None:
    kept = _constants()
    for name in ("SIGNIN_TOKEN_URLS", "WITH_USERINFO", "WITH_QUERY", "NOT_URLS", "lower_urls",
                 "FSTRING"):
        assert name not in kept
    flat = repr(kept)
    assert "user:pw" not in flat and "token=abc" not in flat


def test_the_node_is_marked_a_constant_and_contained_in_the_file() -> None:
    nodes, edges = _parse(LAMBDA)
    node = next(n for n in nodes if n.name == "ALLOWED_SIGNIN_URLS")
    assert node.extra["constant"] is True and node.extra["value_count"] == 2
    assert node.language == "python" and node.line_start == 3
    file_path = next(n.file_path for n in nodes if n.kind == "File")
    assert any(
        e.kind == "CONTAINS" and e.source == file_path
        and e.target == f"{file_path}::ALLOWED_SIGNIN_URLS"
        for e in edges
    )


def test_a_function_that_uses_a_constant_references_it() -> None:
    _, edges = _parse(LAMBDA)
    refs = {
        (e.source.split("::")[-1], e.target.split("::")[-1], e.line)
        for e in edges
        if e.kind == "REFERENCES" and e.extra.get("resolution") == "module_constant"
    }
    assert refs == {
        ("handler", "ALLOWED_SIGNIN_URLS", 23),
        ("handler", "ALLOWED_REFRESH_URLS", 24),
    }


def test_a_parameter_or_local_of_the_same_name_is_not_the_constant() -> None:
    _, edges = _parse(LAMBDA)
    sources = {
        e.source.split("::")[-1]
        for e in edges
        if e.kind == "REFERENCES" and e.extra.get("resolution") == "module_constant"
    }
    assert "shadowed" not in sources and "rebinds" not in sources


def test_a_file_without_urls_or_a_test_file_adds_nothing() -> None:
    assert _constants("X = 1\nNAMES = ['a', 'b']\n") == {}
    nodes, _ = _parse(LAMBDA, name="tests/test_lambda.py")
    assert not any(n.kind == "Variable" for n in nodes)


def test_at_most_twenty_urls_per_constant_are_kept() -> None:
    many = "URLS = [" + ", ".join(f"'https://h{i}.example.net'" for i in range(60)) + "]\n"
    nodes, _ = _parse(many)
    (node,) = [n for n in nodes if n.kind == "Variable"]
    assert len(node.extra["url_values"]) == 20 and node.extra["truncated"] is True


def test_references_to_a_constant_finds_the_code_it_governs(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    folder = tmp_path / "python-lambdas" / "start"
    folder.mkdir(parents=True)
    (folder / "start.py").write_text(LAMBDA, encoding="utf-8")
    with GraphStore(tmp_path / ".code-review-graph" / "graph.db") as store:
        full_build(tmp_path, store)
    result = query_graph("references_to", "ALLOWED_REFRESH_URLS", repo_root=str(tmp_path))
    assert result["status"] == "ok", result.get("summary")
    assert [r["name"] for r in result["results"]] == ["handler"]
    found = query_graph("file_summary", str(folder / "start.py"), repo_root=str(tmp_path))
    variable = next(r for r in found["results"] if r["name"] == "ALLOWED_REFRESH_URLS")
    assert variable["kind"] == "Variable"
