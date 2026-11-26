"""The index fingerprint moves with graph-affecting code and nothing else."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import code_review_graph
from code_review_graph import index_fingerprint as fp

PACKAGE_ROOT = Path(code_review_graph.__file__).parent

# Every module of the package that the build path does not reach. A new module
# fails test_every_module_is_classified until it is either reached from a build
# entry point or added here on purpose. Adding a name here asserts that it
# cannot change what a build writes.
QUERY_ONLY = {
    # read-side analysis, review and refactor tooling
    "analysis", "change_details", "changes", "graph_diff", "hints", "neighbourhood",
    "new_code", "pr_blast_radius", "prompts", "refactor", "uncertainty", "exports",
    "visualization", "memory", "http_access_log", "http_origin_guard",
    # cross-repo link passes, run at query time over finished graphs
    "aws_sdk_links", "stepfunction_links", "terraform_links", "workflow_links",
    "service_aliases", "infra_join", "amqp_join", "repo_cache",
    # MCP tool front-ends
    "tools.change_scope",
    "tools.analysis_tools", "tools.community_tools", "tools.context", "tools.flows_tools",
    "tools.pr_tools", "tools.query", "tools.query_extras", "tools.refactor_tools",
    "tools.registry_tools", "tools.review",
    # CLI, server, installers and environment plumbing
    "__main__", "cli", "main", "stdio", "daemon_cli", "doctor", "dsh", "enrich",
    "hook_update", "install_gitignore", "install_manifest", "jsonc", "multi_worktree",
    "skills", "codex_skill", "uninstall", "_legacy_instructions",
    # removes files from a graph; the indexer never calls it
    "forget",
    # benchmarks and evaluation harness
    "eval", "eval.benchmarks", "eval.benchmarks.agent_baseline",
    "eval.benchmarks.build_performance", "eval.benchmarks.flow_completeness",
    "eval.benchmarks.impact_accuracy", "eval.benchmarks.incremental_fidelity",
    "eval.benchmarks.multi_hop_retrieval", "eval.benchmarks.search_quality",
    "eval.benchmarks.token_efficiency", "eval.reporter", "eval.runner", "eval.scorer",
    "eval.token_benchmark", "token_benchmark",
    # bundled skill scripts
    "assets.code-review-graph.scripts.crg_readonly",
    # the fingerprint itself is hashed separately
    "index_fingerprint",
}

# Non-Python files under these directories are documentation or assets that no
# build reads. Any other data file in the package must be added to
# index_fingerprint.DATA_FILES.
NON_BUILD_DATA_DIRS = ("docs", "assets", "eval")


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")


@pytest.fixture
def pkg(tmp_path: Path, _pin_versions: None) -> Path:
    """A small synthetic package: build -> parser -> util, plus query-only code."""
    root = tmp_path / "synth"
    _write(root, "__init__.py", '"""Synthetic package."""\n\n__version__ = "1"\n')
    _write(
        root,
        "build.py",
        '''\
        """Build entry point."""
        from . import parser


        def build():
            from .schema import LATEST
            return parser.parse(LATEST)
        ''',
    )
    _write(
        root,
        "parser.py",
        '''\
        """Parse things."""
        from synth.util import helper


        def parse(x):
            """Parse x."""
            # a comment
            return helper(x) + 1
        ''',
    )
    _write(root, "util.py", "def helper(x):\n    return x * 2\n")
    _write(root, "schema.py", "LATEST = 3\n")
    _write(root, "query.py", "def find():\n    return 1\n")
    _write(root, "sub/__init__.py", "")
    _write(root, "sub/deep.py", "from .. import util\n")
    _write(root, "sub/leaf.py", "from . import deep\n")
    return root


ENTRY = ("build", "sub.leaf")


@pytest.fixture
def _pin_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fp.metadata, "version", lambda name: "1.0.0")
    monkeypatch.setattr(fp, "_dependency_names", lambda: ["tree-sitter", "tree-sitter-go"])
    for var in fp.ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _fp(root: Path) -> str:
    value = fp.compute_fingerprint(root, ENTRY)
    assert value != fp.UNKNOWN
    return value


# --- behaviour on a fixture package ---------------------------------------------


def test_stable_and_short_hex(pkg: Path) -> None:
    first = _fp(pkg)
    assert first == _fp(pkg)
    assert len(first) == 16
    int(first, 16)


@pytest.mark.parametrize(
    ("rel", "old", "new"),
    [
        ("parser.py", "# a comment", "# a different comment, longer"),
        ("parser.py", '"""Parse x."""', '"""Parse x, documented at length."""'),
        ("parser.py", '"""Parse things."""', '"""Rewritten module docstring."""'),
        ("parser.py", "return helper(x) + 1", "return   helper( x )+1"),
        ("util.py", "def helper(x):", "\n\n\ndef helper(x):"),
    ],
    ids=["comment", "function-docstring", "module-docstring", "formatting", "blank-lines"],
)
def test_cosmetic_edit_keeps_fingerprint(pkg: Path, rel: str, old: str, new: str) -> None:
    before = _fp(pkg)
    path = pkg / rel
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")
    assert _fp(pkg) == before


@pytest.mark.parametrize(
    ("rel", "old", "new"),
    [
        ("util.py", "x * 2", "x * 3"),
        ("parser.py", "+ 1", "+ 2"),
        ("schema.py", "LATEST = 3", "LATEST = 4"),
        ("sub/deep.py", "from .. import util", "from .. import util\nX = 1"),
    ],
    ids=["transitive", "direct", "function-level-import", "relative-import"],
)
def test_code_edit_changes_fingerprint(pkg: Path, rel: str, old: str, new: str) -> None:
    before = _fp(pkg)
    path = pkg / rel
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    assert _fp(pkg) != before


def test_module_outside_closure_is_ignored(pkg: Path) -> None:
    before = _fp(pkg)
    (pkg / "query.py").write_text("def find():\n    return 2\n", encoding="utf-8")
    _write(pkg, "newtool.py", "X = 1\n")
    assert _fp(pkg) == before


def test_closure_contents(pkg: Path) -> None:
    closure = set(fp.import_closure(pkg, ENTRY))
    assert closure == {"", "build", "parser", "util", "schema", "sub", "sub.deep", "sub.leaf"}


def test_parent_package_init_is_included(pkg: Path) -> None:
    before = _fp(pkg)
    (pkg / "sub" / "__init__.py").write_text("FLAG = True\n", encoding="utf-8")
    assert _fp(pkg) != before


def test_package_init_reexport_is_followed(tmp_path: Path) -> None:
    root = tmp_path / "rx"
    _write(root, "__init__.py", "from .core import thing\n")
    _write(root, "entry.py", "from rx import thing\n")
    _write(root, "core.py", "def thing():\n    return 1\n")
    assert "core" in fp.import_closure(root, ("entry",))


def test_import_module_with_constant_name_is_followed(tmp_path: Path) -> None:
    root = tmp_path / "dyn"
    _write(root, "__init__.py", "")
    _write(
        root,
        "entry.py",
        "import importlib\nmod = importlib.import_module('dyn.plug')\n"
        "other = importlib.import_module('.rel', package='dyn')\n",
    )
    _write(root, "plug.py", "")
    _write(root, "rel.py", "")
    assert {"plug", "rel"} <= set(fp.import_closure(root, ("entry",)))


def test_dependency_version_change_changes_fingerprint(
    pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _fp(pkg)
    monkeypatch.setattr(fp.metadata, "version", lambda name: "2.0.0")
    assert _fp(pkg) != before


def test_one_grammar_version_change_changes_fingerprint(
    pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _fp(pkg)
    monkeypatch.setattr(
        fp.metadata, "version", lambda name: "9.9.9" if name == "tree-sitter-go" else "1.0.0"
    )
    assert _fp(pkg) != before


def test_embedding_model_env_changes_fingerprint(
    pkg: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _fp(pkg)
    monkeypatch.setenv("CRG_EMBEDDING_MODEL", "another-model")
    assert _fp(pkg) != before


def test_unrelated_env_is_ignored(pkg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = _fp(pkg)
    monkeypatch.setenv("CRG_OPENAI_API_KEY", "secret")
    monkeypatch.setenv("CRG_DATA_DIR", "/elsewhere")
    assert _fp(pkg) == before


def test_missing_entry_point_returns_unknown(pkg: Path) -> None:
    assert fp.compute_fingerprint(pkg, ("nope",)) == fp.UNKNOWN


def test_syntax_error_returns_unknown_without_raising(pkg: Path) -> None:
    (pkg / "util.py").write_text("def broken(:\n", encoding="utf-8")
    assert fp.compute_fingerprint(pkg, ENTRY) == fp.UNKNOWN


def test_unreadable_root_returns_unknown(tmp_path: Path) -> None:
    assert fp.compute_fingerprint(tmp_path / "missing", ("x",)) == fp.UNKNOWN


# --- the real package -------------------------------------------------------------


@pytest.fixture
def real_closure() -> dict[str, Path]:
    return fp.import_closure(PACKAGE_ROOT)


def test_default_entry_points_exist() -> None:
    assert fp.compute_fingerprint(use_cache=False) != fp.UNKNOWN


def test_real_fingerprint_is_cached_and_stable() -> None:
    first = fp.compute_fingerprint()
    assert first == fp.compute_fingerprint()
    assert first == fp.compute_fingerprint(use_cache=False)


def test_known_index_modules_are_in_closure(real_closure: dict[str, Path]) -> None:
    expected = {
        "parser", "graph", "migrations", "incremental", "embeddings", "flows",
        "communities", "search", "postprocessing", "tools.build", "tools.docs",
        "python_resolver", "spring_resolver", "hcl_resolver", "scoped_resolver",
        "aws_terraform_wiring", "custom_languages", "signatures",
    }
    assert expected <= set(real_closure)


def test_query_side_modules_are_not_in_closure(real_closure: dict[str, Path]) -> None:
    for name in ("cli", "main", "refactor", "tools.query", "tools.registry_tools", "changes"):
        assert name not in real_closure


def test_every_module_is_classified(real_closure: dict[str, Path]) -> None:
    every = set(fp._module_map(PACKAGE_ROOT))
    unclassified = every - set(real_closure) - QUERY_ONLY
    assert not unclassified, (
        "New module(s) neither reached from the build entry points nor listed in "
        "QUERY_ONLY: %s. If a build can use them, import them from the build path or add "
        "an entry point to index_fingerprint.BUILD_ENTRY_POINTS; if they are query-side "
        "only, add them to QUERY_ONLY." % sorted(unclassified)
    )


def test_query_only_has_no_stale_or_contradictory_entries(
    real_closure: dict[str, Path],
) -> None:
    every = set(fp._module_map(PACKAGE_ROOT))
    assert not QUERY_ONLY - every, "stale QUERY_ONLY entries: %s" % sorted(QUERY_ONLY - every)
    both = QUERY_ONLY & (set(real_closure) - {"index_fingerprint"})
    assert not both, "in the closure but listed QUERY_ONLY: %s" % sorted(both)


def test_package_data_files_are_classified() -> None:
    stray = []
    for path in PACKAGE_ROOT.rglob("*"):
        if not path.is_file() or path.suffix in (".py", ".pyc") or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(PACKAGE_ROOT)
        if rel.parts[0] not in NON_BUILD_DATA_DIRS and rel.as_posix() not in fp.DATA_FILES:
            stray.append(rel.as_posix())
    assert not stray, "data file(s) not covered by DATA_FILES: %s" % stray


def test_real_package_modules_parse() -> None:
    for path in fp.import_closure(PACKAGE_ROOT).values():
        ast.parse(path.read_text(encoding="utf-8"))


def test_module_entry_point_prints_fingerprint() -> None:
    out = subprocess.run(
        [sys.executable, "-m", "code_review_graph.index_fingerprint"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out == fp.compute_fingerprint()
