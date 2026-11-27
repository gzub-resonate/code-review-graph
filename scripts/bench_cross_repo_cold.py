"""Cold-path benchmark of ``cross_repo_requests_func`` on a SYNTHETIC registry.

Builds ``--repos`` graph databases (each with ``--servers`` server endpoints, ``--feign`` Feign
clients and ``--js`` JavaScript clients, stored endpoint index included), registers them in a
throwaway ``CRG_HOME`` and times the first (cold) and the second (warm) call in a fresh
process.  ``--profile`` prints the top cumulative costs of the cold call.  The numbers are
for the machine it runs on; a network file system (EFS) is not modelled.

    PYTHONPATH=. uv run python scripts/bench_cross_repo_cold.py --repos 40 --profile
"""

from __future__ import annotations

import argparse
import cProfile
import os
import pstats
import random
import shutil
import tempfile
import time
from pathlib import Path


def filler(db: Path, nodes: int, edges: int) -> None:
    """Pad a graph with ordinary Function nodes and CALLS edges (raw SQL, same columns)."""
    import sqlite3

    conn = sqlite3.connect(str(db))
    now = time.time()
    conn.executemany(
        "INSERT INTO nodes (kind, name, qualified_name, file_path, line_start, line_end, "
        "language, extra, updated_at) VALUES ('Function', ?, ?, ?, 1, 2, 'java', '{}', ?)",
        ((f"f{i}", f"/x/F{i % 900}.java::f{i}", f"/x/F{i % 900}.java", now)
         for i in range(nodes)),
    )
    conn.executemany(
        "INSERT INTO edges (kind, source_qualified, target_qualified, file_path, line, extra, "
        "updated_at) VALUES ('CALLS', ?, ?, ?, 1, '{}', ?)",
        ((f"/x/F{i % 900}.java::f{i % nodes}", f"/x/F{(i * 7) % 900}.java::f{(i * 7) % nodes}",
          f"/x/F{i % 900}.java", now) for i in range(edges)),
    )
    conn.commit()
    conn.close()


def build(root: Path, repos: int, servers: int, feign: int, js: int,
          fill_nodes: int = 0, fill_edges: int = 0) -> None:
    from code_review_graph.endpoint_index import build_endpoint_index
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path
    from code_review_graph.parser import NodeInfo
    from code_review_graph.registry import Registry

    rnd = random.Random(7)
    registry = Registry()
    for r in range(repos):
        name = f"svc{r:03d}"
        repo = (root / name).resolve()
        (repo / ".git").mkdir(parents=True)
        path = str(repo / "A.java")
        with GraphStore(get_db_path(repo)) as store:
            store.upsert_node(
                NodeInfo(kind="File", name=path, file_path=path, line_start=1, line_end=1)
            )
            for i in range(servers):
                store.upsert_node(NodeInfo(
                    kind="Endpoint", name=f"s{i}", file_path=path, line_start=i + 1,
                    line_end=i + 1, extra={
                        "role": "server", "http_method": rnd.choice(["GET", "POST", "PUT"]),
                        "route": f"/api/{name}/v{rnd.randint(1, 3)}/res{i}/{{id}}"}))
            for i in range(feign):
                target = f"svc{rnd.randrange(repos):03d}"
                store.upsert_node(NodeInfo(
                    kind="Endpoint", name=f"c{i}", file_path=path, line_start=5000 + i,
                    line_end=5000 + i, extra={
                        "role": "client", "client": "feign", "service": target,
                        "http_method": "GET",
                        "route": f"/api/{target}/v1/res{rnd.randrange(servers)}/{{id}}"}))
            for i in range(js):
                target = f"svc{rnd.randrange(repos):03d}"
                store.upsert_node(NodeInfo(
                    kind="Endpoint", name=f"j{i}", file_path=str(repo / "a.ts"),
                    line_start=9000 + i, line_end=9000 + i, language="typescript", extra={
                        "role": "client", "client": "js", "http_method": "GET",
                        "route": f"/api/{target}/v1/res{rnd.randrange(servers)}/{i}"}))
            store.commit()
            build_endpoint_index(store, repo)
        if fill_nodes:
            filler(get_db_path(repo), fill_nodes, fill_edges)
        registry.register(str(repo), alias=name)


def steps(root: Path) -> None:
    """Time each step of the cold read of every repo, summed (single thread, cold process)."""
    import json

    from code_review_graph import repo_cache
    from code_review_graph.endpoint_index import _client, _server, attach, endpoint_stamp
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import get_db_path
    from code_review_graph.registry import Registry

    totals: dict[str, float] = {}

    def lap(name: str, t0: float) -> float:
        now = time.perf_counter()
        totals[name] = totals.get(name, 0.0) + now - t0
        return now

    for entry in Registry().list_repos():
        db = get_db_path(Path(entry["path"]), read_only=True)
        t = time.perf_counter()
        repo_cache._STAMP_MEMO.clear()
        repo_cache.graph_stamp(entry)
        t = lap("graph_stamp (marker: MAX/COUNT on nodes+edges)", t)
        store = GraphStore(db, read_only=True)
        t = lap("GraphStore open (+ schema checks)", t)
        stamp = endpoint_stamp(store)
        t = lap("endpoint_stamp query", t)
        raw = store.get_metadata("endpoint_index")
        t = lap("get_metadata(endpoint_index)", t)
        data = json.loads(raw)
        t = lap("json.loads", t)
        assert data["stamp"] == stamp
        clients = [_client(r) for r in data["clients"]]
        servers = [_server(r) for r in data["servers"]]
        t = lap("rows -> dataclasses", t)
        attach(entry["alias"], Path(entry["path"]), (clients, servers))
        t = lap("attach", t)
        store.close()
        lap("GraphStore close", t)
    for name, sec in totals.items():
        print(f"{sec * 1000:9.1f} ms  {name}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repos", type=int, default=40)
    ap.add_argument("--servers", type=int, default=300)
    ap.add_argument("--feign", type=int, default=150)
    ap.add_argument("--js", type=int, default=150)
    ap.add_argument("--fill-nodes", type=int, default=0)
    ap.add_argument("--fill-edges", type=int, default=0)
    ap.add_argument("--steps", action="store_true", help="time each step of one repo's read")
    ap.add_argument("--origin", default=None)
    ap.add_argument("--profile", action="store_true")
    ap.add_argument("--keep", default=None, help="reuse/keep this directory")
    args = ap.parse_args()
    root = Path(args.keep or tempfile.mkdtemp(prefix="crg-bench-"))
    os.environ["CRG_HOME"] = str(root / "home")
    os.environ.pop("CRG_DATA_DIR", None)
    os.environ["CRG_CROSS_REPO_BUDGET_SECONDS"] = "0"
    if not (root / "home").exists():
        build(root, args.repos, args.servers, args.feign, args.js,
              args.fill_nodes, args.fill_edges)
    from code_review_graph.tools.registry_tools import cross_repo_requests_func

    if args.steps:
        steps(root)
        return

    def run() -> dict:
        t0 = time.perf_counter()
        reply = cross_repo_requests_func(origin=args.origin, max_results=100)
        return {"seconds": round(time.perf_counter() - t0, 3), "total": reply["total"],
                "counts": reply["counts"], "skipped": reply["skipped_repos_total"],
                "unreadable": reply["unreadable_total"]}

    if args.profile:
        prof = cProfile.Profile()
        prof.enable()
    print("cold", run())
    if args.profile:
        prof.disable()
        pstats.Stats(prof).sort_stats("cumulative").print_stats(28)
    print("warm", run())
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
