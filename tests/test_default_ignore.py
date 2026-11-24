"""``CRG_DEFAULT_IGNORE`` operator-wide ignore patterns; dead code skips Dockerfile stages."""

import pytest

from code_review_graph.constants import default_ignore_patterns
from code_review_graph.errors import CodeReviewGraphError
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import (
    _load_ignore_patterns,
    _should_ignore,
    clear_nested_ignore_cache,
    full_build,
    incremental_update,
)
from code_review_graph.parser import EdgeInfo, NodeInfo
from code_review_graph.refactor import find_dead_code

ENV = "CRG_DEFAULT_IGNORE"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    clear_nested_ignore_cache()


def _repo(root):
    (root / "src" / "generated").mkdir(parents=True)
    (root / "src" / "generated" / "Big.py").write_text("def big():\n    return 1\n")
    (root / "src" / "generated" / "keep").mkdir()
    (root / "src" / "generated" / "keep" / "Spared.py").write_text("def spared():\n    return 2\n")
    (root / "src" / "real.py").write_text("def real():\n    return 3\n")
    return root


def _files(store):
    return {p.rsplit("/", 1)[-1] for p in store.get_all_files()}


class TestEnvParsing:
    def test_unset_and_blank_are_empty(self, monkeypatch):
        assert default_ignore_patterns() == []
        monkeypatch.setenv(ENV, "  \n , ,\n")
        assert default_ignore_patterns() == []

    def test_commas_newlines_and_blanks(self, monkeypatch):
        monkeypatch.setenv(ENV, "src/generated/**, .claude/**\n\n*.gen.py,")
        assert default_ignore_patterns() == ["src/generated/**", ".claude/**", "*.gen.py"]

    def test_too_many_patterns(self, monkeypatch):
        monkeypatch.setenv(ENV, ",".join(f"p{i}/**" for i in range(201)))
        with pytest.raises(CodeReviewGraphError, match="CRG_DEFAULT_IGNORE has 201 patterns"):
            default_ignore_patterns()

    def test_pattern_too_long(self, monkeypatch):
        monkeypatch.setenv(ENV, "a" * 257)
        with pytest.raises(CodeReviewGraphError, match="257 characters"):
            default_ignore_patterns()

    def test_control_character(self, monkeypatch):
        monkeypatch.setenv(ENV, "a\x01b")
        with pytest.raises(CodeReviewGraphError, match="control character"):
            default_ignore_patterns()

    def test_negation_rejected(self, monkeypatch):
        monkeypatch.setenv(ENV, "!src/keep")
        with pytest.raises(CodeReviewGraphError, match="negation"):
            default_ignore_patterns()


class TestMatching:
    def test_applies_in_addition_to_repo_file(self, tmp_path, monkeypatch):
        (tmp_path / ".code-review-graphignore").write_text("own/\n")
        monkeypatch.setenv(ENV, "src/generated/**,.claude/**")
        patterns = _load_ignore_patterns(tmp_path)
        assert _should_ignore("src/generated/A.java", patterns)
        assert _should_ignore(".claude/settings.json", patterns)
        assert _should_ignore("own/x.py", patterns)
        assert not _should_ignore("src/main/A.java", patterns)

    def test_unset_is_noop(self, tmp_path):
        (tmp_path / ".code-review-graphignore").write_text("own/\n")
        before = _load_ignore_patterns(tmp_path)
        assert not any(type(p) is not str for p in before)
        assert not _should_ignore("src/generated/A.java", before)

    def test_blank_is_noop(self, tmp_path, monkeypatch):
        baseline = _load_ignore_patterns(tmp_path)
        monkeypatch.setenv(ENV, " , \n")
        assert _load_ignore_patterns(tmp_path) == baseline

    def test_repo_negation_spares_a_default_path(self, tmp_path, monkeypatch):
        (tmp_path / ".code-review-graphignore").write_text("!src/generated/keep\n")
        monkeypatch.setenv(ENV, "src/generated/**")
        patterns = _load_ignore_patterns(tmp_path)
        assert _should_ignore("src/generated/Big.java", patterns)
        assert not _should_ignore("src/generated/keep/Spared.java", patterns)

    def test_negation_does_not_spare_repo_own_pattern(self, tmp_path, monkeypatch):
        (tmp_path / ".code-review-graphignore").write_text(
            "src/generated/keep/\n!src/generated/keep\n",
        )
        monkeypatch.setenv(ENV, "src/generated/**")
        patterns = _load_ignore_patterns(tmp_path)
        assert _should_ignore("src/generated/keep/Spared.java", patterns)


class TestBuildAndUpdate:
    def test_full_build_honours_default(self, tmp_path, monkeypatch):
        _repo(tmp_path)
        monkeypatch.setenv(ENV, "src/generated/**")
        with GraphStore(tmp_path / "g.db") as store:
            full_build(tmp_path, store)
            assert _files(store) == {"real.py"}

    def test_full_build_negation_spares_path(self, tmp_path, monkeypatch):
        _repo(tmp_path)
        (tmp_path / ".code-review-graphignore").write_text("!src/generated/keep\n")
        monkeypatch.setenv(ENV, "src/generated/**")
        with GraphStore(tmp_path / "g.db") as store:
            full_build(tmp_path, store)
            assert _files(store) == {"real.py", "Spared.py"}

    def test_full_build_without_env_indexes_everything(self, tmp_path):
        _repo(tmp_path)
        with GraphStore(tmp_path / "g.db") as store:
            full_build(tmp_path, store)
            assert _files(store) == {"real.py", "Big.py", "Spared.py"}

    def test_incremental_update_skips_ignored_changed_file(self, tmp_path, monkeypatch):
        _repo(tmp_path)
        monkeypatch.setenv(ENV, "src/generated/**")
        with GraphStore(tmp_path / "g.db") as store:
            incremental_update(
                tmp_path, store,
                changed_files=["src/generated/Big.py", "src/real.py"],
            )
            assert _files(store) == {"real.py"}

    def test_file_already_in_graph_is_removed_when_it_becomes_ignored(
        self, tmp_path, monkeypatch,
    ):
        _repo(tmp_path)
        with GraphStore(tmp_path / "g.db") as store:
            full_build(tmp_path, store)
            assert "Big.py" in _files(store)
            monkeypatch.setenv(ENV, "src/generated/**")
            result = incremental_update(tmp_path, store, changed_files=[])
            assert result["stale_files_removed"] == 2
            assert _files(store) == {"real.py"}


class TestDeadCodeDockerfileStage:
    def test_stage_not_dead_but_real_unused_function_is(self, tmp_path):
        with GraphStore(tmp_path / "g.db") as store:
            store.upsert_node(NodeInfo(
                kind="File", name="/repo/Dockerfile", file_path="/repo/Dockerfile",
                line_start=1, line_end=10, language="dockerfile",
            ))
            store.upsert_node(NodeInfo(
                kind="Class", name="stage:0", file_path="/repo/Dockerfile",
                line_start=1, line_end=5, language="dockerfile",
                extra={"dockerfile_type": "stage", "stage_index": 0},
            ))
            store.upsert_node(NodeInfo(
                kind="Class", name="stage:builder", file_path="/repo/Dockerfile",
                line_start=6, line_end=9, language="dockerfile",
            ))
            store.upsert_node(NodeInfo(
                kind="Function", name="unused_helper", file_path="/repo/app.py",
                line_start=1, line_end=3, language="python",
            ))
            store.upsert_node(NodeInfo(
                kind="Class", name="UnusedThing", file_path="/repo/app.py",
                line_start=5, line_end=9, language="python",
            ))
            store.upsert_edge(EdgeInfo(
                kind="CONTAINS", source="/repo/Dockerfile",
                target="/repo/Dockerfile::stage:0", file_path="/repo/Dockerfile", line=1,
            ))
            store.commit()
            names = {d["name"] for d in find_dead_code(store)}
        assert "unused_helper" in names
        assert "UnusedThing" in names
        assert "stage:0" not in names
        assert "stage:builder" not in names
