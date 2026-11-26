"""Tests for the HTTP client access-log middleware."""

from __future__ import annotations

import asyncio
import logging

import pytest

from code_review_graph.http_access_log import (
    ClientAccessLog,
    _quiet_uvicorn_probe_lines,
    _UvicornProbeFilter,
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


# --- uvicorn's own access line for probes -------------------------------------------------


def _uvicorn_record(client, method, path, status, version="1.1"):
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, "", 0,
        '%s - "%s %s HTTP/%s" %d', (client, method, path, version, status), None,
    )


@pytest.fixture
def uvicorn_access():
    """uvicorn's access logger with no probe filter, restored afterwards.

    Constructing ``ClientAccessLog`` attaches the filter globally, so earlier tests in this
    module may have left one behind.
    """
    access = logging.getLogger("uvicorn.access")
    before = list(access.filters)
    for f in before:
        if isinstance(f, _UvicornProbeFilter):
            access.removeFilter(f)
    yield access
    for f in list(access.filters):
        access.removeFilter(f)
    for f in before:
        access.addFilter(f)


def test_uvicorn_get_mcp_406_from_a_proxy_is_dropped():
    flt = _UvicornProbeFilter()
    assert flt.filter(_uvicorn_record("10.215.45.183:34142", "GET", "/mcp", 406)) is False
    assert flt.filter(_uvicorn_record("[::1]:5000", "GET", "/mcp/", 406)) is False


def test_uvicorn_get_health_is_dropped_whatever_the_peer_or_status():
    flt = _UvicornProbeFilter()
    assert flt.filter(_uvicorn_record("8.8.4.4:4000", "GET", "/health", 200)) is False
    assert flt.filter(_uvicorn_record("10.0.0.5:4000", "GET", "/health?x=1", 503)) is False


def test_uvicorn_lines_that_are_not_probes_are_kept():
    flt = _UvicornProbeFilter()
    # A public peer's 406 is a real client mistake, not a load balancer probe.
    assert flt.filter(_uvicorn_record("8.8.4.4:4000", "GET", "/mcp", 406)) is True
    # Real traffic, including a POST that was answered 406, stays visible.
    assert flt.filter(_uvicorn_record("10.0.0.5:4000", "POST", "/mcp", 200)) is True
    assert flt.filter(_uvicorn_record("10.0.0.5:4000", "POST", "/mcp", 406)) is True
    assert flt.filter(_uvicorn_record("10.0.0.5:4000", "GET", "/mcp", 200)) is True
    assert flt.filter(_uvicorn_record("10.0.0.5:4000", "GET", "/other", 406)) is True


def test_uvicorn_record_of_an_unexpected_shape_is_kept():
    flt = _UvicornProbeFilter()
    odd = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "plain message", None, None)
    assert flt.filter(odd) is True
    short = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s %s", ("a", "b"), None)
    assert flt.filter(short) is True


def test_uvicorn_filter_follows_the_trusted_proxy_setting(monkeypatch):
    monkeypatch.setenv("CRG_TRUSTED_PROXIES", "192.0.2.0/24")
    flt = _UvicornProbeFilter()
    assert flt.filter(_uvicorn_record("192.0.2.7:1", "GET", "/mcp", 406)) is False
    # Private, but not in the configured list: not a trusted proxy, so it is kept.
    assert flt.filter(_uvicorn_record("10.0.0.5:1", "GET", "/mcp", 406)) is True


def test_the_filter_is_attached_once(uvicorn_access):
    _quiet_uvicorn_probe_lines()
    _quiet_uvicorn_probe_lines()
    ours = [f for f in uvicorn_access.filters if isinstance(f, _UvicornProbeFilter)]
    assert len(ours) == 1


def test_building_the_middleware_attaches_the_filter(uvicorn_access, monkeypatch):
    monkeypatch.delenv("CRG_HTTP_CLIENT_LOG", raising=False)
    build_client_log_middleware()
    assert any(isinstance(f, _UvicornProbeFilter) for f in uvicorn_access.filters)


def test_a_disabled_client_log_leaves_uvicorn_alone(uvicorn_access, monkeypatch):
    monkeypatch.setenv("CRG_HTTP_CLIENT_LOG", "0")
    assert build_client_log_middleware() == []
    assert not any(isinstance(f, _UvicornProbeFilter) for f in uvicorn_access.filters)


def test_a_real_uvicorn_access_record_is_filtered_end_to_end(uvicorn_access, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.access")
    _quiet_uvicorn_probe_lines()
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "10.215.1.1:9", "GET", "/mcp", "1.1", 406)
    access.info('%s - "%s %s HTTP/%s" %d', "10.215.1.1:9", "POST", "/mcp", "1.1", 200)
    lines = [r.getMessage() for r in caplog.records if r.name == "uvicorn.access"]
    assert lines == ['10.215.1.1:9 - "POST /mcp HTTP/1.1" 200']
