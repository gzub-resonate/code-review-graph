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
disable.
"""

from __future__ import annotations

import ipaddress
import logging
import os

from starlette.middleware import Middleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: uvicorn configures this logger to stdout, which container platforms ship.
logger = logging.getLogger("uvicorn.error")

ENV_VAR = "CRG_HTTP_CLIENT_LOG"
_FALSE_VALUES = frozenset({"0", "false", "off"})
_MAX_UA = 200
_MAX_XFF = 200
_MAX_PATH = 200
_HEALTH_CHECK_PREFIX = "elb-healthchecker"


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


def resolve_client(peer: str, xff: str) -> str:
    """Return the client address to attribute the request to.

    ``X-Forwarded-For`` is trusted only when the direct peer is private or
    loopback (an internal load balancer), and then only its LAST entry: that is
    the address the balancer itself observed; earlier entries are client-supplied.
    """
    peer_ip = _parse_ip(peer)
    if peer_ip is None or not (peer_ip.is_private or peer_ip.is_loopback):
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
        if ua.lower().startswith(_HEALTH_CHECK_PREFIX):
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
                logger.info(
                    'mcp_client peer=%s client=%s method=%s path=%s status=%s ua="%s" xff="%s"',
                    sanitize(peer, 64),
                    sanitize(resolve_client(peer, xff), 64),
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


def build_client_log_middleware() -> list[Middleware]:
    """Return the client-logging middleware (empty when disabled)."""
    return [Middleware(ClientAccessLog)] if is_enabled() else []
