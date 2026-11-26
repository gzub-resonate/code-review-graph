"""Client address / User-Agent logging for the ``serve --http`` MCP endpoint.

Behind a load balancer the uvicorn access log only shows the balancer's address.
This pure ASGI middleware emits one INFO line per request with the direct peer,
the resolved client address and the User-Agent, so operators can tell which
machine called the server.

It is a pure ASGI middleware (not ``BaseHTTPMiddleware``) so streaming/SSE
responses are never buffered, and it never reads the request body. It logs when
the response starts, so the status is known.

Only the peer, ``X-Forwarded-For``, ``User-Agent``, method and path are logged;
every value is sanitised and truncated. Authorization, cookies, query strings and
bodies are never read. Set ``CRG_HTTP_CLIENT_LOG=0`` (or ``false``/``off``) to
disable. Set ``CRG_TRUSTED_PROXIES`` to comma-separated CIDRs to trust
``X-Forwarded-For`` only from those proxies (default: any private or loopback
peer); the raw peer and header are always logged beside the resolved address.

Health probes are not client calls and would bury them: ``GET /health`` and a ``GET /mcp``
answered 406 (a probe without the streaming ``Accept`` header) are logged at DEBUG, and counted
in one INFO line (``mcp_probe_summary``) at most every ``CRG_PROBE_SUMMARY_SECONDS`` (default
300). A POST, and any GET that gets another status, is logged as before.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import threading
import time

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .constants import env_float

#: uvicorn configures this logger to stdout, which container platforms ship.
logger = logging.getLogger("uvicorn.error")

ENV_VAR = "CRG_HTTP_CLIENT_LOG"
TRUSTED_PROXIES_ENV = "CRG_TRUSTED_PROXIES"
_FALSE_VALUES = frozenset({"0", "false", "off"})
_MAX_UA = 200
_MAX_XFF = 200
_MAX_PATH = 200
_HEALTH_CHECK_PREFIX = "elb-healthchecker"
PROBE_SUMMARY_ENV = "CRG_PROBE_SUMMARY_SECONDS"
_DEFAULT_PROBE_SUMMARY_SECONDS = 300.0
_NOT_ACCEPTABLE = 406


def _summary_interval() -> float:
    value = env_float(PROBE_SUMMARY_ENV, _DEFAULT_PROBE_SUMMARY_SECONDS)
    return value if value >= 0 else _DEFAULT_PROBE_SUMMARY_SECONDS


class _ProbeCounter:
    """Count quiet probe requests and say how many came in, at most once per interval."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0
        self._since = time.monotonic()

    def record(self) -> None:
        interval = _summary_interval()
        now = time.monotonic()
        with self._lock:
            self._count += 1
            if now - self._since < interval:
                return
            count, seconds = self._count, now - self._since
            self._count, self._since = 0, now
        logger.info(
            "mcp_probe_summary probes=%d window_seconds=%d "
            "(GET /health and GET /mcp answered 406; shown individually at DEBUG)",
            count, int(seconds),
        )


def is_enabled() -> bool:
    """Return ``False`` when ``CRG_HTTP_CLIENT_LOG`` is 0/false/off."""
    return os.environ.get(ENV_VAR, "").strip().lower() not in _FALSE_VALUES


def sanitize(value: str, limit: int) -> str:
    """Drop control characters and quotes, then truncate to ``limit`` chars."""
    cleaned = "".join(
        ch for ch in value if ch.isprintable() and ch not in '"\\'
    )
    return cleaned[:limit]


def _parse_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def trusted_proxy_networks() -> list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None:
    """Parse ``CRG_TRUSTED_PROXIES`` (comma-separated CIDRs or addresses).

    ``None`` means the variable is unset and the default applies (any private or
    loopback peer is treated as a proxy). Unparseable entries are ignored; when
    every entry is invalid the list is empty, so no peer is trusted.
    """
    raw = os.environ.get(TRUSTED_PROXIES_ENV, "").strip()
    if not raw:
        return None
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in raw.split(","):
        try:
            networks.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted_proxy(
    peer_ip: ipaddress.IPv4Address | ipaddress.IPv6Address | None,
    trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None,
) -> bool:
    if peer_ip is None:
        return False
    if trusted is None:
        return peer_ip.is_private or peer_ip.is_loopback
    return any(peer_ip in network for network in trusted if network.version == peer_ip.version)


def resolve_client(
    peer: str,
    xff: str,
    trusted: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None,
) -> str:
    """Return the client address to attribute the request to.

    ``X-Forwarded-For`` is trusted only when the direct peer is a trusted proxy
    and then only its LAST entry: that is the address the proxy itself observed;
    earlier entries are client-supplied. ``trusted`` is the parsed
    ``CRG_TRUSTED_PROXIES`` list; when it is ``None`` (the variable is unset) any
    private or loopback peer counts as the internal load balancer. This is a
    diagnostic field: the raw peer and the full header are always logged beside it.
    """
    peer_ip = _parse_ip(peer)
    if not _is_trusted_proxy(peer_ip, trusted):
        return peer
    if not xff:
        return peer
    last = _parse_ip(xff.rsplit(",", 1)[-1])
    return str(last) if last is not None else peer


class ClientAccessLog:
    """Log the real client address and User-Agent of each HTTP request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.enabled = is_enabled()
        self.trusted = trusted_proxy_networks()
        self.probes = _ProbeCounter()
        if self.enabled:
            # uvicorn configures its loggers after the middleware is built, so attach
            # the filter again here, when the app starts, to be sure it is in place.
            _quiet_uvicorn_probe_lines()

    def _is_probe(self, scope: Scope, status: object, peer: str) -> bool:
        """A ``GET /health``, or a ``GET /mcp`` a proxy peer got 406 for (no streaming Accept)."""
        if scope.get("method") != "GET":
            return False
        path = str(scope.get("path", ""))
        if path == "/health":
            return True
        return (
            status == _NOT_ACCEPTABLE
            and path.rstrip("/") == "/mcp"
            and _is_trusted_proxy(_parse_ip(peer), self.trusted)
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.enabled or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
            if key in (b"user-agent", b"x-forwarded-for")
        }
        ua = headers.get("user-agent", "")
        # Skip only real health-check traffic: a GET from a trusted proxy peer. The
        # User-Agent alone is client-controlled, so a caller must not be able to
        # drop its own requests from the log by sending the health checker's name.
        if ua.lower().startswith(_HEALTH_CHECK_PREFIX) and scope.get("method") == "GET":
            health_client = scope.get("client")
            health_peer = _parse_ip(str(health_client[0])) if health_client else None
            if _is_trusted_proxy(health_peer, self.trusted):
                await self.app(scope, receive, send)
                return

        logged = False

        def emit(status: object) -> None:
            nonlocal logged
            logged = True
            try:
                client = scope.get("client")
                peer = str(client[0]) if client else "-"
                xff = headers.get("x-forwarded-for", "")
                level = logging.INFO
                if self._is_probe(scope, status, peer):
                    self.probes.record()
                    level = logging.DEBUG
                logger.log(
                    level,
                    'mcp_client peer=%s client=%s method=%s path="%s" status=%s ua="%s" xff="%s"',
                    sanitize(peer, 64),
                    sanitize(resolve_client(peer, xff, self.trusted), 64),
                    sanitize(str(scope.get("method", "-")), 16),
                    sanitize(str(scope.get("path", "-")), _MAX_PATH),
                    status,
                    sanitize(ua, _MAX_UA),
                    sanitize(xff, _MAX_XFF),
                )
            except Exception:  # logging must never break a request
                pass

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and not logged:
                emit(message.get("status", "-"))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if not logged:
                emit("-")


class _UvicornProbeFilter(logging.Filter):
    """Drop uvicorn's own access line for the requests ``ClientAccessLog`` treats as probes.

    uvicorn logs ``peer - "GET /mcp HTTP/1.1" 406`` for every request on its own logger,
    so a load balancer's probes kept filling the log even after this middleware stopped
    logging them. Same rule as ``ClientAccessLog._is_probe``: ``GET /health`` always, and
    ``GET /mcp`` answered 406 only when the peer is a trusted proxy. Everything else,
    including every POST, is kept.
    """

    def __init__(self) -> None:
        super().__init__()
        self.trusted = trusted_proxy_networks()

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        client, method, target, _http_version, status = args
        if method != "GET":
            return True
        path = str(target).split("?", 1)[0]
        if path == "/health":
            return False
        if status != _NOT_ACCEPTABLE or path.rstrip("/") != "/mcp":
            return True
        peer = str(client).rsplit(":", 1)[0].strip("[]")
        return not _is_trusted_proxy(_parse_ip(peer), self.trusted)


def _quiet_uvicorn_probe_lines() -> None:
    """Attach the probe filter to uvicorn's access logger once."""
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _UvicornProbeFilter) for f in access.filters):
        access.addFilter(_UvicornProbeFilter())


def build_client_log_middleware() -> list[Middleware]:
    """Return the client-logging middleware (empty when disabled)."""
    if not is_enabled():
        return []
    _quiet_uvicorn_probe_lines()
    return [Middleware(ClientAccessLog)]
