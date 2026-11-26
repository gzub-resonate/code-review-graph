"""Tests for the HTTP client access-log middleware."""

from __future__ import annotations

import asyncio
import logging

import pytest

from code_review_graph.http_access_log import (
    ClientAccessLog,
    build_client_log_middleware,
    resolve_client,
    sanitize,
)

LOGGER = "uvicorn.error"


async def _app(scope, receive, send):
    if scope["type"] != "http":
        scope["touched"] = True
        return
    await send({"type": "http.response.start", "status": 202, "headers": []})
    await send({"type": "http.response.body", "body": b"hello", "more_body": True})
    await send({"type": "http.response.body", "body": b"!"})


def _call(app, headers, peer="10.0.0.5", method="POST", path="/mcp", scope_type="http"):
    scope = {
        "type": scope_type,
        "method": method,
        "path": path,
        "query_string": b"token=secret",
        "client": (peer, 1234),
        "headers": [(k.encode(), v.encode("latin-1")) for k, v in headers],
    }
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        raise AssertionError("body must not be read")

    asyncio.run(app(scope, receive, send))
    return scope, sent


@pytest.fixture
def records(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    return caplog


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


def test_xff_last_entry_used_behind_private_peer(records):
    hdrs = [("user-agent", "cli/1"), ("x-forwarded-for", "1.1.1.1, 203.0.113.9")]
    _call(ClientAccessLog(_app), hdrs)
    (line,) = _lines(records)
    assert line.startswith(
        'mcp_client peer=10.0.0.5 client=203.0.113.9 method=POST path="/mcp" status=202'
    )
    assert 'ua="cli/1"' in line and 'xff="1.1.1.1, 203.0.113.9"' in line


def test_public_peer_ignores_xff(records):
    _call(ClientAccessLog(_app), [("x-forwarded-for", "203.0.113.9")], peer="8.8.4.4")
    assert "client=8.8.4.4" in _lines(records)[0]


def test_malformed_xff_falls_back_to_peer():
    assert resolve_client("10.0.0.5", "1.1.1.1, not-an-ip") == "10.0.0.5"
    assert resolve_client("127.0.0.1", "") == "127.0.0.1"
    assert resolve_client("fd00::1", "2001:db8::1") == "2001:db8::1"


def test_health_checker_not_logged(records):
    _, sent = _call(
        ClientAccessLog(_app), [("user-agent", "ELB-HealthChecker/2.0")], method="GET"
    )
    assert _lines(records) == []
    assert sent[0]["status"] == 202


def test_health_checker_user_agent_on_post_is_still_logged(records):
    """A caller cannot drop its own requests by sending the health checker's name."""
    _call(ClientAccessLog(_app), [("user-agent", "ELB-HealthChecker/2.0")], method="POST")
    (line,) = _lines(records)
    assert "method=POST" in line


def test_health_checker_user_agent_from_public_peer_is_still_logged(records):
    _call(
        ClientAccessLog(_app),
        [("user-agent", "ELB-HealthChecker/2.0")],
        peer="8.8.4.4",
        method="GET",
    )
    assert len(_lines(records)) == 1


def test_path_is_quoted_so_it_cannot_forge_fields(records):
    _call(ClientAccessLog(_app), [], path="/x status=200 client=1.2.3.4")
    (line,) = _lines(records)
    assert 'path="/x status=200 client=1.2.3.4" status=202' in line


def test_trusted_proxies_env_limits_xff_trust(monkeypatch, records):
    monkeypatch.setenv("CRG_TRUSTED_PROXIES", "10.1.0.0/16")
    hdrs = [("x-forwarded-for", "203.0.113.9")]
    _call(ClientAccessLog(_app), hdrs, peer="10.1.2.3")
    _call(ClientAccessLog(_app), hdrs, peer="10.9.9.9")
    inside, outside = _lines(records)
    assert "peer=10.1.2.3 client=203.0.113.9" in inside
    assert "peer=10.9.9.9 client=10.9.9.9" in outside


def test_trusted_proxies_env_all_invalid_trusts_nobody(monkeypatch, records):
    monkeypatch.setenv("CRG_TRUSTED_PROXIES", "not-a-cidr")
    _call(ClientAccessLog(_app), [("x-forwarded-for", "203.0.113.9")], peer="10.1.2.3")
    assert "client=10.1.2.3" in _lines(records)[0]


def test_control_chars_stripped_and_truncated(records):
    ua = "evil\r\nINFO fake line\x00\x1b[31m" + "A" * 500
    _call(ClientAccessLog(_app), [("user-agent", ua), ("x-forwarded-for", "9" * 500)])
    (line,) = _lines(records)
    assert "\n" not in line and "\r" not in line and "\x00" not in line and "\x1b" not in line
    assert "A" * 201 not in line and "9" * 201 not in line


def test_sanitize_strips_quotes():
    assert sanitize('a"b\\c', 10) == "abc"


def test_secrets_never_logged(records):
    _call(
        ClientAccessLog(_app),
        [("authorization", "Bearer s3cret"), ("cookie", "sid=s3cret"), ("user-agent", "x")],
    )
    text = _lines(records)[0]
    assert "s3cret" not in text and "Bearer" not in text and "token=" not in text


@pytest.mark.parametrize("value", ["0", "false", "OFF"])
def test_env_disable(monkeypatch, records, value):
    monkeypatch.setenv("CRG_HTTP_CLIENT_LOG", value)
    assert build_client_log_middleware() == []
    _call(ClientAccessLog(_app), [])
    assert _lines(records) == []


def test_enabled_by_default(monkeypatch):
    monkeypatch.delenv("CRG_HTTP_CLIENT_LOG", raising=False)
    assert len(build_client_log_middleware()) == 1


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_pass_through(records, scope_type):
    scope, sent = _call(ClientAccessLog(_app), [], scope_type=scope_type)
    assert scope["touched"] and sent == [] and _lines(records) == []


def test_response_unchanged(records):
    _, sent = _call(ClientAccessLog(_app), [])
    assert sent == [
        {"type": "http.response.start", "status": 202, "headers": []},
        {"type": "http.response.body", "body": b"hello", "more_body": True},
        {"type": "http.response.body", "body": b"!"},
    ]


def test_app_exception_propagates_and_logs(records):
    async def boom(scope, receive, send):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        _call(ClientAccessLog(boom), [])
    assert "status=-" in _lines(records)[0]


# -- Health probes ---------------------------------------------------------------------------


async def _not_acceptable_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 406, "headers": []})
    await send({"type": "http.response.body", "body": b"Not Acceptable"})


def test_get_mcp_406_from_a_proxy_is_not_logged_at_info(records):
    _call(ClientAccessLog(_not_acceptable_app), [("user-agent", "probe/1")], method="GET")
    assert _lines(records) == []


def test_get_mcp_406_is_logged_at_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    _call(ClientAccessLog(_not_acceptable_app), [("user-agent", "probe/1")], method="GET")
    (record,) = [r for r in caplog.records if r.name == LOGGER]
    assert record.levelno == logging.DEBUG and "status=406" in record.getMessage()


def test_get_mcp_406_from_a_public_peer_is_still_logged(records):
    _call(ClientAccessLog(_not_acceptable_app), [], method="GET", peer="8.8.4.4")
    (line,) = _lines(records)
    assert "status=406" in line


def test_get_mcp_with_another_status_is_still_logged(records):
    _call(ClientAccessLog(_app), [("user-agent", "real/1")], method="GET")
    (line,) = _lines(records)
    assert "status=202" in line


def test_a_406_post_is_still_logged(records):
    _call(ClientAccessLog(_not_acceptable_app), [], method="POST")
    (line,) = _lines(records)
    assert "method=POST" in line and "status=406" in line


def test_health_get_is_quiet_even_from_any_peer(records):
    _call(ClientAccessLog(_app), [], method="GET", path="/health", peer="8.8.4.4")
    assert _lines(records) == []


def test_probes_are_counted_in_a_periodic_summary(monkeypatch, records):
    monkeypatch.setenv("CRG_PROBE_SUMMARY_SECONDS", "0")
    app = ClientAccessLog(_not_acceptable_app)
    for _ in range(3):
        _call(app, [], method="GET")
    lines = _lines(records)
    assert len(lines) == 3 and all(line.startswith("mcp_probe_summary") for line in lines)

    monkeypatch.setenv("CRG_PROBE_SUMMARY_SECONDS", "3600")
    app = ClientAccessLog(_not_acceptable_app)
    for _ in range(3):
        _call(app, [], method="GET")
    assert len(_lines(records)) == 3  # no new summary inside the interval


def test_real_post_line_format_is_unchanged(records):
    _call(ClientAccessLog(_app), [("user-agent", "claude/1")], method="POST")
    (line,) = _lines(records)
    assert line == (
        'mcp_client peer=10.0.0.5 client=10.0.0.5 method=POST path="/mcp" status=202 '
        'ua="claude/1" xff=""'
    )


def test_health_route_answers_200_without_graph_or_headers(records):
    from starlette.testclient import TestClient

    from code_review_graph import main

    app = main.mcp.http_app(middleware=build_client_log_middleware())
    with TestClient(app) as client:
        reply = client.get("/health")
        assert reply.status_code == 200
        assert reply.json() == {"status": "ok"}
    assert _lines(records) == []
