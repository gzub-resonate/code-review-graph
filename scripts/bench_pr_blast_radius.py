"""Synthetic benchmark for ``pr_blast_radius_tool``: build a repo, branch a PR, time the call.

The repository is synthetic: ``--files`` Python modules whose functions call each other
(a few shared hub modules are called from many places), YAML configs, and a PR branch that
modifies, adds, deletes and renames files. It measures the query only (the graph build is done
once up front and cached under ``--workdir``).

    PYTHONPATH=. uv run python scripts/bench_pr_blast_radius.py --workdir $TMPDIR/prbr \
        [--files 3000] [--pr-files 60] [--profile] [--dump result.json]

Prints wall-clock seconds per run, the number of ``git`` subprocesses the call started, and a
digest of the result so two code versions can be compared on identical input.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import os
import pstats
import random
import subprocess
import sys
import time
from pathlib import Path


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.strip()


def _module(index: int, total: int, rng: random.Random, variant: int = 0) -> str:
    # Callees are drawn mostly from the first 150 modules, so those modules have many callers.
    callees = []
    for _ in range(3):
        popular = min(150, max(1, total // 4))
        target = rng.randrange(popular) if rng.random() < 0.7 else rng.randrange(total)
        if target != index:
            callees.append((target, rng.randrange(6)))
    lines = ["from pkg import hub"]
    for target, fn in callees:
        lines.append(f"from pkg.m{target % 40}.mod_{target} import fn_{target}_{fn}")
    lines.append("")
    for fn in range(6):
        lines.append(f"def fn_{index}_{fn}(x):")
        lines.append(f"    y = x + {variant}")
        lines.append(f"    y += hub.shared_{rng.randrange(20)}(y)")
        if fn:
            lines.append(f"    y += fn_{index}_{fn - 1}(y)")
        if callees:
            target, tfn = callees[fn % len(callees)]
            lines.append(f"    y += fn_{target}_{tfn}(y)")
        lines.append("    return y")
        lines.append("")
    return "\n".join(lines)


def _hub() -> str:
    return "\n".join(f"def shared_{i}(v):\n    return v + {i}\n" for i in range(20))


def _yaml(index: int, variant: int = 0) -> str:
    keys = "\n".join(f"  key_{k}: {k + variant}" for k in range(12))
    return f"service:\n  name: svc{index}\n{keys}\nlogging:\n  level: info\n"


def build_repo(root: Path, files: int, pr_files: int) -> None:
    rng = random.Random(7)
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "main")
    pkg = root / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "hub.py").write_text(_hub())
    for i in range(files):
        sub = pkg / f"m{i % 40}"
        sub.mkdir(exist_ok=True)
        (sub / "__init__.py").write_text("")
        (sub / f"mod_{i}.py").write_text(_module(i, files, rng))
    for i in range(max(10, pr_files // 4)):
        cfg = root / "config" / f"svc{i}.yml"
        cfg.parent.mkdir(exist_ok=True)
        cfg.write_text(_yaml(i))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    from code_review_graph.graph import GraphStore
    from code_review_graph.incremental import full_build, get_db_path

    with GraphStore(get_db_path(root)) as store:
        full_build(root, store)
    _git(root, "checkout", "-q", "-b", "pr")
    rng = random.Random(11)
    popular = min(150, max(1, files // 4))
    picks = rng.sample(range(popular), pr_files // 2) + rng.sample(
        range(popular, files), pr_files - pr_files // 2,
    )
    for i in picks[: pr_files * 3 // 5]:  # modified
        path = pkg / f"m{i % 40}" / f"mod_{i}.py"
        path.write_text(_module(i, files, random.Random(7 + i), variant=1))
    for i in picks[pr_files * 3 // 5: pr_files * 4 // 5]:  # deleted
        (pkg / f"m{i % 40}" / f"mod_{i}.py").unlink()
    for i in range(pr_files // 5):  # added
        (pkg / "m0" / f"new_{i}.py").write_text(_module(files + i, files, rng))
    for i in range(max(10, pr_files // 4) // 2):  # config changed
        (root / "config" / f"svc{i}.yml").write_text(_yaml(i, variant=1))
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr")
    _git(root, "checkout", "-q", "main")


class GitCounter:
    """Counts ``git`` subprocesses started by ``subprocess.Popen`` during a run."""

    def __init__(self) -> None:
        self.count = 0
        self._orig = subprocess.Popen

    def __enter__(self) -> "GitCounter":
        counter = self
        orig = self._orig

        class Counting(orig):  # type: ignore[valid-type, misc]
            def __init__(self, args, *a, **kw):  # type: ignore[no-untyped-def]
                if args and args[0] == "git":
                    counter.count += 1
                super().__init__(args, *a, **kw)

        subprocess.Popen = Counting  # type: ignore[misc]
        return self

    def __exit__(self, *exc: object) -> None:
        subprocess.Popen = self._orig  # type: ignore[misc]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--files", type=int, default=3000)
    parser.add_argument("--pr-files", type=int, default=60)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--dump", default="")
    args = parser.parse_args()
    root = Path(args.workdir).resolve() / f"repo_{args.files}_{args.pr_files}"
    os.environ.setdefault("CRG_HOME", str(Path(args.workdir).resolve() / "home"))
    if not root.exists():
        t0 = time.perf_counter()
        build_repo(root, args.files, args.pr_files)
        print(f"built synthetic repo in {time.perf_counter() - t0:.1f}s")
    from code_review_graph.tools.pr_tools import pr_blast_radius_func

    def call() -> dict:
        return pr_blast_radius_func(
            repo_root=str(root), head="pr", base="main", include_external=False,
            files_limit=200,
        )

    result: dict = {}
    for run in range(args.runs):
        with GitCounter() as counter:
            t0 = time.perf_counter()
            result = call()
            elapsed = time.perf_counter() - t0
        print(f"run {run + 1}: {elapsed:.3f}s, git subprocesses={counter.count}")
    text = json.dumps(result, sort_keys=True, default=str)
    print("result digest:", hashlib.sha256(text.encode()).hexdigest()[:16],
          "status:", result.get("status"), "totals:", result.get("totals"))
    if args.dump:
        Path(args.dump).write_text(json.dumps(result, sort_keys=True, indent=1, default=str))
    if args.profile:
        prof = cProfile.Profile()
        prof.enable()
        call()
        prof.disable()
        pstats.Stats(prof).sort_stats("cumulative").print_stats(25)
        pstats.Stats(prof).sort_stats("tottime").print_stats(15)
    return 0


if __name__ == "__main__":
    sys.exit(main())
