"""Detect bundled or minified JavaScript/TypeScript output so the graph can skip it.

Compiled GitHub Action bundles (``dist/index.js`` built by ncc or webpack) are
committed to many repositories. Their thousands of vendored functions swamp the
architecture overview, the large-function list and the surprising-connection
ranking. A file is treated as bundled output when any of these holds:

* it sits below a ``dist``, ``build``, ``out`` or ``bundle`` folder AND its first
  2 KB or last 1 KB carry a bundler signature (webpack or ncc runtime,
  a ``/******/`` banner, ``//# sourceMappingURL=``, a ``/*! For license`` banner,
  an esbuild-style ``// ... bundled`` header);
* its average line length is extreme (a minified file), wherever it lives;
* the repository's root ``.gitattributes`` marks it ``linguist-generated``.

Hand-written source, and a committed ``dist/`` without any signature, is kept.
Set ``CRG_SKIP_BUNDLED_JS=0`` to turn the rule off.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

SKIP_BUNDLED_JS_ENV = "CRG_SKIP_BUNDLED_JS"

JS_TS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts"})
OUTPUT_DIR_NAMES = frozenset({"dist", "build", "out", "bundle"})

HEAD_BYTES = 2048
TAIL_BYTES = 1024
#: Bytes sampled to estimate the average line length.
MINIFIED_SAMPLE_BYTES = 65536
#: Files under this size are never judged minified.
MINIFIED_MIN_BYTES = 4096
#: A file whose sampled lines average more than this many characters is minified.
MINIFIED_AVG_LINE_LENGTH = 500

_SIGNATURES = (
    b"__webpack_require__",
    b"__nccwpck_require__",
    b"webpackBootstrap",
    b"/******/",
    b"//# sourceMappingURL=",
    b"/*! For license",
)
_BUNDLED_HEADER = re.compile(rb"^//[^\n]*\bbundled\b", re.MULTILINE | re.IGNORECASE)


def bundled_js_skip_enabled() -> bool:
    """Whether the rule is on (default). ``CRG_SKIP_BUNDLED_JS=0`` turns it off."""
    raw = os.environ.get(SKIP_BUNDLED_JS_ENV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _has_signature(head: bytes, tail: bytes) -> bool:
    if any(sig in head or sig in tail for sig in _SIGNATURES):
        return True
    return bool(_BUNDLED_HEADER.search(head))


def _generated_patterns(repo_root: Path) -> list[str]:
    """Globs of the root ``.gitattributes`` lines that set ``linguist-generated``."""
    try:
        text = (repo_root / ".gitattributes").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    patterns: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        for attr in fields[1:]:
            if attr in ("linguist-generated", "linguist-generated=true"):
                patterns.append(fields[0])
                break
    return patterns


def _matches_gitattributes(normalized: str, patterns: list[str]) -> bool:
    base = PurePosixPath(normalized).name
    for pattern in patterns:
        glob = pattern.lstrip("/")
        if glob.endswith("/"):
            glob += "**"
        if "/" not in glob:
            if fnmatch.fnmatch(base, glob):
                return True
        elif fnmatch.fnmatch(normalized, glob) or (
            glob.endswith("/**") and normalized.startswith(glob[:-2])
        ):
            return True
    return False


def is_bundled_js(repo_root: Path, rel_path: str) -> bool:
    """Whether *rel_path* is bundled, minified or declared-generated JS/TS output.

    Cheap for everything else: a suffix check first, then a read of at most the
    sample sizes above. Unreadable files are never judged bundled.
    """
    normalized = rel_path.replace("\\", "/").lstrip("/")
    posix = PurePosixPath(normalized)
    if posix.suffix.lower() not in JS_TS_SUFFIXES:
        return False
    if not bundled_js_skip_enabled():
        return False
    if (repo_root / ".gitattributes").exists() and _matches_gitattributes(
        normalized, _generated_patterns(repo_root)
    ):
        return True
    in_output_dir = any(part in OUTPUT_DIR_NAMES for part in posix.parts[:-1])
    try:
        with open(repo_root / normalized, "rb") as handle:
            sample = handle.read(MINIFIED_SAMPLE_BYTES)
            size = os.fstat(handle.fileno()).st_size
            if in_output_dir:
                if size > len(sample):
                    handle.seek(max(size - TAIL_BYTES, 0))
                    tail = handle.read(TAIL_BYTES)
                else:
                    tail = sample[-TAIL_BYTES:]
                if _has_signature(sample[:HEAD_BYTES], tail):
                    return True
    except OSError:
        return False
    if size >= MINIFIED_MIN_BYTES and len(sample) / (sample.count(b"\n") + 1) > (
        MINIFIED_AVG_LINE_LENGTH
    ):
        return True
    return False
