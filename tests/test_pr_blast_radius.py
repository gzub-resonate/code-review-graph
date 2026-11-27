"""A pull request's blast radius comes from its changed files, reached through the base graph."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from code_review_graph import pr_blast_radius as pbr
from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, get_db_path
from code_review_graph.registry import Registry
from code_review_graph.tools.pr_tools import pr_blast_radius_func

CLIENT = (
    "import axios from 'axios';\n"
    "export const list = () => axios.get('/api/items');\n"
    "export const fresh = () => axios.get('/api/fresh');\n"
)
SERVER_BASE = (
    "const express = require('express');\nconst app = express();\n"
    "function loadItems() {\n  return [];\n}\n"
    "function unusedHelper() {\n  return 1;\n}\n"
    "app.get('/api/items', (req, res) => res.json(loadItems()));\n"
    "app.post('/api/old', (req, res) => res.end());\n"
)
# The pull request: loadItems changes, unusedHelper is removed, /api/old goes, /api/fresh
# is added, and a whole new file arrives.
SERVER_PR = (
    "const express = require('express');\nconst app = express();\n"
    "function loadItems() {\n  return [1];\n}\n"
    "app.get('/api/items', (req, res) => res.json(loadItems()));\n"
    "app.get('/api/fresh', (req, res) => res.end());\n"
)
NEW_FILE = (
    "const express = require('express');\nconst router = express();\n"
    "router.get('/api/extra', (req, res) => res.end());\n"
)
MIGRATION = (
    "CREATE TABLE public.expression (id bigint);\n"
    "CREATE INDEX idx_expression_id ON public.expression (id);\n"
    "ALTER TABLE public.other ADD COLUMN x int;\n"
)


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("CRG_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CRG_DATA_DIR", raising=False)
    return Registry()


@pytest.fixture
def pull_request(tmp_path, registry):
    """A server repo on its base commit (graph built), a PR commit on a branch, and a UI repo."""
    api = (tmp_path / "api").resolve()
    api.mkdir()
    git(api, "init", "-q", "-b", "main")
    write(api, "server.js", SERVER_BASE)
    git(api, "add", "-A")
    git(api, "commit", "-q", "-m", "base")
    with GraphStore(get_db_path(api)) as store:
        full_build(api, store)
    registry.register(str(api), alias="api")

    ui = (tmp_path / "ui").resolve()
    (ui / ".git").mkdir(parents=True)
    write(ui, "src/api.ts", CLIENT)
    with GraphStore(get_db_path(ui)) as store:
        full_build(ui, store)
    registry.register(str(ui), alias="ui")

    git(api, "checkout", "-q", "-b", "pr")
    write(api, "server.js", SERVER_PR)
    write(api, "routes.js", NEW_FILE)
    write(api, "src/main/resources/db/migration/V1__expr.sql", MIGRATION)
    git(api, "add", "-A")
    git(api, "commit", "-q", "-m", "pr")
    git(api, "checkout", "-q", "main")
    return api


def run(api: Path, **args):
    args.setdefault("head", "pr")
    args.setdefault("base", "main")
    return pr_blast_radius_func(repo_root=str(api), **args)


def file_row(result, name):
    return next(f for f in result["files"] if f["file"].endswith(name))


def test_a_changed_symbol_is_found_in_the_base_graph(pull_request):
    result = run(pull_request)
    assert result["status"] == "ok"
    assert "loadItems" in file_row(result, "server.js")["changed_symbols"]


def test_a_symbol_the_pull_request_deletes_is_reported_removed(pull_request):
    assert "unusedHelper" in file_row(run(pull_request), "server.js")["removed_symbols"]


def test_a_new_file_is_parsed_though_the_graph_never_saw_it(pull_request):
    result = run(pull_request)
    new = file_row(result, "routes.js")
    assert new["status"] == "added"
    assert new["changed_symbols"] == [] and new["reached"] == 0
    assert result["totals"]["parsed"] >= 2


def test_routes_the_pull_request_adds_and_removes_are_listed(pull_request):
    changes = {(c["change"], c["http_method"], c["route"]) for c in run(pull_request)[
        "contract_changes"]}
    assert ("route added", "GET", "/api/fresh") in changes
    assert ("route added", "GET", "/api/extra") in changes
    assert ("route removed", "POST", "/api/old") in changes
    assert not any(route == "/api/items" for _c, _m, route in changes)


def test_a_client_call_in_another_repo_is_an_external_caller(pull_request):
    result = run(pull_request)
    routes = {(r["client_repo"], r["route"]) for r in result["external_callers"]}
    assert ("ui", "/api/fresh") in routes
    assert ("ui", "/api/items") in routes
    assert result["totals"]["external_callers"] == len(result["external_callers"])


def test_external_callers_can_be_left_out(pull_request):
    result = run(pull_request, include_external=False)
    assert result["external_callers"] == []
    assert result["external_callers_total"] == 0


def test_a_migration_the_pull_request_adds_is_listed(pull_request):
    schema = {(s["change"], s["target"]) for s in run(pull_request)["schema_changes"]}
    assert ("create table", "public.expression") in schema
    assert ("create index", "idx_expression_id") in schema
    assert ("alter table", "public.other") in schema


def test_nothing_is_written_to_the_graph(pull_request):
    db = get_db_path(pull_request)
    with GraphStore(db) as store:
        before = (len(store.get_all_nodes()), len(store.get_all_edges()))
    run(pull_request)
    with GraphStore(db) as store:
        assert (len(store.get_all_nodes()), len(store.get_all_edges())) == before


def test_a_stacked_pull_request_says_how_far_it_is_from_the_default_branch(pull_request):
    git(pull_request, "update-ref", "refs/remotes/origin/main", "main")
    git(pull_request, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git(pull_request, "checkout", "-q", "pr")
    git(pull_request, "checkout", "-q", "-b", "child")
    write(pull_request, "child.js", "const x = 1;\n")
    git(pull_request, "add", "-A")
    git(pull_request, "commit", "-q", "-m", "child")
    git(pull_request, "checkout", "-q", "main")
    result = run(pull_request, head="child", base="pr")
    assert len(result["files"]) == 1 and file_row(result, "child.js")["status"] == "added"
    assert any("default branch" in note and "4 files" in note for note in result["notes"])


def test_what_calls_a_changed_symbol_is_reached(tmp_path):
    repo = (tmp_path / "lib").resolve()
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "lib.py", "def helper():\n    return 1\n\n\ndef lonely():\n    return 2\n")
    write(repo, "app.py", "from lib import helper\n\n\ndef run():\n    return helper()\n")
    write(repo, "test_app.py", "from app import run\n\n\ndef test_run():\n    assert run()\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
    git(repo, "checkout", "-q", "-b", "pr")
    write(repo, "lib.py", "def helper():\n    return 11\n\n\ndef lonely():\n    return 22\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pr")
    git(repo, "checkout", "-q", "main")
    result = pr_blast_radius_func(
        repo_root=str(repo), head="pr", base="main", include_external=False,
    )
    row = file_row(result, "lib.py")
    assert sorted(row["changed_symbols"]) == ["helper", "lonely"]
    assert row["reached"] >= 1 and row["same_repo_files"] >= 1
    assert row["reached_tests"] >= 1
    assert "lonely" in row.get("untested", [])


def test_a_missing_head_is_an_error_not_a_crash(pull_request):
    result = run(pull_request, head="no-such-branch")
    assert result["status"] == "error"


def test_neither_pr_nor_head_reviews_the_labelled_last_commit(pull_request):
    result = pr_blast_radius_func(repo_root=str(pull_request))
    assert result["status"] == "ok"
    assert result["scope"]["kind"] == "last_commit"


def test_no_difference_is_an_empty_answer(pull_request):
    result = run(pull_request, head="main", base="main")
    assert result["status"] == "ok" and result["total_files"] == 0


def test_a_ref_that_looks_like_an_option_is_refused(pull_request):
    result = run(pull_request, head="--output=/tmp/x")
    assert result["status"] == "error"


def test_sql_changes_reads_the_statements():
    rows = pbr.sql_changes("V1__x.sql", MIGRATION)
    assert [r["change"] for r in rows] == ["create table", "create index", "alter table"]


def test_a_pull_request_touching_many_files_is_capped(tmp_path):
    repo = (tmp_path / "wide").resolve()
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "seed.py", "def seed():\n    return 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
    git(repo, "checkout", "-q", "-b", "pr")
    for number in range(70):
        write(repo, f"pkg/mod{number}.py", f"def f{number}():\n    return {number}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pr")
    git(repo, "checkout", "-q", "main")
    result = pr_blast_radius_func(
        repo_root=str(repo), head="pr", base="main", include_external=False,
    )
    assert result["totals"]["files"] == 70
    assert len(result["files"]) == 50 and result["files_truncated"] is True
    assert result["totals"]["symbols_added"] == 70
    assert result["files_total"] == 70
    assert "showing 50 of 70" in result["summary"]
    # every other capped list says its true size and whether it was cut
    for key in ("contract_changes", "endpoints", "same_file_endpoints", "schema_changes"):
        assert result[f"{key}_total"] == len(result[key]) == 0
        assert result[f"{key}_truncated"] is False
    assert result["external_callers_truncated"] is False


@pytest.mark.parametrize("ref", ["--stat", "-p", "--no-index", "--output=/tmp/x", "-"])
def test_a_ref_that_is_an_option_is_refused_for_head_and_base(pull_request, ref):
    assert run(pull_request, head=ref)["status"] == "error"
    assert run(pull_request, base=ref)["status"] == "error"


def test_refs_are_resolved_to_commits_before_git_sees_them(pull_request, monkeypatch):
    seen: list[tuple[str, ...]] = []
    real = pbr._git

    def spy(repo_root, *args, **kwargs):
        seen.append(args)
        return real(repo_root, *args, **kwargs)

    monkeypatch.setattr(pbr, "_git", spy)
    assert run(pull_request)["status"] == "ok"
    for args in seen:
        if args[0] in ("diff", "show", "cat-file"):
            names = [a for a in args if a != "--"]
            assert "pr" not in names and "main" not in names, args


def _spy_on_reads(monkeypatch):
    shown: list[str] = []
    real = pbr._git

    def spy(repo_root, *args, **kwargs):
        if args[0] == "show":
            shown.append(args[1].split(":", 1)[1])
        elif args[0] == "cat-file" and args[1] == "--batch":
            # A batched read of the files named on stdin, in place of one ``show`` each.
            lines = kwargs["stdin"].decode().splitlines()
            shown.extend(line.split(":", 1)[1] for line in lines)
        return real(repo_root, *args, **kwargs)

    monkeypatch.setattr(pbr, "_git", spy)
    return shown


def test_a_file_over_the_size_limit_is_never_read(pull_request, monkeypatch):
    write(pull_request, "big.js", "// " + "x" * 5000 + "\n")
    git(pull_request, "checkout", "-q", "pr")
    git(pull_request, "add", "-A")
    git(pull_request, "commit", "-q", "-m", "big")
    git(pull_request, "checkout", "-q", "main")
    monkeypatch.setattr(pbr, "MAX_FILE_BYTES", 1000)
    shown = _spy_on_reads(monkeypatch)
    result = run(pull_request)
    big = file_row(result, "big.js")
    assert big["note"] == "larger than the parse limit"
    assert "big.js" not in shown


def test_files_past_the_parse_limit_are_not_read(tmp_path, monkeypatch):
    repo = (tmp_path / "many").resolve()
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "seed.py", "def seed():\n    return 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
    git(repo, "checkout", "-q", "-b", "pr")
    for number in range(8):
        write(repo, f"m{number}.py", f"def f{number}():\n    return {number}\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pr")
    git(repo, "checkout", "-q", "main")
    monkeypatch.setattr(pbr, "MAX_PARSED_FILES", 3)
    shown = _spy_on_reads(monkeypatch)
    result = pr_blast_radius_func(
        repo_root=str(repo), head="pr", base="main", include_external=False,
    )
    assert result["totals"]["parsed"] == 3
    assert len(shown) == 3
    assert sum(1 for f in result["files"] if f.get("note")) == 5


def test_the_byte_budget_stops_further_reads(pull_request, monkeypatch):
    monkeypatch.setattr(pbr, "MAX_TOTAL_BYTES", 10)
    shown = _spy_on_reads(monkeypatch)
    result = run(pull_request)
    assert len(shown) == 1
    assert any(f.get("note") == "past the per-request byte limit" for f in result["files"])


def test_more_changed_files_than_the_cap_are_counted_not_read(pull_request, monkeypatch):
    monkeypatch.setattr(pbr, "MAX_CHANGED_FILES", 2)
    result = run(pull_request)
    assert result["totals"]["files"] == 2
    assert any("files changed; only the first 2" in note for note in result["notes"])


def test_git_output_past_the_limit_is_refused(pull_request):
    with pytest.raises(pbr.PullRequestError):
        pbr._git(str(pull_request), "log", "--format=%H%n%B", limit=5)


def test_credentials_in_a_git_error_are_scrubbed():
    text = "fatal: unable to access 'https://user:s3cret@github.com/o/r.git/': failed"
    assert "s3cret" not in pbr._scrub(text) and "user" not in pbr._scrub(text)
    assert "github.com/o/r.git" in pbr._scrub(text)


def test_a_failed_fetch_does_not_echo_the_remote_credentials(pull_request):
    git(pull_request, "remote", "add", "origin", "https://user:s3cret@127.0.0.1:1/x.git")
    result = pr_blast_radius_func(repo_root=str(pull_request), pr=5, include_external=False)
    assert result["status"] == "error"
    assert "s3cret" not in result["error"] and "user:" not in result["error"]


def test_reach_walks_share_one_visit_budget(tmp_path, monkeypatch):
    repo = (tmp_path / "chain").resolve()
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "lib.py", "def base():\n    return 1\n")
    lines = ["from lib import base\n"]
    for number in range(6):
        lines.append(f"\n\ndef caller{number}():\n    return base()\n")
    write(repo, "app.py", "".join(lines))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "base")
    with GraphStore(get_db_path(repo)) as store:
        full_build(repo, store)
    git(repo, "checkout", "-q", "-b", "pr")
    write(repo, "lib.py", "def base():\n    return 2\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pr")
    git(repo, "checkout", "-q", "main")
    monkeypatch.setattr(pbr, "MAX_REACH_VISITS", 2)
    result = pr_blast_radius_func(
        repo_root=str(repo), head="pr", base="main", include_external=False,
    )
    assert any("visit limit" in note for note in result["notes"])


def test_names_in_migration_rows_are_sanitised(pull_request):
    git(pull_request, "checkout", "-q", "pr")
    write(pull_request, "db/migration/V2__a\x1b[31mb.sql", "CREATE TABLE evil\x07name (id int);\n")
    git(pull_request, "add", "-A")
    git(pull_request, "commit", "-q", "-m", "odd names")
    git(pull_request, "checkout", "-q", "main")
    rows = run(pull_request)["schema_changes"]
    assert rows
    for row in rows:
        assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in row["file"] + row["target"])


# --- config paths, endpoints of a changed controller, a pull request already merged ---------

YAML_BASE = (
    "spring:\n  app:\n    client:\n      connect-timeout: 2000\n      logger-level: NONE\n"
    "    old-key: gone\n"
)
YAML_PR = (
    "spring:\n  app:\n    client:\n      connect-timeout: 2500\n      logger-level: NONE\n"
    "      read-timeout: 10000\n"
)
PLAIN_BASE = "a:\n  b: 1  # note\n  c: x\n  d: keep\n"
PLAIN_PR = "a:\n  b: 1  # another note\n  c: y\n  e: new\n  d: keep\n"
JSON_BASE = '{"retries": 3, "name": "svc", "gone": true}'
JSON_PR = '{"retries": 5, "name": "svc", "added": {"x": 1}}'
CONTROLLER_BASE = """package a;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/v2/things")
public class Ctl {

    @GetMapping("/{id}")
    public String one(@PathVariable String id) {
        return id;
    }

    @PostMapping("/x")
    public String two() {
        return "x";
    }
}
"""
CONTROLLER_PR = CONTROLLER_BASE.replace(
    "    @PostMapping",
    '    @GetMapping("/{id}/file")\n    public String file(@PathVariable String id) {\n'
    "        return id;\n    }\n\n    @PostMapping",
)


@pytest.fixture
def config_pull_request(tmp_path, registry):
    """A Spring-like service: YAML, JSON and a controller change on the PR branch."""
    svc = (tmp_path / "svc").resolve()
    svc.mkdir()
    git(svc, "init", "-q", "-b", "main")
    write(svc, "src/main/resources/application.yml", YAML_BASE)
    write(svc, "settings.yml", PLAIN_BASE)
    write(svc, "conf.json", JSON_BASE)
    write(svc, "src/main/java/a/Ctl.java", CONTROLLER_BASE)
    git(svc, "add", "-A")
    git(svc, "commit", "-q", "-m", "base")
    git(svc, "branch", "topic")
    with GraphStore(get_db_path(svc)) as store:
        full_build(svc, store)
    git(svc, "checkout", "-q", "-b", "pr")
    write(svc, "src/main/resources/application.yml", YAML_PR)
    write(svc, "settings.yml", PLAIN_PR)
    write(svc, "conf.json", JSON_PR)
    write(svc, "src/main/java/a/Ctl.java", CONTROLLER_PR)
    git(svc, "add", "-A")
    git(svc, "commit", "-q", "-m", "pr")
    git(svc, "checkout", "-q", "main")
    write(svc, "later.txt", "later\n")
    git(svc, "add", "-A")
    git(svc, "commit", "-q", "-m", "later")
    return svc


def config_rows(result):
    return {(r["file"].rsplit("/", 1)[-1], r["path"]): r["change"]
            for r in result["config_changes"]}


def test_spring_application_yml_changes_are_reported(config_pull_request):
    rows = config_rows(run(config_pull_request))
    assert rows[("application.yml", "spring.app.client.connectTimeout")] == "changed"
    assert rows[("application.yml", "spring.app.client.readTimeout")] == "added"
    assert rows[("application.yml", "spring.app.oldKey")] == "removed"
    assert ("application.yml", "spring.app.client.loggerLevel") not in rows


def test_plain_yaml_paths_compare_by_path_and_value_not_comments(config_pull_request):
    rows = config_rows(run(config_pull_request))
    assert rows[("settings.yml", "$.a.c")] == "changed"
    assert rows[("settings.yml", "$.a.e")] == "added"
    assert ("settings.yml", "$.a.b") not in rows  # only its trailing comment changed
    assert ("settings.yml", "$.a.d") not in rows


def test_minified_json_compares_values_per_path(config_pull_request):
    rows = config_rows(run(config_pull_request))
    assert rows[("conf.json", "$.retries")] == "changed"
    assert rows[("conf.json", "$.gone")] == "removed"
    assert rows[("conf.json", "$.added")] == "added"
    assert ("conf.json", "$.name") not in rows


def test_config_rows_carry_no_values_and_the_summary_counts_them(config_pull_request):
    result = run(config_pull_request)
    assert "2500" not in str(result["config_changes"])
    assert "config path(s) changed" in result["summary"]
    assert result["totals"]["config_paths_changed"] == len(result["config_changes"])
    row = file_row(result, "application.yml")
    assert row["config_changes"] == {"added": 1, "changed": 1, "removed": 1}


def test_config_paths_are_capped_per_file(config_pull_request, monkeypatch):
    monkeypatch.setattr(pbr, "MAX_CONFIG_PATHS_PER_FILE", 1)
    result = run(config_pull_request)
    assert result["config_changes_truncated"] is True
    assert len([r for r in result["config_changes"] if r["file"].endswith("settings.yml")]) == 1


def test_only_endpoints_whose_handler_changed_are_affected(config_pull_request):
    result = run(config_pull_request)
    affected = {(e["http_method"], e["route"]) for e in result["endpoints"]}
    assert affected == {("GET", "/v2/things/{id}/file")}
    others = {(e["http_method"], e["route"]) for e in result["same_file_endpoints"]}
    assert others == {("GET", "/v2/things/{id}"), ("POST", "/v2/things/x")}
    assert result["totals"]["endpoints"] == 1
    assert result["totals"]["same_file_endpoints"] == 2
    assert result["totals"]["external_callers"] == 0


def test_an_edited_handler_makes_its_endpoint_affected(config_pull_request):
    git(config_pull_request, "checkout", "-q", "pr")
    text = CONTROLLER_PR.replace('return "x";', 'return "y";')
    write(config_pull_request, "src/main/java/a/Ctl.java", text)
    git(config_pull_request, "add", "-A")
    git(config_pull_request, "commit", "-q", "-m", "edit")
    git(config_pull_request, "checkout", "-q", "main")
    affected = {e["route"] for e in run(config_pull_request)["endpoints"]}
    assert affected == {"/v2/things/{id}/file", "/v2/things/x"}


def test_a_head_that_is_an_ancestor_of_the_base_says_it_is_merged(config_pull_request):
    result = run(config_pull_request, head="topic")
    assert result["status"] == "ok" and result["total_files"] == 0
    assert result["already_merged"] is True
    assert "already merged into main" in result["summary"]
    assert "graph already includes it" in result["summary"]


def test_a_head_that_differs_is_not_called_merged(config_pull_request):
    assert "already merged" not in run(config_pull_request)["summary"]
