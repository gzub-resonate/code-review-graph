"""Bundled and minified JavaScript is left out of the graph; hand-written source stays."""

import pytest

from code_review_graph.bundled_js import is_bundled_js
from code_review_graph.incremental import collect_all_files

NCC = "/******/ (() => { // webpackBootstrap\n/******/ var m = {};\n" + "var x = 1;\n" * 50
HAND = "export function add(a, b) {\n  return a + b;\n}\n" * 5


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("CRG_SKIP_BUNDLED_JS", raising=False)


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_signature_in_dist_is_skipped(tmp_path):
    _write(tmp_path, ".github/actions/a/dist/index.js", NCC)
    assert is_bundled_js(tmp_path, ".github/actions/a/dist/index.js")


def test_tail_source_map_marker_in_build_is_skipped(tmp_path):
    _write(tmp_path, "build/app.js", HAND * 3 + "//# sourceMappingURL=app.js.map\n")
    assert is_bundled_js(tmp_path, "build/app.js")


def test_handwritten_dist_without_signature_is_kept(tmp_path):
    _write(tmp_path, "pkg/dist/lib.js", HAND)
    assert not is_bundled_js(tmp_path, "pkg/dist/lib.js")


def test_src_with_signature_text_is_untouched(tmp_path):
    _write(tmp_path, "src/index.js", NCC)
    assert not is_bundled_js(tmp_path, "src/index.js")


def test_minified_single_line_is_skipped_anywhere(tmp_path):
    _write(tmp_path, "src/vendor_min.js", "var a=" + "function(){return 1};" * 400)
    assert is_bundled_js(tmp_path, "src/vendor_min.js")


def test_gitattributes_linguist_generated(tmp_path):
    _write(tmp_path, ".gitattributes", "gen/** linguist-generated=true\n")
    _write(tmp_path, "gen/a.js", HAND)
    _write(tmp_path, "other/b.js", HAND)
    assert is_bundled_js(tmp_path, "gen/a.js")
    assert not is_bundled_js(tmp_path, "other/b.js")


def test_env_var_turns_rule_off(tmp_path, monkeypatch):
    _write(tmp_path, "dist/index.js", NCC)
    monkeypatch.setenv("CRG_SKIP_BUNDLED_JS", "0")
    assert not is_bundled_js(tmp_path, "dist/index.js")


def test_non_js_never_skipped(tmp_path):
    _write(tmp_path, "dist/x.py", NCC)
    assert not is_bundled_js(tmp_path, "dist/x.py")


def test_collect_all_files_drops_bundle_keeps_source(tmp_path):
    _write(tmp_path, "src/app.js", HAND)
    _write(tmp_path, ".github/actions/a/dist/index.js", NCC)
    _write(tmp_path, "lib/dist/real.js", HAND)
    files = {f.replace("\\", "/") for f in collect_all_files(tmp_path)}
    assert "src/app.js" in files
    assert "lib/dist/real.js" in files
    assert ".github/actions/a/dist/index.js" not in files
