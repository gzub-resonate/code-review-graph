"""End-to-end (``e2e``) paths and files are test code, like ``tests/``."""

from pathlib import Path

import pytest

from code_review_graph.communities import _is_test_file as community_is_test_file
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build
from code_review_graph.parser import CodeParser, is_test_file
from code_review_graph.refactor import find_dead_code

ROOT = Path("/repo")

MATCHING = [
    "e2e/a.ts",
    "E2E/a.ts",
    "apps/web/e2e/pages/LoginPage.ts",
    "e2e-tests/a.ts",
    "e2e_tests/a.py",
    "end-to-end/a.ts",
    "end_to_end/a.py",
    "end2end/a.ts",
    "tests/e2e/a.ts",
    "src/foo.e2e.ts",
    "src/foo.e2e.tsx",
    "src/foo.e2e-spec.ts",
    "src/foo.e2e-test.ts",
    "src/foo.e2e_spec.js",
    "src/foo_e2e.py",
    "src/foo_e2e_test.go",
    "src/login-e2e-flow.spec.ts",
]

NOT_MATCHING = [
    "e2e-utils-lib/x.ts",
    "src/me2eng.ts",
    "src/e2e.ts",
    "src/xe2e/a.ts",
    "src/latest2e/a.ts",
    "playwright/a.ts",
    "cypress/a.ts",
    "integration/a.ts",
    "src/utils/LoginPage.ts",
    "src/foo_e2e.ts",
]


@pytest.mark.parametrize("rel", MATCHING)
def test_e2e_paths_and_files_are_test_files(rel):
    assert is_test_file(ROOT / rel, ROOT)
    assert is_test_file(rel)  # filename conventions hold without a root


@pytest.mark.parametrize("rel", [m for m in MATCHING if "/" in m and not m.startswith("src/")])
def test_e2e_directories_need_the_repo_root_for_absolute_paths(rel):
    # A checkout living under a directory named e2e must not flip production code.
    assert not is_test_file(Path("/work/e2e/proj") / "src/app.ts", Path("/work/e2e/proj"))


@pytest.mark.parametrize("rel", NOT_MATCHING)
def test_similar_names_are_not_test_files(rel):
    assert not is_test_file(ROOT / rel, ROOT)


@pytest.mark.parametrize("rel", MATCHING)
def test_community_heuristic_agrees(rel):
    assert community_is_test_file(rel)


@pytest.mark.parametrize("rel", NOT_MATCHING)
def test_community_heuristic_rejects_lookalikes(rel):
    assert not community_is_test_file(rel)


def test_every_node_in_an_e2e_file_is_test_flagged():
    page = b"export class LoginPage { async getName() { return 1; } helper() { return 2; } }"
    nodes, _ = CodeParser(ROOT).parse_bytes(ROOT / "e2e/pages/LoginPage.ts", page)
    assert nodes and all(n.is_test for n in nodes)
    other, _ = CodeParser(ROOT).parse_bytes(ROOT / "src/utils/LoginPage.ts", page)
    assert other and not any(n.is_test for n in other)


@pytest.fixture
def repo(tmp_path):
    files = {
        "src/auth.ts": "export function signIn(user: string) { return user; }\n",
        "src/utils/LoginPage.ts": (
            "export class LoginPage { fill() { return 1; } waitForElement() { return 2; } }\n"
        ),
        "e2e/pages/LoginPage.ts": (
            "export class LoginPage {\n"
            "  fill() { return 1; }\n"
            "  waitForElement() { return 2; }\n"
            "  getName() { return 'x'; }\n"
            "}\n"
        ),
        "e2e/login.spec.ts": (
            "import { signIn } from '../src/auth';\n"
            "import { LoginPage } from './pages/LoginPage';\n"
            "describe('login', () => {\n"
            "  it('signs in', () => {\n"
            "    const page = new LoginPage();\n"
            "    page.fill();\n"
            "    signIn('a');\n"
            "  });\n"
            "});\n"
        ),
        "e2e-utils-lib/x.ts": "export function lib() { return 1; }\n",
    }
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    with GraphStore(tmp_path / "graph.db") as store:
        full_build(tmp_path, store)
        yield tmp_path, store


def _nodes_in(store, suffix):
    rows = store._conn.execute(
        "SELECT name, kind, file_path, is_test FROM nodes WHERE file_path LIKE ?",
        (f"%{suffix}",),
    ).fetchall()
    return [dict(r) for r in rows]


def test_page_object_nodes_are_test_flagged_and_not_dead(repo):
    root, store = repo
    page_nodes = _nodes_in(store, "e2e/pages/LoginPage.ts")
    assert {n["name"] for n in page_nodes} >= {"LoginPage", "getName", "waitForElement"}
    assert all(n["is_test"] for n in page_nodes)

    dead = {(Path(n["file_path"]).as_posix(), n["name"]) for n in find_dead_code(store, root=root)}
    assert not [d for d in dead if "/e2e/" in d[0]]


def test_non_e2e_util_with_similar_name_is_not_test_flagged(repo):
    _root, store = repo
    util = _nodes_in(store, "src/utils/LoginPage.ts")
    assert util and not any(n["is_test"] for n in util)


def test_e2e_utils_lib_directory_is_not_matched(repo):
    _root, store = repo
    lib = _nodes_in(store, "e2e-utils-lib/x.ts")
    assert lib and not any(n["is_test"] for n in lib)


def test_spec_is_linked_to_the_production_function(repo):
    root, store = repo
    sign_in = next(
        n for n in _nodes_in(store, "src/auth.ts") if n["name"] == "signIn"
    )
    qn = f"{sign_in['file_path']}::signIn"
    tests = store.get_transitive_tests(qn)
    assert any("e2e/login.spec.ts" in t["qualified_name"] for t in tests), tests
