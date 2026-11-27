"""Opt-in background warm-up of the heavy cross-repo origins.

The first ``cross_repo_requests_tool`` call for ``awssdk``, ``feign`` or ``js`` after a start or
a reindex reads every repo's graph, which is minutes of work; later calls are served from the
caches in ``repo_cache``. With ``CRG_CROSS_REPO_WARMUP=1`` a daemon thread makes that first call
itself, so a client does not.

* It runs once after the server starts and again whenever a registered repo's graph stamp
  changes (the indexer published), once the stamps have stopped moving for a poll.
* It makes the same call a client would (unscoped, default page size), one origin at a time,
  so it fills ``MEMO`` and ``INFRASTRUCTURE_RESULTS`` through the shared bounded pool and
  ``SingleFlight``: a real request that arrives meanwhile shares the work, never repeats it.
* A pass stops starting origins after ``CRG_CROSS_REPO_WARMUP_MAX_SECONDS`` (default 1800). An
  origin already running is bounded by ``CRG_CROSS_REPO_BUDGET_SECONDS`` as any call is.
* It never raises into the server and logs one line per origin with the seconds it took.

``CRG_CROSS_REPO_WARMUP_ORIGINS`` is a comma list (default ``awssdk,feign,js``).
``CRG_CROSS_REPO_WARMUP_INTERVAL_SECONDS`` (default 60) is the poll for changed graphs.

A second, independent warm-up reads each registered repo's Git state. Change discovery
(``git diff`` against the base, then a scan for untracked files) stats every tracked file, and on
a freshly started task whose disk cache is cold that takes longer than the discovery timeout on a
large repo: measured, 17.7 s for the first run on one repo and 0.05 s for the next. A client's first
``get_impact_radius_tool`` or ``detect_changes_tool`` call then failed with "git timed out". With
``CRG_GIT_WARMUP=1`` a daemon thread runs those two read-only commands once per repo after the
server starts, so the disk cache is warm before a client asks. It stops starting repos after
``CRG_GIT_WARMUP_MAX_SECONDS`` (default 900), never raises into the server and sets
``GIT_OPTIONAL_LOCKS=0`` so the commands cannot write to a repository.

Query-only: nothing here is imported by the build code.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .constants import env_float

logger = logging.getLogger(__name__)

WARMUP_ENV = "CRG_CROSS_REPO_WARMUP"
ORIGINS_ENV = "CRG_CROSS_REPO_WARMUP_ORIGINS"
MAX_SECONDS_ENV = "CRG_CROSS_REPO_WARMUP_MAX_SECONDS"
INTERVAL_ENV = "CRG_CROSS_REPO_WARMUP_INTERVAL_SECONDS"
DEFAULT_ORIGINS = ("awssdk", "feign", "js")
DEFAULT_MAX_SECONDS = 1800.0
DEFAULT_INTERVAL_SECONDS = 60.0
#: Origins ``cross_repo_requests_tool`` accepts; anything else in the list is ignored.
KNOWN_ORIGINS = frozenset({
    "awssdk", "feign", "js", "stepfunction", "terraform", "workflow", "amqp", "trigger",
    "external", "consumers",
})


def warmup_enabled() -> bool:
    return os.environ.get(WARMUP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def warmup_origins() -> list[str]:
    raw = os.environ.get(ORIGINS_ENV)
    if raw is None or not raw.strip():
        return list(DEFAULT_ORIGINS)
    wanted: list[str] = []
    for item in raw.split(","):
        name = item.strip().lower()
        if not name or name in wanted:
            continue
        if name not in KNOWN_ORIGINS:
            logger.warning("Ignoring unknown %s entry %r.", ORIGINS_ENV, name)
            continue
        wanted.append(name)
    return wanted


def registry_signature() -> tuple[Any, ...]:
    """The registered repos and the graph stamp of each: changes when any graph is published."""
    from .registry import Registry
    from .repo_cache import graph_stamp

    return tuple(
        (str(entry["path"]), graph_stamp(entry)) for entry in Registry().list_repos()
    )


def _default_call(origin: str) -> Any:
    from .tools.registry_tools import cross_repo_requests_func

    return cross_repo_requests_func(origin=origin)


class Warmer:
    """The warm-up loop; ``start`` it once, ``stop`` it on shutdown."""

    def __init__(
        self,
        *,
        origins: list[str] | None = None,
        call: Callable[[str], Any] | None = None,
        signature: Callable[[], tuple[Any, ...]] | None = None,
        max_seconds: float | None = None,
        interval: float | None = None,
        pause: float = 1.0,
        initial_delay: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.origins = list(origins) if origins is not None else warmup_origins()
        self._call = call or _default_call
        self._signature = signature or registry_signature
        self.max_seconds = (
            env_float(MAX_SECONDS_ENV, DEFAULT_MAX_SECONDS) if max_seconds is None else max_seconds
        )
        self.interval = (
            env_float(INTERVAL_ENV, DEFAULT_INTERVAL_SECONDS) if interval is None else interval
        )
        self.pause = pause
        self.initial_delay = initial_delay
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.passes = 0
        self.warmed_signature: tuple[Any, ...] | None = None

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self._loop, name="crg-xrepo-warmup", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _safe_signature(self) -> tuple[Any, ...] | None:
        try:
            return self._signature()
        except Exception:  # never raise into the server
            logger.warning("Cross-repo warm-up could not read the registry.", exc_info=True)
            return None

    def _loop(self) -> None:
        try:
            if self._stop.wait(self.initial_delay):
                return
            pending: tuple[Any, ...] | None = None
            while not self._stop.is_set():
                current = self._safe_signature()
                if current is not None and current != self.warmed_signature:
                    # Wait until the stamps hold still for one poll: an indexer is still
                    # publishing while they move, and a pass then would read half a reindex.
                    if current == pending:
                        self.run_pass()
                        self.warmed_signature = current
                        pending = None
                        continue
                    pending = current
                    wait = min(self.interval, 5.0) if self.warmed_signature is None else (
                        self.interval
                    )
                    if self._stop.wait(wait):
                        return
                    continue
                pending = None
                if self._stop.wait(self.interval):
                    return
        except Exception:
            logger.warning("Cross-repo warm-up stopped on an error.", exc_info=True)

    def run_pass(self) -> list[tuple[str, float, str]]:
        """Warm each origin in turn; ``[(origin, seconds, outcome)]``. Never raises."""
        self.passes += 1
        started = self._clock()
        report: list[tuple[str, float, str]] = []
        for index, origin in enumerate(self.origins):
            if self._stop.is_set():
                break
            if self._clock() - started >= self.max_seconds:
                logger.info(
                    "Cross-repo warm-up: stopped after %.0fs cap; not warmed: %s",
                    self._clock() - started, ",".join(self.origins[index:]),
                )
                break
            begun = self._clock()
            try:
                reply = self._call(origin)
                status = reply.get("status", "ok") if isinstance(reply, dict) else "ok"
                outcome = str(status)
                if isinstance(reply, dict) and reply.get("truncated"):
                    outcome += " (truncated: not fully cached)"
            except Exception as exc:
                outcome = f"failed: {type(exc).__name__}"
            seconds = self._clock() - begun
            report.append((origin, seconds, outcome))
            logger.info("Cross-repo warm-up: origin=%s %.1fs %s", origin, seconds, outcome)
            if self._stop.wait(self.pause):
                break
        return report


def start_warmup() -> Warmer | None:
    """Start the warm-up when ``CRG_CROSS_REPO_WARMUP`` is on; otherwise do nothing."""
    try:
        if not warmup_enabled():
            return None
        warmer = Warmer()
        if not warmer.origins:
            return None
        warmer.start()
        logger.info("Cross-repo warm-up on for: %s", ",".join(warmer.origins))
        return warmer
    except Exception:
        logger.warning("Cross-repo warm-up could not start.", exc_info=True)
        return None


GIT_WARMUP_ENV = "CRG_GIT_WARMUP"
GIT_MAX_SECONDS_ENV = "CRG_GIT_WARMUP_MAX_SECONDS"
DEFAULT_GIT_MAX_SECONDS = 900.0
#: One command may run this long: far above the cold 17.7 s measured, below a hang.
_GIT_COMMAND_TIMEOUT = 120.0
#: The read-only commands change discovery runs (``incremental.get_changed_files``): a diff of the
#: working tree against the previous commit, then the scan for untracked files.
GIT_STATE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("git", "diff", "--relative", "--name-status", "-z", "HEAD~1", "--", "."),
    ("git", "ls-files", "--others", "--exclude-standard", "-z"),
)


def git_warmup_enabled() -> bool:
    return os.environ.get(GIT_WARMUP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def warm_git_state(
    paths: list[str],
    *,
    max_seconds: float | None = None,
    stop: threading.Event | None = None,
    run: Callable[..., Any] = subprocess.run,
    clock: Callable[[], float] = time.monotonic,
) -> list[tuple[str, float, str]]:
    """Run the change-discovery Git commands once in each repo; ``[(path, seconds, outcome)]``.

    A path with no ``.git`` is skipped. A command that times out or cannot start ends that repo
    and the next one is tried. Never raises.
    """
    cap = env_float(GIT_MAX_SECONDS_ENV, DEFAULT_GIT_MAX_SECONDS) if max_seconds is None else (
        max_seconds
    )
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    started = clock()
    report: list[tuple[str, float, str]] = []
    for path in paths:
        if stop is not None and stop.is_set():
            break
        if clock() - started >= cap:
            logger.info(
                "Git-state warm-up: stopped after %.0fs cap with %d of %d repo(s) done",
                clock() - started, len(report), len(paths),
            )
            break
        if not (Path(path) / ".git").exists():
            continue
        begun = clock()
        outcome = "ok"
        for command in GIT_STATE_COMMANDS:
            try:
                run(
                    list(command), cwd=path, capture_output=True, timeout=_GIT_COMMAND_TIMEOUT,
                    stdin=subprocess.DEVNULL, env=env, check=False,
                )
            except subprocess.TimeoutExpired:
                outcome = "timeout"
                break
            except OSError as exc:
                outcome = f"failed: {type(exc).__name__}"
                break
        report.append((path, clock() - begun, outcome))
    slowest = sorted(report, key=lambda row: row[1], reverse=True)[:3]
    logger.info(
        "Git-state warm-up: %d repo(s) in %.1fs; slowest: %s", len(report), clock() - started,
        ", ".join(f"{Path(p).name} {s:.1f}s {o}" for p, s, o in slowest) or "none",
    )
    return report


def _run_git_warmup(paths: list[str]) -> None:
    try:
        warm_git_state(paths)
    except Exception:  # never raise into the server
        logger.warning("Git-state warm-up stopped on an error.", exc_info=True)


def start_git_warmup() -> threading.Thread | None:
    """Start the Git-state warm-up when ``CRG_GIT_WARMUP`` is on; otherwise do nothing."""
    try:
        if not git_warmup_enabled():
            return None
        from .registry import Registry

        paths = [str(entry["path"]) for entry in Registry().list_repos()]
        if not paths:
            return None
        thread = threading.Thread(
            target=_run_git_warmup, args=(paths,), name="crg-git-warmup", daemon=True,
        )
        thread.start()
        logger.info("Git-state warm-up on for %d repo(s)", len(paths))
        return thread
    except Exception:
        logger.warning("Git-state warm-up could not start.", exc_info=True)
        return None
