#!/usr/bin/env python3
"""Compare mypy or bandit findings with a committed baseline.

Findings are reduced to ``<file>:<code>`` keys with no line number, so edits
that only move a finding do not trip the check. The baseline is a multiset:
a key listed twice allows two findings with that key, and a third one fails.
Fixing a finding never fails; run ``--update`` to shrink the baseline.

    baseline_check.py mypy   <baseline> <tool-output-file>
    baseline_check.py bandit <baseline> <bandit-json-file>
    baseline_check.py <tool> <baseline> <input> --update   # rewrite baseline
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

_MYPY_LINE = re.compile(r"^(?P<file>[^:\s][^:]*\.pyi?):\d+(?::\d+)?: error: .*\[(?P<code>[\w-]+)\]\s*$")


def mypy_keys(text: str) -> list[str]:
    keys = []
    for line in text.splitlines():
        m = _MYPY_LINE.match(line)
        if m:
            keys.append(f"{m['file']}:{m['code']}")
        elif ": error:" in line:
            # An error without a [code] suffix would slip past the baseline.
            keys.append(f"{line.split(':', 1)[0]}:no-code")
    return keys


def bandit_keys(text: str) -> list[str]:
    data = json.loads(text)
    return [f"{r['filename']}:{r['test_id']}" for r in data.get("results", [])]


def main(argv: list[str]) -> int:
    update = "--update" in argv
    args = [a for a in argv if a != "--update"]
    if len(args) != 3 or args[0] not in ("mypy", "bandit"):
        print(__doc__, file=sys.stderr)
        return 2
    tool, baseline_path, input_path = args[0], Path(args[1]), Path(args[2])
    text = Path(input_path).read_text(encoding="utf-8")
    current = Counter(mypy_keys(text) if tool == "mypy" else bandit_keys(text))

    if update:
        lines = sorted(current.elements())
        baseline_path.write_text("".join(f"{k}\n" for k in lines), encoding="utf-8")
        print(f"{tool}: wrote {len(lines)} baseline entries to {baseline_path}")
        return 0

    base = Counter(
        ln.strip()
        for ln in baseline_path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    )
    new = current - base
    fixed = base - current
    print(f"{tool}: {sum(current.values())} findings, baseline {sum(base.values())}")
    if fixed:
        print(f"{tool}: {sum(fixed.values())} baseline entries no longer occur "
              "(shrink the baseline with --update):")
        for k, n in sorted(fixed.items()):
            print(f"  - {k} x{n}")
    if new:
        print(f"::error::{tool}: {sum(new.values())} finding(s) beyond the baseline")
        for k, n in sorted(new.items()):
            print(f"  + {k} x{n}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
