#!/usr/bin/env python3
"""Print the test files of one shard, one path per line.

Reads ``pytest --collect-only -q`` output on stdin (node ids), counts tests
per file and assigns files to shards with a deterministic greedy split
(largest file first, to the lightest shard, ties broken by name). Every file
lands in exactly one shard.

    pytest --collect-only -q -m "..." | shard_tests.py <index> <total>
"""

from __future__ import annotations

import sys
from collections import Counter


def split(counts: Counter[str], total: int) -> list[list[str]]:
    shards: list[list[str]] = [[] for _ in range(total)]
    load = [0] * total
    for path, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        i = min(range(total), key=lambda j: (load[j], j))
        shards[i].append(path)
        load[i] += n
    return [sorted(s) for s in shards]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    index, total = int(argv[0]), int(argv[1])
    if not 0 <= index < total:
        print("index must be in [0, total)", file=sys.stderr)
        return 2
    counts: Counter[str] = Counter()
    for line in sys.stdin:
        if "::" in line and not line.startswith(" "):
            counts[line.split("::", 1)[0].strip()] += 1
    if not counts:
        print("no tests collected", file=sys.stderr)
        return 1
    print("\n".join(split(counts, total)[index]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
