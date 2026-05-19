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


# ---------------------------------------------------------------------------
# LLM-syntax-leak recovery (Plan 2026-05-18-002 Unit 5 follow-up)
# ---------------------------------------------------------------------------
#
# Observed 2026-05-18 across nemotron-3, qwen-3.5, deepseek-pro-4: all
# three models emit the literal string "None" (or "null") for
# Optional[Dict] args on create_plotly_chart, triggering Pydantic
# `dict_type` errors that produce "argument validation failed" envelopes
# the LLM can't easily recover from. The server now accepts
# Union[Dict, str] on `layout` and `config`, coerces "None"/"null"/""
# strings to actual None, and JSON-decodes anything else string-shaped.


@pytest.mark.parametrize("none_str", ["None", "null", "", "NONE", "  none  "])
async def test_create_plotly_chart_coerces_layout_none_string(client, none_str):
    """`layout='None'` (and variants) coerced to actual None, not rejected."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": [{"x": [1], "y": [1]}], "layout": none_str},
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload


@pytest.mark.parametrize("none_str", ["None", "null", "", "NULL"])
async def test_create_plotly_chart_coerces_config_none_string(client, none_str):
    """Same coercion path for `config`."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": [{"x": [1], "y": [1]}], "config": none_str},
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload


async def test_create_plotly_chart_accepts_layout_as_json_string(client):
    """`layout` may be passed as a JSON-string of a dict (matches the
    existing `data: Union[List, str]` pattern). Server decodes it."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": [{"x": [1], "y": [1]}],
                "layout": '{"title": "Time Series", "xaxis": {"title": "t"}}',
            },
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload


async def test_create_plotly_chart_rejects_malformed_layout_json_string(client):
    """If `layout` is a string but not valid JSON, return a typed envelope
    (not a Pydantic-level error — the schema accepts the string)."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": [{"x": [1], "y": [1]}], "layout": '{"unterminated":'},
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "`layout` is not valid JSON" in payload["error"], payload


async def test_create_plotly_chart_reproduces_production_failure_mode(client):
    """The exact failure observed 2026-05-18 across nemotron-3-super,
    qwen-3.5-397b, and deepseek-pro-4 — `layout: 'None'` and
    `config: 'None'` as Python-syntax string leaks. Server now recovers."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": [{"x": [1, 2, 3], "y": [4, 5, 6], "type": "scatter"}],
                "h": 40,
                "layout": "None",
                "config": "None",
                "title": "Flow over Time",
                "w": 50,
            },
        )
    payload = _structured(result)
    # Smoke-reproducing call succeeds (vs the production "argument validation
    # failed / other=2" rejection) because the coercion now converts
    # `layout: 'None'` and `config: 'None'` to actual None before they reach
    # the tool body's defaulting logic.
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload
    # Config defaulted (no input dict was supplied).
    assert payload["visualization"]["inlineData"]["config"] == {
        "displaylogo": False,
        "responsive": True,
    }, payload


# ---------------------------------------------------------------------------
# Records mode + envelope unwrap (Plan 2026-05-18-002 Unit 6 follow-up)
# ---------------------------------------------------------------------------
#
# The cache+URI protocol stores the FULL upstream tool envelope (e.g., the
# nrds query result {ok, rows, columns, data:[records], ...}) and the
# substitution layer writes that envelope verbatim into `data`. The envelope
# is dict-shaped, which fails create_plotly_chart's Union[List, str] check
# with two Pydantic errors. Server-side defense unwraps dict envelopes
# (`.data`, `.rows`, `.records`) before validation. Records mode then accepts
# the unwrapped records + pivot hints and constructs traces server-side,
# removing the LLM-as-ETL transformation step entirely.


async def test_create_plotly_chart_unwraps_dict_envelope_with_data_key(client):
    """A dict envelope with a `data` list is unwrapped before validation.
    Mirrors the cache-URI path where the LLM's `data_uri` resolves to the
    whole cached payload."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": {
                    "ok": True,
                    "rows": 1,
                    "columns": ["x", "y"],
                    "data": [{"x": [1, 2], "y": [3, 4], "type": "scatter"}],
                },
            },
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload


async def test_create_plotly_chart_unwraps_dict_envelope_with_rows_key(client):
    """Alternate envelope shape: `rows` key carries the list."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": {
                    "rows": [{"x": [1, 2], "y": [3, 4], "type": "scatter"}],
                },
            },
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload


async def test_create_plotly_chart_unwraps_dict_envelope_with_records_key(client):
    """Alternate envelope shape: `records` key carries the list."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": {
                    "records": [{"x": [1, 2], "y": [3, 4], "type": "scatter"}],
                },
            },
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload


async def test_create_plotly_chart_dict_envelope_without_list_field_rejected(client):
    """A dict with no list-valued data/rows/records key cannot be unwrapped —
    Pydantic rejects with the standard Union mismatch error."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": {"ok": True, "rows": 240}},  # no list values
        )
    payload = _structured(result)
    assert "error" in payload, payload


async def test_create_plotly_chart_records_mode_single_trace(client):
    """Records (non-Plotly-shaped dicts) + x_field + y_field pivots
    server-side into a single trace."""
    records = [
        {"feature_id": 42, "time": "2026-01-01", "flow": 10.5},
        {"feature_id": 42, "time": "2026-01-02", "flow": 11.0},
        {"feature_id": 42, "time": "2026-01-03", "flow": 12.3},
    ]
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": records,
                "x_field": "time",
                "y_field": "flow",
            },
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload
    traces = payload["visualization"]["inlineData"]["data"]
    assert len(traces) == 1, traces
    assert traces[0]["x"] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert traces[0]["y"] == [10.5, 11.0, 12.3]


async def test_create_plotly_chart_records_mode_with_series_field(client):
    """series_field groups records into one trace per series, preserving
    insertion order."""
    records = [
        {"feature_id": 1, "time": "2026-01-01", "flow": 10.5},
        {"feature_id": 1, "time": "2026-01-02", "flow": 11.0},
        {"feature_id": 2, "time": "2026-01-01", "flow": 20.5},
        {"feature_id": 2, "time": "2026-01-02", "flow": 21.0},
    ]
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": records,
                "x_field": "time",
                "y_field": "flow",
                "series_field": "feature_id",
            },
        )
    payload = _structured(result)
    traces = payload["visualization"]["inlineData"]["data"]
    assert len(traces) == 2, traces
    assert traces[0]["name"] == "1"
    assert traces[1]["name"] == "2"
    assert traces[0]["y"] == [10.5, 11.0]
    assert traces[1]["y"] == [20.5, 21.0]


async def test_create_plotly_chart_records_without_pivot_fields_rejected(client):
    """Records-shaped data (no `x`/`y`/`type`) without x_field/y_field —
    typed error envelope naming the missing args."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": [{"feature_id": 1, "time": "2026-01-01", "flow": 10.5}]},
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "x_field" in payload["error"], payload


async def test_create_plotly_chart_records_with_missing_x_field_rejected(client):
    """x_field names a column not present in records — typed error."""
    records = [{"time": "2026-01-01", "flow": 10.5}]
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": records, "x_field": "nonexistent", "y_field": "flow"},
        )
    payload = _structured(result)
    assert payload.get("error", "").startswith("invalid_args:"), payload
    assert "nonexistent" in payload["error"], payload


async def test_create_plotly_chart_traces_still_pass_through_unchanged(client):
    """Backward compat: existing Plotly-trace shape (dicts with `x`/`y`/`type`)
    bypasses records-mode detection and passes through unchanged."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {"data": [{"x": [1, 2], "y": [3, 4], "type": "scatter"}]},
        )
    payload = _structured(result)
    traces = payload["visualization"]["inlineData"]["data"]
    assert traces[0]["type"] == "scatter"
    assert traces[0]["x"] == [1, 2]


async def test_create_plotly_chart_envelope_with_records_unwraps_and_pivots(client):
    """End-to-end cache-URI scenario: dict envelope wraps records (no x/y
    keys). Server unwraps the envelope, detects records, pivots into
    traces. This is exactly what the URI substitution layer produces when
    the cached payload is an nrds query result."""
    async with client:
        result = await client.call_tool(
            "create_plotly_chart",
            {
                "data": {
                    "ok": True,
                    "rows": 3,
                    "columns": ["time", "flow"],
                    "data": [
                        {"time": "2026-01-01", "flow": 10.5},
                        {"time": "2026-01-02", "flow": 11.0},
                        {"time": "2026-01-03", "flow": 12.3},
                    ],
                    "query": "SELECT time, flow FROM output",
                },
                "x_field": "time",
                "y_field": "flow",
            },
        )
    payload = _structured(result)
    assert payload.get("visualization", {}).get("vizType") == "plotly", payload
    traces = payload["visualization"]["inlineData"]["data"]
    assert len(traces) == 1
    assert traces[0]["x"] == ["2026-01-01", "2026-01-02", "2026-01-03"]
    assert traces[0]["y"] == [10.5, 11.0, 12.3]
