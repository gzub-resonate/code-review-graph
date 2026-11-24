"""Dead code: Next.js conventions, tooling-run files, same-file value refs, test_support."""

from pathlib import Path

from code_review_graph import refactor
from code_review_graph.graph import GraphStore
from code_review_graph.parser import CodeParser
from code_review_graph.refactor import find_dead_code

NEXT = {"next.config.js": "module.exports = {};\n"}
PAGE = "export default function UsersPage() { return null; }\n"
ROUTE = "export async function GET() { return 1; }\nexport async function POST() { return 2; }\n"
UTIL = "export function orphanUtil() { return 1; }\n"


def _entries(tmp_path: Path, files: dict[str, str], before=None) -> list[dict]:
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
        if before:
            before()
        return find_dead_code(store, root=repo)
    finally:
        store.close()


def _dead(tmp_path: Path, files: dict[str, str]) -> set[str]:
    return {d["name"] for d in _entries(tmp_path, files)}


def test_next_page_reported_without_config(tmp_path):
    assert "UsersPage" in _dead(tmp_path, {"app/users/page.tsx": PAGE})


def test_next_page_not_reported_with_config(tmp_path):
    dead = _dead(tmp_path, {**NEXT, "app/(main)/users/page.tsx": PAGE})
    assert "UsersPage" not in dead


def test_next_route_verbs_not_reported(tmp_path):
    dead = _dead(tmp_path, {**NEXT, "src/app/api/x/route.ts": ROUTE})
    assert not {"GET", "POST"} & dead


def test_next_helper_in_route_file_still_reported(tmp_path):
    dead = _dead(tmp_path, {**NEXT, "app/api/x/route.ts": ROUTE + UTIL})
    assert "orphanUtil" in dead and "GET" not in dead


def test_plain_util_in_next_repo_still_reported(tmp_path):
    assert "orphanUtil" in _dead(tmp_path, {**NEXT, "lib/util.ts": UTIL})


def test_next_config_functions_not_reported(tmp_path):
    cfg = "export async function redirects() { return []; }\n"
    assert "redirects" not in _dead(tmp_path, {"next.config.mjs": cfg})


def test_next_middleware_not_reported(tmp_path):
    mw = "export function middleware(req) { return req; }\n"
    assert "middleware" not in _dead(tmp_path, {**NEXT, "middleware.ts": mw})


def test_scripts_dir_not_reported(tmp_path):
    dead = _dead(tmp_path, {"scripts/codemods/x.js": UTIL, "lib/y.js": "function other() {}\n"})
    assert "orphanUtil" not in dead and "other" in dead


def test_global_setup_default_export_not_reported(tmp_path):
    setup = "export default async function globalSetup() {}\nexport function helperX() {}\n"
    dead = _dead(tmp_path, {"e2e/global-setup.ts": setup})
    assert "globalSetup" not in dead and "helperX" in dead


def test_playwright_config_functions_not_reported(tmp_path):
    cfg = "export function projects() { return []; }\n"
    assert "projects" not in _dead(tmp_path, {"playwright.config.ts": cfg})


def test_storybook_mock_class_still_reported(tmp_path):
    mock = "export class TextStreamChatTransport {}\n"
    assert "TextStreamChatTransport" in _dead(tmp_path, {".storybook/mocks/ai.ts": mock})


def test_same_file_value_references_not_reported(tmp_path):
    src = (
        "export function run(items) {\n"
        "  function statusLabel(s) { return s; }\n"
        "  function handleError(e) {}\n"
        "  items.map(statusLabel);\n"
        "  window.addEventListener('error', handleError);\n"
        "}\n"
    )
    dead = _dead(tmp_path, {"lib/a.ts": src})
    assert not {"statusLabel", "handleError"} & dead


def test_bind_and_object_value_not_reported(tmp_path):
    src = (
        "function onA() {}\nfunction onB() {}\n"
        "export const x = { a: onA, c: 1 };\nexport const y = onB.bind(null);\n"
    )
    assert not {"onA", "onB"} & _dead(tmp_path, {"lib/a.js": src})


def test_comment_or_string_mention_still_reported(tmp_path):
    src = (
        "function ghost() {}\nfunction text() {}\n"
        "// items.map(ghost)\n"
        "const s = 'call(text)';\n"
        "export const keep = s;\n"
    )
    assert {"ghost", "text"} <= _dead(tmp_path, {"lib/a.js": src})


def test_declared_and_exported_only_still_reported(tmp_path):
    src = "function lonely() {}\nexport { lonely };\n"
    assert "lonely" in _dead(tmp_path, {"lib/a.js": src})


def test_test_support_flag_and_total(tmp_path):
    entries = _entries(tmp_path, {
        "e2e/pages/login.ts": "export function loginHelperZ() {}\n",
        "src/stories/Foo.ts": "export function storyHelperZ() {}\n",
        "lib/real.ts": UTIL,
    })
    flags = {e["name"]: e["test_support"] for e in entries}
    assert flags.get("loginHelperZ") is True
    assert flags.get("storyHelperZ") is True
    assert flags["orphanUtil"] is False
    assert sum(e["test_support"] for e in entries) == 2


def test_unreadable_file_not_suppressed(tmp_path, monkeypatch):
    real = Path.read_text

    def fake(self, *a, **k):
        if self.name in ("page.tsx", "a.js"):
            raise OSError("denied")
        return real(self, *a, **k)

    repo = tmp_path / "repo"
    (repo / "lib").mkdir(parents=True)
    (repo / "lib" / "a.js").write_text("function used() {}\nitems.map(used);\n")
    node = type("N", (), {"name": "used", "file_path": str(repo / "lib" / "a.js"),
                          "line_start": 1})()
    monkeypatch.setattr(Path, "read_text", fake)
    assert refactor._used_as_value(node, {}, {}) is False
    dead = _dead(tmp_path, {**NEXT, "app/users/page.tsx": PAGE})
    assert "UsersPage" in dead


def test_each_file_read_once(tmp_path, monkeypatch):
    reads: list[str] = []
    real = Path.read_text

    def counting(self, *a, **k):
        reads.append(self.name)
        return real(self, *a, **k)

    src = (
        "export default function UsersPage() { return null; }\n"
        "export function other() {}\nexport function another() {}\n"
    )
    entries = _entries(
        tmp_path, {**NEXT, "app/users/page.tsx": src},
        before=lambda: monkeypatch.setattr(Path, "read_text", counting),
    )
    names = {e["name"] for e in entries}
    assert {"other", "another"} <= names and "UsersPage" not in names
    assert reads.count("page.tsx") == 1
