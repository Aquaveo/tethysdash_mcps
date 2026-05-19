"""Tests for the `data_uri` opt-in (Plan 2026-05-18-002 Unit 5).

Three create_* tools (`create_plotly_chart`, `create_data_table`,
`create_card`) gained an optional `data_uri: str | list[str]` arg as
the receiving side of chatbox-core's MCP result-by-reference protocol.

After chatbox-core's substitution layer resolves a `data_uri` into an
inline `data` arg, the server sees the call as if the LLM had passed
`data` directly. These tests pin the defensive paths that fire when:

  1. The client is unmediated (sends `data_uri` directly with no
     resolution) — server returns an "unresolved" error envelope.
  2. The LLM passes BOTH `data` and `data_uri` despite chatbox-core's
     conflict-resolution — server enforces exactly-one-set as
     defense-in-depth.
  3. The LLM passes neither (`data is None` and `data_uri is None`) on
     tools that require data (chart, table) — server rejects with
     "neither set".
  4. The LLM passes a malformed `data_uri` string — Pydantic rejects
     at the schema layer before the tool body runs.

Backward compat regression tests verify that all existing inline-data
call shapes continue to work unchanged (no `data_uri` was passed).
"""

from __future__ import annotations

import json

import pytest

from fastmcp import Client
from fastmcp.client.transports.memory import FastMCPTransport

from tethysdash_mcp.mcp_server import mcp


pytestmark = pytest.mark.asyncio


@pytest.fixture
def client() -> Client:
    return Client(transport=FastMCPTransport(mcp))


def _structured(result) -> dict:
    if result.structured_content is not None:
        return result.structured_content
    text = "".join(
        block.text for block in result.content if hasattr(block, "text")
    )
    return json.loads(text)


# A valid-shaped cache URI for tests that need one. Server-side code
# never resolves these (chatbox-core's job), so the value just needs to
# match the Pydantic pattern.
SAMPLE_URI = "mcp+cache://conv-abc/Xk9P2qLmZjr"


# ---------------------------------------------------------------------------
# Backward compat — inline `data` calls work unchanged on all three tools
# ---------------------------------------------------------------------------


async def test_create_plotly_chart_inline_data_works_unchanged(client):
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": [{"x": [1, 2], "y": [3, 4], "type": "scatter"}]},
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload


async def test_create_data_table_inline_data_works_unchanged(client):
    async with client:
        result = await client.call_tool(
            "create_data_table",
            {"data": [{"col": 1}, {"col": 2}]},
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "table", payload


async def test_create_card_inline_data_works_unchanged(client):
    async with client:
        result = await client.call_tool(
            "create_card",
            {"title": "T", "data": [{"label": "Status", "value": "OK"}]},
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "card", payload


# ---------------------------------------------------------------------------
# Unmediated client — server rejects literal `data_uri` (cannot resolve)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name, extra_args",
    [
        ("create_plotly_chart", {}),
        ("create_data_table", {}),
        ("create_card", {"title": "T"}),
    ],
)
async def test_data_uri_arrives_unresolved_returns_envelope(
    client, tool_name, extra_args
):
    """An unmediated MCP client (no chatbox-core substitution layer)
    sends `data_uri` literally. The server cannot resolve client-side
    cache URIs and must return a typed error directing the caller to
    use inline `data` instead.
    """
    async with client:
        result = await client.call_tool(
            tool_name,
            {**extra_args, "data_uri": SAMPLE_URI},
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "unresolved" in payload["error"].lower(), payload
    # fix_hint should exist on the two big create_* tools that the plan
    # specifically targets (chart, table). create_card's envelope has the
    # same shape — pin the hint for all three.
    assert "fix_hint" in payload, payload
    assert "chatbox-core" in payload["fix_hint"], payload["fix_hint"]


# ---------------------------------------------------------------------------
# Conflict: both `data` and `data_uri` set → server rejects
# ---------------------------------------------------------------------------


async def test_create_plotly_chart_both_data_and_data_uri_rejected(client):
    """chatbox-core's substitution layer drops `data_uri` after resolving
    it, so the server should never see both set in the mediated path.
    Defense-in-depth: if both arrive, reject with a clear error.
    """
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": [{"x": [1], "y": [1]}],
                "data_uri": SAMPLE_URI,
            },
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "both" in payload["error"].lower(), payload


async def test_create_data_table_both_data_and_data_uri_rejected(client):
    async with client:
        result = await client.call_tool(
            "create_data_table",
            {
                "data": [{"col": 1}],
                "data_uri": SAMPLE_URI,
            },
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload


async def test_create_card_both_data_and_data_uri_rejected(client):
    async with client:
        result = await client.call_tool(
            "create_card",
            {
                "title": "T",
                "data": [{"value": "x"}],
                "data_uri": SAMPLE_URI,
            },
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "both" in payload["error"].lower(), payload


# ---------------------------------------------------------------------------
# Neither set → reject (for tools that require data)
# ---------------------------------------------------------------------------


async def test_create_plotly_chart_neither_data_nor_uri_rejected(client):
    async with client:
        result = await client.call_tool("create_plotly_chart", {})
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "neither" in payload["error"].lower(), payload


async def test_create_data_table_neither_data_nor_uri_rejected(client):
    async with client:
        result = await client.call_tool("create_data_table", {})
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "neither" in payload["error"].lower(), payload


# Note: create_card explicitly allows `data=None` (empty placeholder
# card), so the "neither set" rejection does NOT apply there. The card
# tool's existing test for None data continues to pass.


# ---------------------------------------------------------------------------
# Pydantic pattern enforcement on `data_uri` values
# ---------------------------------------------------------------------------


async def test_create_plotly_chart_rejects_non_cache_uri_scheme(client):
    """The Pydantic regex on `data_uri` only accepts `mcp+cache://` URIs.
    HTTPS or other schemes are rejected at the schema layer (the input-
    validation middleware fires before the tool body).
    """
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data_uri": "https://example.com/file.json"},
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload


async def test_create_plotly_chart_rejects_malformed_cache_uri(client):
    """Even with the right scheme, a malformed `mcp+cache://` URI (no
    token, invalid characters) is rejected by the Pydantic regex.
    """
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data_uri": "mcp+cache://conv/"},  # token missing
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
