"""Every read tool accepts ``detail_level`` so a client can pass it uniformly."""

import asyncio

from code_review_graph.main import mcp
from tests.mcp_client_compat import wire


def _read_tools():
    tools = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tools if wire(t.annotations, "readOnlyHint") is True}


def test_every_read_tool_accepts_detail_level():
    tools = _read_tools()
    assert tools
    missing = sorted(
        name for name, t in tools.items()
        if "detail_level" not in (t.parameters.get("properties") or {})
    )
    assert missing == []


def test_detail_level_is_never_required():
    for name, t in _read_tools().items():
        assert "detail_level" not in (t.parameters.get("required") or []), name


def test_detail_level_is_last_parameter_of_previously_missing_tools():
    # Appended last so positional callers of these tools keep working.
    tools = _read_tools()
    names = ("get_flow_tool", "pr_blast_radius_tool", "traverse_graph_tool", "get_community_tool")
    for name in names:
        assert list(tools[name].parameters["properties"])[-1] == "detail_level", name
