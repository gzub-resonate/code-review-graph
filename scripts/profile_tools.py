"""Time every MCP tool body on a synthetic repository.

Generates a deterministic Python repo (packages of modules with classes, functions, calls, imports
and tests), builds its graph with the real build tool, then times each read tool cold (first call
after the process starts) and warm (median of repeats), with the provenance stamp timed apart.

    uv run python scripts/profile_tools.py --files 4000 --root /path/to/scratch/repo
    uv run python scripts/profile_tools.py --root /path/to/scratch/repo --skip-build --profile NAME

The synthetic repo is not production data; the numbers say how the tools scale, not what the
deployed server costs.
"""

from __future__ import annotations

import argparse
import asyncio
import cProfile
import json
import pstats
import random
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

BIG = [f"pkg{p}/mod{m}.py" for p in range(8) for m in range(25)]


def generate(root: Path, files: int, seed: int = 7) -> int:
    """Write ``files`` Python modules, plus tests, and commit them."""
    rnd = random.Random(seed)
    per_pkg = 25
    pkgs = max(1, files // per_pkg)
    root.mkdir(parents=True, exist_ok=True)
    (root / "core").mkdir(exist_ok=True)
    (root / "core" / "__init__.py").write_text("")
    (root / "core" / "util.py").write_text(
        "\n".join(f"def helper_{k}(x):\n    return x + {k}\n" for k in range(20))
    )
    written = 0
    for p in range(pkgs):
        pdir = root / f"pkg{p}"
        pdir.mkdir(exist_ok=True)
        (pdir / "__init__.py").write_text("")
        for m in range(per_pkg):
            lines = [f"from core.util import helper_{rnd.randrange(20)}"]
            for _ in range(2):
                lines.append(
                    f"from pkg{rnd.randrange(pkgs)}.mod{rnd.randrange(per_pkg)} "
                    f"import func_{rnd.randrange(6)}"
                )
            lines.append(f"\n\nclass Service{p}x{m}:")
            for k in range(3):
                lines.append(f"    def method_{k}(self, a):")
                callee, helper = rnd.randrange(6), rnd.randrange(20)
                lines.append(f"        return func_{callee}(a) + helper_{helper}(a)")
            for k in range(6):
                lines.append(f"\n\ndef func_{k}(a):")
                body = 3 if rnd.random() > 0.03 else 90
                for i in range(body):
                    lines.append(f"    a = a + {i}")
                lines.append(f"    s = Service{p}x{m}()")
                lines.append(f"    return s.method_{k % 3}(a)")
            (pdir / f"mod{m}.py").write_text("\n".join(lines) + "\n")
            written += 1
    tdir = root / "tests"
    tdir.mkdir(exist_ok=True)
    for t in range(max(1, files // 10)):
        p, m = rnd.randrange(pkgs), rnd.randrange(per_pkg)
        (tdir / f"test_t{t}.py").write_text(
            f"from pkg{p}.mod{m} import func_0\n\n\ndef test_a{t}():\n    assert func_0(1)\n"
        )
        written += 1
    return written


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def timed(fn: Any) -> tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    return (time.perf_counter() - t0) * 1000, out


def calls(root: str, sample: dict[str, str]) -> dict[str, Any]:
    from code_review_graph import main as m

    changed = sample["changed"]
    return {
        "query_graph callers_of": lambda: m.query_graph_tool(
            pattern="callers_of", target="helper_3", repo_root=root),
        "query_graph callees_of": lambda: m.query_graph_tool(
            pattern="callees_of", target=sample["func"], repo_root=root),
        "query_graph importers_of": lambda: m.query_graph_tool(
            pattern="importers_of", target="core/util.py", repo_root=root),
        "query_graph tests_for": lambda: m.query_graph_tool(
            pattern="tests_for", target="func_0", repo_root=root),
        "get_impact_radius": lambda: m.get_impact_radius_tool(
            changed_files=[changed], repo_root=root),
        "get_architecture_overview": lambda: m.get_architecture_overview_tool(repo_root=root),
        "list_communities": lambda: m.list_communities_tool(repo_root=root),
        "list_flows": lambda: m.list_flows_tool(repo_root=root),
        "get_hub_nodes": lambda: m.get_hub_nodes_tool(repo_root=root),
        "get_bridge_nodes": lambda: m.get_bridge_nodes_tool(repo_root=root),
        "get_knowledge_gaps": lambda: m.get_knowledge_gaps_tool(repo_root=root),
        "get_surprising_connections": lambda: m.get_surprising_connections_tool(repo_root=root),
        "semantic_search_nodes": lambda: m.semantic_search_nodes_tool(
            query="service method", repo_root=root),
        "detect_changes": lambda: m.detect_changes_tool(repo_root=root),
        "detect_changes (200 files)": lambda: m.detect_changes_tool(
            changed_files=BIG, repo_root=root),
        "get_affected_flows (200 files)": lambda: m.get_affected_flows_tool(
            changed_files=BIG, repo_root=root),
        "get_review_context": lambda: m.get_review_context_tool(
            changed_files=[changed], repo_root=root),
        "find_large_functions": lambda: m.find_large_functions_tool(repo_root=root),
        "refactor rename": lambda: m.refactor_tool(
            mode="rename", old_name="func_1", new_name="func_one", repo_root=root),
        "refactor dead_code": lambda: m.refactor_tool(mode="dead_code", repo_root=root),
        "list_graph_stats": lambda: m.list_graph_stats_tool(repo_root=root),
        "get_affected_flows": lambda: m.get_affected_flows_tool(
            changed_files=[changed], repo_root=root),
        "traverse_graph": lambda: m.traverse_graph_tool(query="func_0", repo_root=root),
    }


def run_all(root: str, repeats: int, profile: str | None) -> list[dict[str, Any]]:
    from code_review_graph.tools._common import with_provenance

    sample = {"changed": "pkg1/mod1.py", "func": "func_2"}
    table = []
    for name, make in calls(root, sample).items():
        def once(make: Any = make) -> Any:
            return asyncio.run(make())

        cold, out = timed(once)
        warm = [timed(once)[0] for _ in range(repeats)]
        prov_ms = [timed(lambda: with_provenance({"x": 1}, root))[0] for _ in range(repeats)]
        size = len(json.dumps(out, default=str)) if out is not None else 0
        status = out.get("status") if isinstance(out, dict) else "?"
        table.append({
            "tool": name, "cold_ms": round(cold, 1),
            "warm_ms": round(statistics.median(warm), 1),
            "provenance_ms": round(statistics.median(prov_ms), 1),
            "bytes": size, "status": status,
        })
        if profile and profile == name:
            prof = cProfile.Profile()
            prof.enable()
            once()
            prof.disable()
            pstats.Stats(prof).sort_stats("cumulative").print_stats(25)
    return sorted(table, key=lambda r: -r["warm_ms"])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--files", type=int, default=4000)
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--profile", default=None, help="tool name to cProfile")
    a = ap.parse_args()
    root = Path(a.root)
    if not a.skip_build:
        n = generate(root, a.files)
        git(root, "init", "-q")
        git(root, "add", "-A")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
        for f in ("pkg1/mod1.py", "pkg2/mod2.py", "pkg3/mod3.py"):
            p = root / f
            p.write_text(p.read_text() + "\n\ndef added_fn(a):\n    return a\n")
        git(root, "add", "-A")
        git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "change")
        print("files written", n)
        from code_review_graph import main as m

        t, out = timed(lambda: asyncio.run(m.build_or_update_graph_tool(
            full_rebuild=True, repo_root=str(root))))
        print("build_ms", round(t), "status", out.get("status"))
    print(json.dumps(run_all(str(root), a.repeats, a.profile), indent=1))


if __name__ == "__main__":
    main()
