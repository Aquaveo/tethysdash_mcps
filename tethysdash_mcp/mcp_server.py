"""
TethysDash MCP Server

Exposes tools for creating native TethysDash visualizations (Plotly charts,
tables, maps, cards, text) and rendering registered MFE components.

The LLM calls these tools to create dashboard grid items with inline data -
no backend API call needed. The chatbox dispatches the returned specs as
DOM events that DashboardLayout.js handles.

Usage:
    python -m tethysdash_mcp.mcp_server

Connects to chatbox via MCP Streamable HTTP transport on port 9001 by
default (override with ``MCP_PORT``) (path: ``/mcp``). Set ``MCP_TRANSPORT=sse`` for the legacy SSE
compat path during the migration window. Default host binding is
loopback (``127.0.0.1``); override with ``MCP_HOST`` for deployments
behind an authenticated reverse proxy. CORS is env-driven via
``ALLOWED_ORIGINS`` (default wildcard).
"""

import logging
import math
import os
import json
import re
import uuid
from typing import Optional, Dict, Any, List, Literal, Union
from typing_extensions import Annotated
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.functional_validators import BeforeValidator
from fastmcp import FastMCP

from ._uri_field import uri_field, ensure_exactly_one_set
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse
from starlette.responses import Response as StarletteResponse
import requests as http_requests

from tethysdash_mcp.plugin_helpers import (
    LAYER_PROPERTIES_ALLOWLIST,
    LayerConfigurationBuilder,
    get_allowed_source_prop_keys,
)
from tethysdash_mcp.editable_schemas import (
    LLM_EDITABLE_PATHS,
    is_path_allowed,
)
from tethysdash_mcp.editable_schemas_plugin import (
    is_path_allowed_plugin,
    resolve_editable_paths,
)
from tethysdash_mcp._input_validation_middleware import (
    InputValidationEnvelopeMiddleware,
)
from tethysdash_mcp._observability_middleware import (
    ToolCallObservabilityMiddleware,
)

mcp = FastMCP(
    "TethysDash MCP Server",
    # 2026-05-10 Phase 3c probe: removed BM25SearchTransform entirely.
    # The chatbox-core engine (engine/index.js:94-129 selectToolsForPrompt
    # + engine/embeddings.js) already runs per-prompt semantic-similarity
    # ranking using @huggingface/transformers on any server it classifies
    # as full-catalog with >= SMALL_CATALOG_THRESHOLD (8) tools. Tethysdash
    # has 26 tools, well above that threshold, so the embedding ranker is
    # the authoritative selection layer regardless of what BM25SearchTransform
    # would have done on the server side.
    #
    # Trading server-side BM25 filtering for client-side embedding ranking:
    #   + Slash prompts route reliably without manual `always_visible`
    #     curation (Phase 3a → 3b → 3c pattern stays trivial; no per-tool
    #     pinning decisions needed)
    #   + Per-prompt context-aware tool surfacing — "Add a WMS layer..."
    #     ranks add_wms_layer highly; "Create a card tile..." ranks
    #     create_card highly
    #   + The contract test
    #     `test_prompt_target_tool_is_visible_in_default_list_tools`
    #     trivially passes since every tool is visible at the server
    #   - Compound prompts like "create a map with a WMS layer" can let the
    #     11 add_*_layer tools dominate the embedding score, crowding out
    #     create_map_visualization. If that regression appears, fall back
    #     to re-introducing BM25SearchTransform with a curated
    #     `always_visible` set or pinning 3-4 most-common layer types only.
    middleware=[
        # Order: observability is OUTERMOST so it observes the final
        # envelope produced by InputValidationEnvelopeMiddleware
        # (validation errors come back as structured tool results, not
        # exceptions, and the observability log line shows status=invalid_args).
        ToolCallObservabilityMiddleware(),
        InputValidationEnvelopeMiddleware(),
    ],
)
LOGGER = logging.getLogger("tethysdash.mcp")
TETHYSDASH_BASE_URL = os.getenv("TETHYSDASH_BASE_URL", "")


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: StarletteRequest) -> JSONResponse:
    """Liveness probe used by Docker HEALTHCHECK and container orchestrators.

    Returns 200 with a minimal payload. Does not exercise downstream
    dependencies (the TethysDash backend HTTP call) -- keep it cheap so
    polling stays free.
    """
    return JSONResponse({"status": "ok"})


def _backend_not_configured_envelope() -> Dict[str, Any]:
    """Structured response returned when TETHYSDASH_BASE_URL is unset.

    The standalone package ships with the URL blank so an unconfigured
    deployment does not silently mis-target ``localhost:8080``. Tools
    that proxy to the TethysDash Django API surface this envelope so the
    LLM gets an actionable signal instead of a connection error.
    """
    return {
        "error": "backend_not_configured",
        "message": (
            "TETHYSDASH_BASE_URL is unset. Set it to the TethysDash "
            "Django app URL (e.g., https://tethys.example.com/apps/tethysdash) "
            "before invoking tools that proxy to the backend."
        ),
    }


def _load_runtime_plugin_registry_http() -> List[Dict[str, Any]]:
    """Read the runtime plugin registry from tethysdash over HTTP.

    Replaces the filesystem-coupled reader from the prior architecture.
    The tethysdash side exposes the registry at
    ``${TETHYSDASH_BASE_URL}/runtime-plugins/list/`` -- a sibling of the
    auth-gated ``runtime-plugins/sync/`` endpoint that powers the
    browser-side registration UI.

    Returns an empty list on any failure (BASE_URL unset, network
    unreachable, non-200, non-JSON body, non-list payload) to match the
    legacy loader's silent-recovery posture: registry-read failures must
    never crash an unrelated tool call. Failures are logged at warning
    level so operators have a breadcrumb when registered plugins go
    missing from the LLM-visible tool list.

    ``allow_redirects=False`` defends against a tethysdash misconfiguration
    that redirects unauthenticated requests to a login page (the response
    would parse as 200 OK + HTML body and look like an empty registry).
    """
    if not TETHYSDASH_BASE_URL:
        return []
    try:
        response = http_requests.get(
            f"{TETHYSDASH_BASE_URL}/runtime-plugins/list/",
            timeout=5,
            allow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        LOGGER.warning(
            "Failed to fetch runtime plugin registry from %s: %s",
            TETHYSDASH_BASE_URL,
            exc,
        )
        return []
    if not isinstance(data, list):
        LOGGER.warning(
            "Runtime plugin registry endpoint returned non-list payload "
            "(type=%s); treating as empty.",
            type(data).__name__,
        )
        return []
    return data


def _parse_allowed_origins() -> List[str]:
    """Read ALLOWED_ORIGINS from env (comma-separated). Defaults to wildcard.

    Mirrors mcp/nrds_mcps/nextgen_mcp/mcp_server.py::_parse_allowed_origins
    exactly — drift between sibling MCP servers' CORS handling is a
    maintenance hazard. Production deployments behind a known origin
    should set this explicitly via ALLOWED_ORIGINS=https://example.com[,...].
    Empty / malformed values fall back to the wildcard (no silent lockdown).
    """
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if not raw or raw == "*":
        return ["*"]
    parsed = [o.strip() for o in raw.split(",") if o.strip()]
    return parsed or ["*"]


ALLOWED_ORIGINS = _parse_allowed_origins()
# CORS spec forbids `allow_credentials=True` together with `allow_origins=["*"]`.
# Auto-derive so a misconfigured deploy can't produce the spec violation.
ALLOW_CREDENTIALS = ALLOWED_ORIGINS != ["*"]


def _patch_sse_transport_for_cors():
    """Monkey-patch SseServerTransport.handle_post_message to handle OPTIONS.

    MCP SDK v1.26+ validates Content-Type on all requests routed to
    handle_post_message, including CORS preflight OPTIONS (which have no
    Content-Type). This patch intercepts OPTIONS and returns 200 with
    CORS headers before the SDK's validation runs.

    Origin handling mirrors mcp/nrds_mcps's corrected version: when
    ``ALLOWED_ORIGINS != ["*"]``, only origins in the allowlist receive
    CORS approval, and ``access-control-allow-credentials`` is gated on
    a successful origin match. Without this gating, the patch is a
    reflected-origin CORS vulnerability (any origin can issue authenticated
    cross-site requests).

    Invoked from `__main__` only when `MCP_TRANSPORT=sse`; the streamable-
    http path uses the regular Starlette `CORSMiddleware` in `CORS_MIDDLEWARE`
    and never reaches this code.
    """
    from mcp.server.sse import SseServerTransport

    original_handle = SseServerTransport.handle_post_message

    async def patched_handle(self, scope, receive, send):
        if scope.get("method") == "OPTIONS":
            origin = dict(scope.get("headers", [])).get(b"origin", b"").decode()
            if ALLOWED_ORIGINS == ["*"]:
                allow_origin = "*"
            else:
                allow_origin = origin if origin in ALLOWED_ORIGINS else ""
            headers = {
                "access-control-allow-methods": "GET, POST, OPTIONS",
                "access-control-allow-headers": "content-type, x-csrftoken, authorization",
                "access-control-max-age": "86400",
            }
            if allow_origin:
                headers["access-control-allow-origin"] = allow_origin
            if ALLOW_CREDENTIALS and allow_origin and allow_origin != "*":
                headers["access-control-allow-credentials"] = "true"
            response = StarletteResponse(status_code=200, headers=headers)
            await response(scope, receive, send)
            return
        await original_handle(self, scope, receive, send)

    SseServerTransport.handle_post_message = patched_handle


CORS_MIDDLEWARE = [
    Middleware(
        CORSMiddleware,
        allow_origins=ALLOWED_ORIGINS,
        allow_credentials=ALLOW_CREDENTIALS,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id", "Last-Event-Id", "mcp-protocol-version"],
    ),
]

# ---------------------------------------------------------------------------
# Runtime plugin registry (read via HTTP from tethysdash; see
# _load_runtime_plugin_registry_http above for the rationale).
# ---------------------------------------------------------------------------


def _get_all_plugins() -> List[Dict[str, Any]]:
    """Return the runtime plugin registry.

    Not cached at the module level: the browser-side chatbox runtime-plugin
    registration UI writes to tethysdash's registry at runtime, and stale
    snapshots would shadow newly-registered plugins from
    ``list_available_visualizations`` / ``render_custom_visualization``.
    """
    plugins = _load_runtime_plugin_registry_http()
    LOGGER.info("Runtime plugin registry: %d plugin(s)", len(plugins))
    return plugins


def _convert_arg_to_schema(arg_name: str, arg_spec) -> Dict[str, Any]:
    """Convert a tethysdash plugin arg type to a structured schema."""
    if isinstance(arg_spec, list):
        return {"type": "string", "enum": arg_spec}
    if arg_spec == "text":
        return {"type": "string"}
    if arg_spec == "number":
        return {"type": "number"}
    if arg_spec == "checkbox":
        return {"type": "boolean", "default": False}
    if arg_spec == "object":
        return {"type": "object"}
    if arg_spec == "array":
        return {"type": "array"}
    if isinstance(arg_spec, dict):
        return arg_spec  # pass through rich declarations as-is
    return {"type": "string", "description": f"Input type: {arg_spec}"}


def _convert_plugin_args_to_schema(args: Dict) -> Dict[str, Any]:
    """Convert all args for a plugin to structured schemas."""
    return {name: _convert_arg_to_schema(name, spec) for name, spec in args.items()}


# ---------------------------------------------------------------------------
# Shared helpers for visualization tools (referenced from Pydantic
# `BeforeValidator` annotations on the @mcp.tool decorators below, so must
# be defined before the first decorator that uses them).
# ---------------------------------------------------------------------------


# Envelope keys checked when unwrapping dict-shaped `data` arguments.
# Order matters: nrds-style results use "data", some toolchains use "rows"
# or "records". First list-valued match wins.
_ENVELOPE_LIST_KEYS = ("data", "rows", "records")


def _unwrap_data_envelope(value: Any) -> Any:
    """Pre-validator: when the cache+URI substitution layer (chatbox-core's
    Unit 3) resolves a `data_uri` it writes the FULL cached envelope into
    the `data` slot — a dict like ``{ok, rows, columns, data:[records], ...}``.
    That dict fails create_plotly_chart's ``Union[List, str]`` check with two
    Pydantic errors. This pre-validator unwraps the envelope by extracting
    the first list-valued ``data`` / ``rows`` / ``records`` key, so by the
    time the Union check runs the value is the inner list.

    Pass-through for non-dict inputs. Dicts with no list-valued envelope
    key fall through unchanged — Pydantic will reject them with the normal
    Union mismatch error.
    """
    if isinstance(value, dict):
        for key in _ENVELOPE_LIST_KEYS:
            inner = value.get(key)
            if isinstance(inner, list):
                return inner
    return value


def _looks_like_records(data: Any) -> bool:
    """Records mode is detected when ``data`` is a non-empty list of dicts
    AND the first dict has none of Plotly's trace marker keys (``x``,
    ``y``, ``type``). A list of trace-shaped dicts is left alone.
    """
    if not isinstance(data, list) or not data:
        return False
    first = data[0]
    if not isinstance(first, dict):
        return False
    return not any(k in first for k in ("x", "y", "type"))


def _records_to_traces(
    records: List[Dict[str, Any]],
    x_field: str,
    y_field: str,
    series_field: Optional[str] = None,
) -> Union[List[Dict[str, Any]], Dict[str, Any]]:
    """Pivot a list of records into Plotly trace dicts. Returns a list of
    traces on success, or a single-key ``{"error": ...}`` envelope on
    failure (caller propagates the error).

    Without ``series_field``, emits a single trace whose ``x`` / ``y``
    arrays are the column values of every record in input order.

    With ``series_field``, groups records by that column's value and emits
    one trace per group (preserving group first-seen order). Group values
    are stringified for the trace ``name``.
    """
    if not records:
        return {"error": "invalid_args: cannot pivot empty records list."}
    sample = records[0]
    if not isinstance(sample, dict):
        return {
            "error": (
                "invalid_args: records-mode requires a list of dicts; "
                f"got list of {type(sample).__name__}."
            )
        }
    if x_field not in sample:
        return {
            "error": (
                f"invalid_args: x_field `{x_field}` not found in records. "
                f"Available fields: {sorted(sample.keys())}."
            )
        }
    if y_field not in sample:
        return {
            "error": (
                f"invalid_args: y_field `{y_field}` not found in records. "
                f"Available fields: {sorted(sample.keys())}."
            )
        }
    if series_field is not None and series_field not in sample:
        return {
            "error": (
                f"invalid_args: series_field `{series_field}` not found in "
                f"records. Available fields: {sorted(sample.keys())}."
            )
        }

    if series_field is None:
        return [
            {
                "x": [r.get(x_field) for r in records],
                "y": [r.get(y_field) for r in records],
                "type": "scatter",
                "mode": "lines",
                "name": y_field,
            }
        ]

    # Group by series_field, preserving first-seen order so traces appear
    # in the same order as the input (deterministic for tests + UX).
    groups: Dict[Any, Dict[str, List[Any]]] = {}
    for r in records:
        key = r.get(series_field)
        if key not in groups:
            groups[key] = {"x": [], "y": []}
        groups[key]["x"].append(r.get(x_field))
        groups[key]["y"].append(r.get(y_field))

    return [
        {
            "x": g["x"],
            "y": g["y"],
            "type": "scatter",
            "mode": "lines",
            "name": str(key),
        }
        for key, g in groups.items()
    ]


# ---------------------------------------------------------------------------
# Built-in visualization tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="create_plotly_chart",
    description=(
        "Create a NEW interactive Plotly chart tile on the dashboard. "
        "Use this after fetching rows or series from a data-source tool — "
        "pass them in via `data` to render a chart tile. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing chart — use `patch_visualization` instead "
        "(e.g., add a trace by patching `/args/inlineData/data/-`). "
        "Only call this for a NEW chart that does not yet exist on the "
        "dashboard."
    ),
    tags=["visualization", "chart"],
)
def create_plotly_chart(
    data: Annotated[
        Optional[Union[List[Dict[str, Any]], str]],
        BeforeValidator(_unwrap_data_envelope),
        Field(
            default=None,
            description=(
                "MUST use `data_uri` when any recent tool result contained "
                "`_cache_uri` — pass that value as `data_uri` and do NOT "
                "inline `data`. Inline `data` is RESERVED for trace "
                "objects you constructed directly with no upstream query. "
                "Re-emitting upstream rows inline wastes output tokens; "
                "the engine resolves URIs server-side without re-tokenizing "
                "the bytes. Inline `data` over ~20 records additionally "
                "triggers JSON parse errors on small models (chatbox-core "
                "rejects locally with an actionable error pointing at "
                "`data_uri`).\n\n"
                "Inline shape (only when no upstream `_cache_uri` exists): "
                "array of Plotly trace objects, OR a list of data records "
                "when `x_field` and `y_field` are provided (server pivots "
                "records into traces). Trace objects MUST have non-empty "
                "`x` and `y` arrays; optional `type` (default 'scatter'), "
                "`name`, `mode`. MUST contain at least one element. Server "
                "unwraps dict envelopes carrying a `data` / `rows` / "
                "`records` list, so the cache-URI substitution path passes "
                "through cleanly."
            ),
            min_length=1,
        ),
    ] = None,
    data_uri: Annotated[
        Optional[Union[str, List[str]]],
        uri_field(inline_arg_name="data"),
    ] = None,
    x_field: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Records-mode pivot: name of the column in `data` records "
                "whose values populate the trace x-axis. Required when "
                "`data` is records-shaped (a list of dicts without Plotly "
                "trace keys like `x` / `y` / `type`). Ignored when `data` "
                "is already a list of trace objects."
            ),
        ),
    ] = None,
    y_field: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Records-mode pivot: name of the column in `data` records "
                "whose values populate the trace y-axis. Required when "
                "`data` is records-shaped. Ignored when `data` is already "
                "a list of trace objects."
            ),
        ),
    ] = None,
    series_field: Annotated[
        Optional[str],
        Field(
            default=None,
            description=(
                "Records-mode pivot: optional name of the column to group "
                "records by, producing one trace per group. Omit for a "
                "single trace across all records."
            ),
        ),
    ] = None,
    layout: Annotated[
        Optional[Union[Dict[str, Any], str]],
        Field(
            default=None,
            description=(
                "Plotly layout object with title, axis labels, etc. "
                "Pass a JSON object or omit. Several LLM models emit the "
                "literal string 'None' / 'null' here; the server coerces "
                "those to actual null."
            ),
        ),
    ] = None,
    config: Annotated[
        Optional[Union[Dict[str, Any], str]],
        Field(
            default=None,
            description=(
                "Plotly config object (responsive, displaylogo, etc.). "
                "Pass a JSON object or omit. String 'None' / 'null' is "
                "coerced to null."
            ),
        ),
    ] = None,
    title: Annotated[Optional[str], Field(description="Chart title (shorthand - added to layout.title)")] = None,
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Examples: 25=quarter-width, 50=half-width (DEFAULT — "
                "recommended for time-series charts), 100=full-width. "
                "Lower than 25 produces an unreadably narrow chart."
            ),
            ge=1,
            le=100,
        ),
    ] = 50,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units (1 unit ≈ 5-10px depending on "
                "viewport). Recommended range: 30-60 for time-series charts. "
                "DEFAULT 40 ≈ 200-400px tall, suitable for 24-48 data points. "
                "Do NOT pass values under 10 — chart will be unreadably "
                "squished."
            ),
            ge=10,
            le=100,
        ),
    ] = 40,
) -> Dict[str, Any]:
    """Create a Plotly chart visualization on the dashboard.

    Returns a visualization spec that the chatbox dispatches as a grid item.
    The chart renders using TethysDash's native BasePlot component.
    """
    # LLM-syntax-leak recovery: some models emit the literal string
    # "None" / "null" for Optional[Dict] args. Accept as Union[Dict, str]
    # then coerce back to actual None here.
    layout = _coerce_none_string(layout)
    config = _coerce_none_string(config)
    # JSON-string acceptance for layout/config (matches the existing
    # `data: Union[List, str]` pattern). Some LLMs emit nested dicts as
    # JSON strings to avoid in-output structure complexity.
    if isinstance(layout, str):
        try:
            layout = json.loads(layout)
        except json.JSONDecodeError as e:
            return {"error": f"invalid_args: `layout` is not valid JSON: {e}"}
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except json.JSONDecodeError as e:
            return {"error": f"invalid_args: `config` is not valid JSON: {e}"}

    # Validate the exactly-one-of contract between `data` (inline) and
    # `data_uri` (cache URI). In the mediated path, chatbox-core resolves
    # `data_uri` into `data` BEFORE dispatch and drops the `data_uri` arg,
    # so this validator should see exactly one of the two set.
    # Defense-in-depth for unmediated clients that don't run chatbox-core's
    # substitution layer.
    err = ensure_exactly_one_set(data, "data", data_uri, "data_uri")
    if err:
        return {"error": err}
    if data_uri is not None:
        # Unmediated client — server cannot resolve cache URIs (they're
        # client-side IndexedDB keys, not server-resolvable handles).
        return {
            "error": (
                "invalid_args: `data_uri` arrived unresolved at the server. "
                "Cache URIs are resolved by chatbox-core's substitution "
                "layer before tool dispatch — non-chatbox-core MCP clients "
                "must pass inline `data` instead."
            ),
            "fix_hint": (
                "If you're using chatbox-core, ensure `enableResultCache={true}` "
                "is set on the <Chatbox> mount. Other MCP clients must retry "
                "with `data` populated inline as the structured array."
            ),
        }

    # Dict-coercion pattern (see docs/solutions/best-practices/mcp-tool-dict-parameter-coercion)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            return {"error": f"invalid_args: `data` is not valid JSON: {e}"}

    # Server-side defense: Pydantic min_length=1 catches `data=[]` at the
    # input level, but `data="[]"` (JSON string of empty array) passes
    # because min_length applies to the str length, not the post-decode
    # list length. After json.loads above, re-check.
    if not isinstance(data, list) or len(data) == 0:
        return {
            "error": (
                "invalid_args: `data` must be a non-empty list of Plotly "
                "trace objects. Fetch values from a data-source tool first; "
                "do NOT call create_plotly_chart with empty data when a "
                "data fetch failed."
            )
        }

    # Records-mode pivot. When `data` is a list of dicts that lack Plotly
    # trace markers (`x` / `y` / `type`), treat it as raw records and pivot
    # using `x_field` / `y_field` / `series_field`. This removes the
    # LLM-as-ETL transformation step — the LLM names the columns to plot,
    # the server constructs the traces. Plotly-trace-shaped inputs bypass
    # the pivot entirely.
    if _looks_like_records(data):
        if not x_field or not y_field:
            return {
                "error": (
                    "invalid_args: `data` looks like records (list of dicts "
                    "without Plotly trace keys). Provide `x_field` and "
                    "`y_field` so the server can pivot records into traces, "
                    "or pass `data` as a list of Plotly trace objects "
                    "(each carrying `x` / `y` / `type`)."
                ),
                "fix_hint": (
                    "Pick one column name from the records for x_field "
                    "(e.g., the time column) and one for y_field (e.g., "
                    "the value column). Add series_field if the records "
                    "contain multiple series and you want one trace per "
                    "series."
                ),
            }
        pivot_result = _records_to_traces(data, x_field, y_field, series_field)
        if isinstance(pivot_result, dict) and "error" in pivot_result:
            return pivot_result
        data = pivot_result

    final_layout = layout or {}
    if title and "title" not in final_layout:
        final_layout["title"] = title

    final_config = config or {"responsive": True, "displaylogo": False}

    return {
        "visualization": {
            "source": "Inline Plotly",
            "vizType": "plotly",
            "uuid": str(uuid.uuid4()),
            "inlineData": {
                "data": data,
                "layout": final_layout,
                "config": final_config,
            },
            "w": w,
            "h": h,
        }
    }


@mcp.tool(
    name="create_data_table",
    description=(
        "Create a NEW data table tile on the dashboard. Use this after "
        "fetching rows from a data-source tool — pass them in via `data` "
        "to render a table tile. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing table — use `patch_visualization` instead. "
        "Only call this for a NEW table that does not yet exist on the "
        "dashboard."
    ),
    tags=["visualization", "table"],
)
def create_data_table(
    data: Annotated[
        Optional[Union[List[Dict[str, Any]], str]],
        BeforeValidator(_unwrap_data_envelope),
        Field(
            default=None,
            description=(
                "MUST use `data_uri` when any recent tool result contained "
                "`_cache_uri` — pass that value as `data_uri` and do NOT "
                "inline `data`. Inline `data` is RESERVED for tables you "
                "constructed directly with no upstream query. Re-emitting "
                "upstream rows inline wastes output tokens; the engine "
                "resolves URIs server-side without re-tokenizing the bytes. "
                "Inline `data` over ~20 rows additionally triggers JSON "
                "parse errors on small models (chatbox-core rejects locally "
                "with an actionable error pointing at `data_uri`).\n\n"
                "Inline shape (only when no upstream `_cache_uri` exists): "
                "array of row objects. Each dict maps column names to cell "
                "values; all rows must share the same keys. MUST contain "
                "at least one row — do NOT call this with `data=[]`. If a "
                "data-source tool failed or returned no rows, ABORT and "
                "report the data-fetch error to the user; do NOT fall "
                "back to creating an empty table. Server unwraps dict "
                "envelopes carrying a `data` / `rows` / `records` list, "
                "so the cache-URI substitution path passes through "
                "cleanly."
            ),
            min_length=1,
        ),
    ] = None,
    data_uri: Annotated[
        Optional[Union[str, List[str]]],
        uri_field(inline_arg_name="data"),
    ] = None,
    title: Annotated[Optional[str], Field(description="Table title")] = None,
    subtitle: Annotated[Optional[str], Field(description="Table subtitle")] = None,
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 50 = half-width."
            ),
            ge=1,
            le=100,
        ),
    ] = 50,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 35 ≈ 175-350px "
                "tall, suitable for ~10-row tables. Do NOT pass values "
                "under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 35,
) -> Dict[str, Any]:
    """Create a data table visualization on the dashboard.

    Returns a visualization spec that renders using TethysDash's native DataTable component.
    """
    # See create_plotly_chart for the same exactly-one-of contract.
    err = ensure_exactly_one_set(data, "data", data_uri, "data_uri")
    if err:
        return {"error": err}
    if data_uri is not None:
        return {
            "error": (
                "invalid_args: `data_uri` arrived unresolved at the server. "
                "Cache URIs are resolved by chatbox-core's substitution "
                "layer before tool dispatch — non-chatbox-core MCP clients "
                "must pass inline `data` instead."
            ),
            "fix_hint": (
                "If you're using chatbox-core, ensure `enableResultCache={true}` "
                "is set on the <Chatbox> mount. If you're a different MCP "
                "client, retry with `data` populated inline as the structured "
                "array."
            ),
        }

    # Dict-coercion pattern (see docs/solutions/best-practices/mcp-tool-dict-parameter-coercion)
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            return {"error": f"invalid_args: `data` is not valid JSON: {e}"}

    # Server-side defense: Pydantic min_length catches data=[] but not
    # data="[]" (JSON string of empty array) — re-check after json.loads.
    if not isinstance(data, list) or len(data) == 0:
        return {
            "error": (
                "invalid_args: `data` must be a non-empty list of row "
                "objects. Fetch values from a data-source tool first; do "
                "NOT call create_data_table with empty data when a data "
                "fetch failed."
            )
        }

    return {
        "visualization": {
            "source": "Inline Table",
            "vizType": "table",
            "uuid": str(uuid.uuid4()),
            "inlineData": {
                "data": data,
                "title": title or "",
                "subtitle": subtitle or "",
            },
            "w": w,
            "h": h,
        }
    }


def _coerce_none_string(value: Any) -> Any:
    """Recover from a common LLM-syntax leak on Optional[Dict] / Optional[X] args.

    Several models (Ollama Cloud's nemotron-3, qwen-3.5, deepseek-pro-4 —
    observed 2026-05-18 across all three) emit the Python literal ``None``
    or its string form ``"None"`` / ``"null"`` as a value when the field
    is genuinely meant to be empty. Pydantic's ``Optional[Dict[str, Any]]``
    rejects the string ``"None"`` with a ``dict_type`` error, producing
    `argument validation failed` envelopes that the LLM can't easily
    recover from since the model thinks it correctly indicated "no value".

    This coercion runs at tool body entry — before any Pydantic-validated
    work uses the value — so the call proceeds normally if the LLM's only
    mistake was the syntax leak. Pure pass-through for any non-string-None
    value (real dicts, None, scalars, etc.).
    """
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped in ("none", "null", ""):
            return None
    return value


def _coerce_card_data(data: Any) -> List[Dict[str, Any]]:
    """Coerce LLM-provided card data into the shape the Card renderer expects.

    Card.js iterates `data` and reads `{label, value, color, icon}` per entry,
    so the renderer requires a list of dicts. But LLMs naturally pass scalars
    when the user says "card with value 42". This helper coerces at the tool
    boundary — matches the project's liberal dict-coercion convention (see
    ``docs/solutions/best-practices/mcp-tool-dict-parameter-coercion``).

    - None                  -> []                         (empty placeholder)
    - JSON string           -> parsed, then recursed      (LLM serialization)
    - scalar (int/str/etc.) -> [{"value": str(x)}]
    - dict                  -> [dict]
    - list of dicts         -> unchanged
    - list with scalars     -> each scalar wrapped in {"value": str(x)}
    """
    if data is None:
        return []
    if isinstance(data, str):
        stripped = data.strip()
        if stripped.startswith("[") or stripped.startswith("{"):
            # The string clearly looks like JSON — a leading `[` or `{` is
            # unambiguous LLM intent. Malformed JSON at that point is an
            # error, not a scalar label, per the project-wide dict-coercion
            # pattern used by add_map_service_layer and patch_visualization.
            try:
                return _coerce_card_data(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"`data` looks like JSON (starts with {stripped[0]!r}) "
                    f"but is malformed: {exc}"
                ) from exc
        # Plain strings like "42" or "Operational" were never intended as
        # JSON — treat as a scalar stat value.
        return [{"value": data}]
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [
            item if isinstance(item, dict) else {"value": str(item)}
            for item in data
        ]
    return [{"value": str(data)}]


@mcp.tool(
    name="create_card",
    description=(
        "Create a NEW card tile showing one or more key-value stats. "
        "Each data entry carries optional `label`, `value`, `color`, and "
        "`icon` fields. A bare scalar or single dict is auto-wrapped into a "
        "single-entry list; None renders an empty placeholder. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing card — use `patch_visualization` instead. "
        "Only call this for a NEW card that does not yet exist on the "
        "dashboard."
    ),
    tags=["visualization", "card"],
)
def create_card(
    title: Annotated[str, Field(description="Card title")],
    description: Annotated[Optional[str], Field(description="Card description text")] = None,
    data: Annotated[Optional[Any], Field(description=(
        "MUST use `data_uri` when any recent tool result contained "
        "`_cache_uri` — pass that value as `data_uri` and do NOT inline "
        "`data`. Inline `data` is RESERVED for stat tiles you constructed "
        "directly with no upstream query (typically 1-5 synthesized "
        "entries). Re-emitting upstream entries inline wastes output "
        "tokens; the engine resolves URIs server-side without "
        "re-tokenizing the list. Inline `data` over ~20 entries "
        "additionally triggers JSON parse errors on small models "
        "(chatbox-core rejects locally).\n\n"
        "Inline shape (only when no upstream `_cache_uri` exists): list "
        "of stat entries; each entry is a dict with optional `label`, "
        "`value`, `color`, and `icon`. Scalars, single dicts, and "
        "JSON-string payloads are coerced into list-of-dict form."
    ))] = None,
    data_uri: Annotated[
        Optional[Union[str, List[str]]],
        uri_field(inline_arg_name="data"),
    ] = None,
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 25 = quarter-width (cards are typically narrow)."
            ),
            ge=1,
            le=100,
        ),
    ] = 25,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 15 ≈ 75-150px tall, "
                "suitable for a small stat-card. Do NOT pass values under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 15,
) -> Dict[str, Any]:
    """Create a card visualization on the dashboard.

    Cards display a title, description, and a list of stat entries.
    Renders using TethysDash's native Card component.

    The ``data`` parameter is typed ``Any`` because the tool accepts several
    LLM-friendly shapes (scalar, dict, list-of-dicts, JSON-string) and
    normalizes internally via :func:`_coerce_card_data` to the
    ``List[Dict]`` shape the renderer expects. Malformed JSON-looking
    strings raise and produce an ``{"error": ...}`` envelope rather than
    silently becoming a scalar label.
    """
    # Exactly-one-of contract between `data` (inline) and `data_uri` (cache
    # URI). For `create_card` the inline form accepts None (empty placeholder
    # is valid) so the validator only fires when BOTH are set or when
    # `data_uri` arrived unresolved.
    if data is not None and data_uri is not None:
        return {
            "error": (
                "invalid_args: both `data` and `data_uri` were set on the "
                "same call. Pass EITHER inline `data` OR a `data_uri` "
                "(mcp+cache:// URI) but not both."
            )
        }
    if data_uri is not None:
        return {
            "error": (
                "invalid_args: `data_uri` arrived unresolved at the server. "
                "Cache URIs are resolved by chatbox-core's substitution "
                "layer before tool dispatch — non-chatbox-core MCP clients "
                "must pass inline `data` instead."
            ),
            "fix_hint": (
                "If you're using chatbox-core, ensure `enableResultCache={true}` "
                "is set on the <Chatbox> mount. If you're a different MCP "
                "client, retry with `data` populated inline."
            ),
        }

    try:
        coerced_data = _coerce_card_data(data)
    except ValueError as exc:
        return {"error": f"invalid_args: {exc}"}
    return {
        "visualization": {
            "source": "Inline Card",
            "vizType": "card",
            "uuid": str(uuid.uuid4()),
            "inlineData": {
                "title": title,
                "description": description or "",
                "data": coerced_data,
            },
            "w": w,
            "h": h,
        }
    }


@mcp.tool(
    name="create_text",
    description=(
        "Create a NEW text content block tile on the dashboard. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing text block — use `patch_visualization` "
        "instead. Only call this for a NEW text tile that does not yet "
        "exist on the dashboard."
    ),
    tags=["visualization", "text"],
)
def create_text(
    text: Annotated[
        str,
        Field(
            description=(
                "Text content to display. MUST be non-empty — do NOT call "
                "this with `text=''` (would produce an empty tile)."
            ),
            min_length=1,
        ),
    ],
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 50 = half-width."
            ),
            ge=1,
            le=100,
        ),
    ] = 50,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 15 ≈ 75-150px tall, "
                "suitable for short text. Do NOT pass values under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 15,
) -> Dict[str, Any]:
    """Create a text visualization on the dashboard.

    Renders using TethysDash's native Text component.
    """
    return {
        "visualization": {
            "source": "Text",
            "uuid": str(uuid.uuid4()),
            "args": {
                "text": text,
            },
            "w": w,
            "h": h,
        }
    }


@mcp.tool(
    name="create_custom_image",
    description=(
        "Display a NEW image-from-URL tile on the dashboard. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing image tile — use `patch_visualization` "
        "instead (e.g., to change the image URL on an existing tile). "
        "Only call this for a NEW image tile that does not yet exist on "
        "the dashboard."
    ),
    tags=["visualization", "image"],
)
def create_custom_image(
    image_url: Annotated[
        str,
        Field(
            description=(
                "URL of the image to display (http/https URL, data URI, or "
                "S3 path). MUST be non-empty."
            ),
            min_length=1,
        ),
    ],
    alt_text: Annotated[Optional[str], Field(description="Alt text for accessibility")] = None,
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 50 = half-width."
            ),
            ge=1,
            le=100,
        ),
    ] = 50,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 30 ≈ 150-300px "
                "tall. Do NOT pass values under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 30,
) -> Dict[str, Any]:
    """Create a custom image visualization on the dashboard.

    Displays an image from a URL. Renders using TethysDash's native Image component.
    """
    return {
        "visualization": {
            "source": "Custom Image",
            "uuid": str(uuid.uuid4()),
            "args": {
                "image_source": image_url,
            },
            "w": w,
            "h": h,
        }
    }


BASE_MAPS = {
    "light_gray": "https://server.arcgisonline.com/arcgis/rest/services/Canvas/World_Light_Gray_Base/MapServer",
    "dark_gray": "https://server.arcgisonline.com/arcgis/rest/services/Canvas/World_Dark_Gray_Base/MapServer",
    "topo": "https://server.arcgisonline.com/arcgis/rest/services/World_Topo_Map/MapServer",
    "imagery": "https://server.arcgisonline.com/arcgis/rest/services/World_Imagery/MapServer",
    "streets": "https://server.arcgisonline.com/arcgis/rest/services/World_Street_Map/MapServer",
    "terrain": "https://server.arcgisonline.com/arcgis/rest/services/World_Terrain_Base/MapServer",
    "ocean": "https://server.arcgisonline.com/arcgis/rest/services/Ocean/World_Ocean_Base/MapServer",
}

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
NOMINATIM_USER_AGENT = "TethysDash/1.0 (tethysdash@aquaveo.com)"


def _geocode(place_name):
    """Geocode a place name to (lon, lat) via Nominatim."""
    resp = http_requests.get(
        NOMINATIM_URL,
        params={"q": place_name, "format": "json", "limit": 1},
        headers={"User-Agent": NOMINATIM_USER_AGENT},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        return None
    return float(results[0]["lon"]), float(results[0]["lat"])


def _build_markers_layer(markers):
    """Build a GeoJSON VectorLayer configuration from a list of marker dicts."""
    features = []
    for m in markers:
        props = {}
        if m.get("label"):
            props["label"] = m["label"]
        features.append({
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [m["lon"], m["lat"]],
            },
            "properties": props,
        })
    geojson = {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "EPSG:4326"}},
        "features": features,
    }
    return {
        "configuration": {
            "type": "VectorLayer",
            "props": {
                "name": "Markers",
                "source": {"type": "GeoJSON", "props": {}, "geojson": geojson},
            },
        }
    }


@mcp.tool(
    name="create_map_visualization",
    description=(
        "Create a NEW geographic map visualization tile on the dashboard. "
        "Supports markers, drawing tools, and dashboard variable integration. "
        "Use 'center' for place names or 'map_extent' for explicit coordinates. "
        "When the user wants to focus on a specific feature, call a "
        "data-source feature-lookup tool first to obtain the bounding box "
        "and pass it via 'map_extent'. To add WMS, ESRI, GeoJSON, or other "
        "service layers to a NEWLY-CREATED map, call the matching `add_*_layer` "
        "tool (add_wms_layer, add_esri_image_layer, add_esri_feature_layer, "
        "add_geojson_layer, add_kml_layer, add_image_tile_layer, "
        "add_vector_tile_layer, add_pmtiles_vector_layer, add_pmtiles_raster_layer, "
        "add_geotiff_layer, add_static_image_layer, or add_dynamic_map_layer) "
        "with the returned map UUID. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing map — use `patch_visualization` for "
        "property edits (zoom, base map, etc.) and the appropriate "
        "`add_*_layer` tool to add a layer to an existing map. "
        "Only call this for a NEW map that does not yet exist on the "
        "dashboard."
    ),
    tags=["visualization", "map", "geographic", "location", "marker", "layer"],
)
def create_map_visualization(
    markers: Annotated[Optional[List[Dict[str, Any]]], Field(description=(
        "Simple point markers as [{\"lon\": <number>, \"lat\": <number>, \"label\": \"<text>\"}]. "
        "The server auto-builds a GeoJSON VectorLayer with correct CRS. "
        "Use this for quick markers instead of constructing full GeoJSON layer configs."
    ))] = None,
    center: Annotated[Optional[str], Field(description=(
        "Place name to center the map on (geocoded via Nominatim). "
        "Ignored when map_extent is provided."
    ))] = None,
    base_map: Annotated[Optional[str], Field(description=(
        "Base map shorthand or full ArcGIS MapServer URL. "
        "Shorthands: 'streets', 'imagery', 'topo', 'light_gray', 'dark_gray', 'terrain', 'ocean'. "
        "Any ArcGIS MapServer URL also works directly. Null for no base map."
    ))] = "streets",
    map_extent: Annotated[Optional[str], Field(description=(
        "Map extent as comma-separated coordinates in EPSG:4326 (lon/lat). "
        "Center+zoom: 'lon,lat,zoom'. Bounding box: 'minLon,minLat,maxLon,maxLat'. "
        "Takes precedence over 'center' when both are provided."
    ))] = None,
    zoom: Annotated[int, Field(description="Zoom level (1=world, 12=city, 18=street)")] = 12,
    layer_control: Annotated[bool, Field(description="Show layer visibility control panel")] = False,
    drawing_tools: Annotated[Optional[List[str]], Field(description=(
        "Drawing tool types to enable: 'Point', 'LineString', 'Polygon', 'Rectangle'. "
        "Null to disable drawing."
    ))] = None,
    drawing_limit: Annotated[int, Field(description="Max drawn features (0 = unlimited)")] = 0,
    drawing_variable: Annotated[Optional[str], Field(description=(
        "Dashboard variable name to publish drawn geometries to."
    ))] = None,
    extent_variable: Annotated[Optional[str], Field(description=(
        "Dashboard variable name to publish map extent on pan/zoom."
    ))] = None,
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 50 = half-width. Maps benefit from 50-100."
            ),
            ge=1,
            le=100,
        ),
    ] = 50,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 45 ≈ 225-450px tall, "
                "suitable for a regional map. Do NOT pass values under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 45,
) -> Dict[str, Any]:
    """Create a geographic map on the dashboard.

    Simple usage: provide 'center' or 'markers' for a quick map.
    For service layers (WMS, ESRI, GeoJSON, KML, GeoTIFF, etc.): call the
    matching `add_*_layer` tool (add_wms_layer, add_geojson_layer, etc.) with
    the returned map UUID after creating the map.
    """
    map_uuid = str(uuid.uuid4())
    LOGGER.info("create_map_visualization: uuid=%s, center=%s, markers=%s",
                map_uuid, center, markers is not None)

    # Resolve base map
    resolved_base_map = BASE_MAPS.get(base_map, base_map) if base_map else None

    # Build layers array from markers only (service layers added via add_map_service_layer)
    all_layers = []
    if markers:
        all_layers.append(_build_markers_layer(markers))

    # Resolve center via geocoding
    resolved_extent = None
    if map_extent:
        resolved_extent = {"extent": map_extent, "projection": "EPSG:4326"}
    elif center:
        coords = _geocode(center)
        if coords is None:
            return {"error": f"Could not geocode '{center}'. Try providing explicit coordinates via map_extent."}
        lon, lat = coords
        resolved_extent = {"extent": f"{lon},{lat},{zoom}", "projection": "EPSG:4326"}
    elif markers:
        # Auto-center on first marker
        lon, lat = markers[0]["lon"], markers[0]["lat"]
        resolved_extent = {"extent": f"{lon},{lat},{zoom}", "projection": "EPSG:4326"}

    # Add extent variable if requested
    if extent_variable and resolved_extent:
        resolved_extent["variable"] = extent_variable

    # Build drawing config
    map_drawing = None
    if drawing_tools:
        map_drawing = {"options": drawing_tools, "limit": drawing_limit}
        if drawing_variable:
            map_drawing["variable_name"] = drawing_variable

    # Build args matching the manual UI's format — only include keys with
    # actual values. The edit modal's checkAllInputs treats null as "unfilled"
    # and blocks save. mapConfig is not a registry arg (frontend generates it).
    args = {
        "baseMap": resolved_base_map,
        "layers": all_layers,
        "layerControl": layer_control,
    }
    if resolved_extent:
        args["map_extent"] = resolved_extent
    if map_drawing:
        args["mapDrawing"] = map_drawing

    return {
        "visualization": {
            "source": "Map",
            "uuid": map_uuid,
            "args": args,
            "w": w,
            "h": h,
        },
        "map_uuid": map_uuid,
        "message": (
            f"Map created (uuid: {map_uuid}). Use the matching `add_*_layer` "
            f"tool (add_wms_layer, add_esri_image_layer, add_esri_feature_layer, "
            f"add_geojson_layer, add_kml_layer, add_image_tile_layer, "
            f"add_vector_tile_layer, add_pmtiles_vector_layer, "
            f"add_pmtiles_raster_layer, add_geotiff_layer, add_static_image_layer, "
            f"or add_dynamic_map_layer) with this UUID to add WMS, ESRI, GeoJSON, "
            f"GeoTIFF, or other service layers."
        ),
    }


# ---------------------------------------------------------------------------
# Map service layer tool
# ---------------------------------------------------------------------------

# Plan-005 S2 cap: cap inline GeoJSON payloads at the MCP boundary to
# prevent an LLM or chatbox user from persisting a multi-MB feature
# collection that gets re-served on every dashboard fetch. Operators
# can raise the limits via env vars (e.g. for legitimate large
# datasets); defaults are conservative.
def _env_int(name: str, default: int) -> int:
    """Parse an integer from env var; fall back to default with a warning
    if the env value is missing or non-numeric. Avoids crashing module
    import on operator misconfiguration (e.g. setting MAX_BYTES='5mb')."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        LOGGER.warning(
            "Env var %s=%r is not a valid integer; using default %d.",
            name, raw, default,
        )
        return default


GEOJSON_MAX_BYTES = _env_int("TETHYSDASH_MCP_GEOJSON_MAX_BYTES", 5 * 1024 * 1024)
GEOJSON_MAX_FEATURES = _env_int("TETHYSDASH_MCP_GEOJSON_MAX_FEATURES", 10000)


# Dispatch table for layer_props application: maps each LAYER_PROPERTIES_ALLOWLIST
# key to the LayerConfigurationBuilder method that applies it. Coupled to the
# allowlist by a CI drift test (test_layer_props_dispatch_covers_allowlist) so
# adding a new allowlist key without a dispatch entry fails CI immediately —
# closes the silent-no-op gap from the prior 6-branch elif chain.
_LAYER_PROP_BUILDER_METHODS = {
    "opacity": "set_opacity",
    "minResolution": "set_min_resolution",
    "maxResolution": "set_max_resolution",
    "minZoom": "set_min_zoom",
    "maxZoom": "set_max_zoom",
    "minZoomQuery": "set_min_zoom_query",
    # Paired with LAYER_PROPERTIES_ALLOWLIST in plugin_helpers.py.
    # Drift test enforces parity.
    "visible": "set_layer_visibility",
}


def _validate_uuid_arg(
    value: Any, arg_name: str, returned_by_tool: str
) -> Optional[str]:
    """Validate that ``value`` is a well-formed UUID string.

    Some LLMs (observed: gemma4:31b) emit Mustache-style template
    placeholders like ``{{last_map_uuid}}`` for chained tool args,
    expecting the host framework to substitute the prior tool's return
    value. MCP doesn't do this. Without validation the literal string
    passes through and the layer/patch is silently dropped downstream
    when the dispatcher can't find a matching grid item.

    Returns ``None`` on success or a structured error string the caller
    wraps in ``{"error": ...}``. The string is always prefixed
    ``invalid_uuid:`` so the chatbox engine's ``toolErrorCheck`` routes
    it through the existing repair-loop machinery without further wiring.

    The fix-hint names the originating create_* tool so the LLM has a
    concrete recovery action to take on the next round.
    """
    # Defensive: Pydantic types this as ``str`` but null inputs from a
    # JSON-stringified payload could slip past in edge cases.
    if not isinstance(value, str):
        return (
            f"invalid_uuid: {arg_name} must be a UUID string returned by "
            f"{returned_by_tool}. Tool arguments are not template-substituted; "
            f"pass the literal UUID from the prior tool result."
        )
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        # Truncate at 80 chars so error logs don't bloat for adversarial
        # inputs while still showing the LLM what shape was rejected.
        offending = value if len(value) <= 80 else value[:77] + "..."
        return (
            f"invalid_uuid: {arg_name} must be a UUID string returned by "
            f"{returned_by_tool}. You provided {offending!r}, which is not "
            f"a valid UUID. Tool arguments are not template-substituted; "
            f"pass the literal UUID from the prior tool result."
        )
    return None


def _validate_source_params(
    source_type: str, params: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Reject `params` for source types that don't consume it.

    The producer's per-source-type ``_flat_source_props`` build (lines
    ~1148-1231 of this file) only handles ``params`` for WMS, ESRI Image
    and Map Service, ESRI Feature Service, and Static Image. The other
    seven types silently drop the argument — the renderer never sees
    the keys and the LLM has no indication.

    Returns ``None`` on success or an ``invalid_source_params:`` error
    string on rejection. Caller wraps in ``{"error": ...}``. Aligns with
    the existing ``invalid_uuid`` / ``invalid_envelope`` /
    ``whitelist_rejected`` error-class precedent.

    Runs AFTER the existing ``Union[Dict, str]`` coercion so the check
    operates on the parsed dict, not the raw string. See
    ``docs/solutions/best-practices/mcp-tool-dict-parameter-coercion-2026-04-17.md``.
    """
    if source_type not in _TYPES_REJECTING_PARAMS:
        return None
    if not params:
        # None and empty-dict are treated as "no params" — no rejection.
        return None
    return (
        f"invalid_source_params: {source_type} does not accept a `params` "
        f"argument. The renderer for {source_type} consumes only the "
        f"required source properties (and `attributions`/`projection` "
        f"via the `source_props` argument). Tool arguments not "
        f"recognized for this source type are silently dropped if not "
        f"explicitly rejected here. Pass `params=None` (the default) "
        f"and use `source_props` for any allowlisted source-type-"
        f"specific properties."
    )


def _validate_advanced_layer_dicts(
    *,
    source_type: str,
    layer_props: Optional[Dict[str, Any]],
    source_props: Optional[Dict[str, Any]],
    popup_options: Optional[Dict[str, Any]],
    opacity: Optional[float],
    min_zoom: Optional[int],
    max_zoom: Optional[int],
) -> Optional[Dict[str, str]]:
    """Validate the three advanced-dict params for add_map_service_layer.

    Returns None on success (caller proceeds to delegation), or
    {"error": <message>} on validation failure (caller short-circuits).

    Checks performed:
      - layer_props: dict shape, allowlist (LAYER_PROPERTIES_ALLOWLIST),
        per-key value type (rejects bool for numeric keys; rejects
        non-finite floats), flat-vs-dict conflict.
      - popup_options: dict shape; aliases sub-dict shape; omit sub-dict
        shape.
      - source_props: dict shape, per-source-type allowlist via
        get_allowed_source_prop_keys.

    Self-contained: no side effects, no external state. Tested directly
    via TestValidateAdvancedLayerDicts in test_layer_contracts.py.
    """
    flat_to_dict_layer_props = {
        "opacity": ("opacity", opacity),
        "min_zoom": ("minZoom", min_zoom),
        "max_zoom": ("maxZoom", max_zoom),
    }
    if layer_props:
        if not isinstance(layer_props, dict):
            return {"error": "layer_props must be a dict (or JSON-string dict)."}
        # Allowlist: keys must be known.
        unknown = set(layer_props) - set(LAYER_PROPERTIES_ALLOWLIST)
        if unknown:
            return {
                "error": (
                    f"layer_props contains unknown keys: {sorted(unknown)}. "
                    f"Allowed: {sorted(LAYER_PROPERTIES_ALLOWLIST)}"
                )
            }
        # Per-key value-type validation. The explicit `not isinstance(val, bool)`
        # check is necessary because Python's bool is a subclass of int —
        # without it, layer_props={"opacity": True} passes the (int, float)
        # check and persists `true` as the JSON value, which OpenLayers
        # treats as the boolean rather than the integer 1.
        # NaN/Infinity check: float('nan') and float('inf') pass isinstance
        # but produce invalid JSON (json.dumps emits NaN/Infinity literals
        # that fail RFC-compliant parsers). Persisting them breaks the
        # renderer downstream. Only opacity has a range guard inside the
        # builder (set_opacity); the other 5 props have no finite-value
        # check, so reject non-finite values at the boundary.
        for key, val in layer_props.items():
            expected = LAYER_PROPERTIES_ALLOWLIST[key]
            numeric_only = expected == (int, float) or expected in (int, float)
            if not isinstance(val, expected) or (
                numeric_only and isinstance(val, bool)
            ):
                return {
                    "error": (
                        f"layer_props[{key!r}] must be of type "
                        f"{expected!r}; got {type(val).__name__}."
                    )
                }
            if numeric_only and isinstance(val, float) and not math.isfinite(val):
                return {
                    "error": (
                        f"layer_props[{key!r}] must be a finite number; "
                        f"got {val!r}."
                    )
                }
        # Conflict check.
        for flat_name, (dict_key, flat_value) in flat_to_dict_layer_props.items():
            if flat_value is not None and dict_key in layer_props:
                return {
                    "error": (
                        f"Conflicting inputs: {flat_name!r} (flat parameter) "
                        f"and layer_props[{dict_key!r}] are both supplied. "
                        f"Pick one path per layer prop."
                    )
                }

    if popup_options is not None and not isinstance(popup_options, dict):
        return {"error": "popup_options must be a dict (or JSON-string dict)."}
    _popup_aliases = (popup_options or {}).get("aliases") or {}
    _popup_omit = (popup_options or {}).get("omit") or {}
    if not isinstance(_popup_aliases, dict):
        return {"error": "popup_options.aliases must be a dict."}
    if not isinstance(_popup_omit, dict):
        return {"error": "popup_options.omit must be a dict (layer_name -> [field, ...])."}

    if source_props is not None and not isinstance(source_props, dict):
        return {"error": "source_props must be a dict (or JSON-string dict)."}
    if source_props:
        # Per-source-type key allowlist: rejects keys absent from
        # available_source_properties[source_type]['required'|'optional'].
        # The tool description promises this validation; this enforces it.
        # Symmetric with layer_props's LAYER_PROPERTIES_ALLOWLIST guard.
        allowed_source_keys = get_allowed_source_prop_keys(source_type)
        unknown_source_keys = set(source_props) - allowed_source_keys
        if unknown_source_keys:
            return {
                "error": (
                    f"source_props contains keys not in the {source_type!r} "
                    f"allowlist: {sorted(unknown_source_keys)}. "
                    f"Allowed: {sorted(allowed_source_keys)}"
                )
            }

    return None


VALID_SOURCE_TYPES = [
    "WMS",
    "ESRI Image and Map Service",
    "ESRI Feature Service",
    "GeoJSON",
    "KML",
    "Image Tile",
    "Vector Tile",
    "PMTiles Vector",
    "PMTiles Raster",
    "GeoTIFF",
    "Static Image",
]

# Source types that don't consume `params` server-side. Calls supplying
# `params` for these types are rejected via `_validate_source_params` with
# a structured `invalid_source_params:` envelope so the LLM can correct
# on the next round. If a future feature adds per-type `params` semantics
# (e.g., GeoJSON style overrides, KML extractStyles), remove it from this
# set in the same change that wires the consumption path.
_TYPES_REJECTING_PARAMS = frozenset({
    "GeoJSON",
    "KML",
    "Image Tile",
    "Vector Tile",
    "PMTiles Vector",
    "PMTiles Raster",
    "GeoTIFF",
})

# Recognized directive prefixes for ESRI Image and Map Service `params.LAYERS`.
# Kept in sync with the JS constant in
# `reactapp/components/map/utilities.js` (the frontend `normalizeLayersParam`
# helper). The two values are duplicated by language but should never diverge —
# both name the ESRI directive vocabulary.
_RECOGNIZED_LAYERS_DIRECTIVES = ("show:", "hide:", "include:", "exclude:")


def _resolve_esri_layer_name(url: str, layer_id: Optional[str]) -> Optional[str]:
    """Fetch the ESRI service's actual layer name for the given layer index.

    The manual map editor uses the service's layer name as the
    attributeVariables key (via getImageArcGISRestLayerAttributes).  The ESRI
    /identify response also returns this name as ``layerName``.  Using the
    service name — not the client display name — ensures the click-time
    attribute variable lookup matches.

    When ``layer_id`` is None or unparseable, fetches the service metadata
    and falls back to layer 0's name if exactly one layer is published.
    Most NOAA / single-purpose MapServer URLs publish one layer at index 0;
    the LLM rarely passes a `layer_id` for those and the previous "give up
    on None" behavior silently broke attribute mapping.

    Returns None on any failure so the caller can fall back to the display name.
    """
    layer_index: Optional[int] = None
    if layer_id is not None:
        # Parse numeric layer index from layer_id (e.g., "show:0" → 0, "0" → 0)
        index_str = layer_id.split(":")[-1] if ":" in layer_id else layer_id
        try:
            layer_index = int(index_str)
        except (ValueError, TypeError):
            layer_index = None

    try:
        resp = http_requests.get(f"{url}?f=json", timeout=5)
        resp.raise_for_status()
        service_info = resp.json()
        layers = service_info.get("layers", [])
        if not layers:
            return None
        # Explicit layer_index match
        if layer_index is not None:
            for layer in layers:
                if layer.get("id") == layer_index:
                    return layer.get("name")
            return None
        # No layer_id supplied → if exactly one layer, use it
        if len(layers) == 1:
            return layers[0].get("name")
        # Multiple layers, no layer_id — ambiguous, fall back to display name
        return None
    except Exception as exc:
        LOGGER.warning(
            "Failed to resolve ESRI layer name from %s (layer_id=%s): %s",
            url, layer_id, exc,
        )
        return None


def _resolve_wms_attr_key(wms_layers: str, fallback_name: str) -> str:
    """Return the canonical layer-name key for WMS ``attributeVariables``.

    The React popup-render path (``reactapp/components/map/utilities.js``
    ``getImageWMSLayerAttributes``) keys alias maps by the WMS ``LAYERS``
    param value (e.g., ``"topp:states"``), not by the user-supplied display
    name. Storing ``attributeVariables`` keyed by the display name causes
    the click-time lookup to silently miss.

    This helper picks the first comma-separated entry from ``wms_layers``
    (stripped of whitespace). Multi-layer WMS calls are rare in practice
    and the React attribute-config UI is per-layer anyway; the first layer
    is the conservative pick. If ``wms_layers`` is empty or missing,
    falls back to ``fallback_name`` so behavior never breaks for
    misconfigured inputs.
    """
    if not wms_layers or not isinstance(wms_layers, str):
        return fallback_name
    first = wms_layers.split(",", 1)[0].strip()
    return first or fallback_name


# ---------------------------------------------------------------------------
# Per-source-type map-layer tools.
#
# These 11 tools replace the umbrella `add_map_service_layer`. Each has a
# flat narrow signature whose required + optional fields match exactly that
# source type's entry in `available_source_properties` (plugin_helpers.py).
# The MCP catalog itself is the per-type contract.
#
# Shared post-routing is factored into `_apply_common_layer_options` rather
# than inlined per-tool to avoid ~957 lines of duplicated code with high
# copy-paste-error risk. The helper is exercised end-to-end through every
# tool's per-tool oracle tests.
# ---------------------------------------------------------------------------


def _coerce_json_strings(args: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Coerce JSON-string values in ``args`` to dicts in-place.

    Some LLM providers stringify dict-typed tool args. This helper accepts
    a kwargs-name -> value mapping; for each value that is a string, it
    runs ``json.loads`` and rebinds the entry. Returns ``None`` on success,
    or ``{"error": ...}`` on the first parse failure (the caller short-
    circuits with the same envelope).
    """
    for arg_name, arg_value in list(args.items()):
        if isinstance(arg_value, str):
            try:
                args[arg_name] = json.loads(arg_value)
            except json.JSONDecodeError as exc:
                return {
                    "error": (
                        f"{arg_name} is not valid JSON: {exc.msg} "
                        f"(line {exc.lineno}, col {exc.colno})."
                    )
                }
    return None


def _apply_common_layer_options(
    builder: LayerConfigurationBuilder,
    *,
    opacity: Optional[float],
    min_zoom: Optional[int],
    max_zoom: Optional[int],
    visible: Optional[bool],
    queryable: bool,
    legend: Optional[Union[str, Dict[str, Any]]],
    style: Optional[Union[str, Dict[str, Any]]],
    attribute_variables: Optional[Dict[str, str]],
    attr_key: str,
    layer_props: Optional[Dict[str, Any]],
    popup_options: Optional[Dict[str, Any]],
) -> None:
    """Apply the cross-cutting shared layer-level options to ``builder``.

    Mirrors the umbrella's post-routing build block (preserved verbatim
    in semantics from the prior `add_map_service_layer` flow). The caller
    has already done UUID validation, JSON-string coercion, advanced-dict
    validation, source-type-specific required-field checks, and source
    routing (`set_source_properties` / `set_source_top_level_props` /
    `set_geojson`) before invoking this helper.

    Raises ``ValueError`` (caught by the caller's try-block and surfaced
    as ``{"error": ...}``) when popup_options sub-shapes are wrong.
    """
    if opacity is not None:
        builder.set_opacity(opacity)
    if min_zoom is not None:
        builder.set_min_zoom(min_zoom)
    if max_zoom is not None:
        builder.set_max_zoom(max_zoom)
    if visible is not None:
        builder.set_layer_visibility(visible)
    if layer_props:
        for key, val in layer_props.items():
            getattr(builder, _LAYER_PROP_BUILDER_METHODS[key])(val)
    if queryable:
        builder.set_queryable(True)
    if legend is not None:
        builder.set_legend(legend)
    if style is not None:
        builder.set_style(style)
    if attribute_variables:
        for key, variable in attribute_variables.items():
            builder.add_attribute_variable(key, variable, attr_key)
    _popup_aliases = _normalize_popup_outer_keys(
        (popup_options or {}).get("aliases") or {}, attr_key, "aliases"
    )
    _popup_omit = _normalize_popup_outer_keys(
        (popup_options or {}).get("omit") or {}, attr_key, "omit"
    )
    for layer_name, alias_map in _popup_aliases.items():
        if not isinstance(alias_map, dict):
            raise ValueError(
                f"popup_options.aliases[{layer_name!r}] must be a dict "
                f"of field-name -> alias."
            )
        for field, alias in alias_map.items():
            builder.add_attribute_alias(field, alias, layer_name)
    for layer_name, fields in _popup_omit.items():
        if not isinstance(fields, list):
            raise ValueError(
                f"popup_options.omit[{layer_name!r}] must be a list "
                f"of field names."
            )
        for field in fields:
            builder.omit_popup_attribute(field, layer_name)


def _normalize_popup_outer_keys(
    sub_dict: Dict[str, Any], layer_name: str, kind: str
) -> Dict[str, Any]:
    """Rewrite a single-entry popup sub-dict's outer key to match ``layer_name``.

    LLM behavior tolerance: when ``add_*_layer`` adds a single layer named
    ``layer_name``, but ``popup_options.aliases`` or ``popup_options.omit``
    is keyed by something other than the layer's name (e.g., ``"0"`` —
    a sublayer ID from ``params.LAYERDEFS`` — or the literal placeholder
    ``"layer_name"`` from a description-shape copy), rewrite the key.

    The React popup-render path (`reactapp/components/visualizations/Map.js`)
    looks up alias maps by **layer name** (the value passed via the tool's
    ``name`` arg). A mismatched outer key means the alias silently never
    fires at click time.

    Rules:
    * Empty dict -> return as-is.
    * Single-entry dict with key != ``layer_name`` -> rewrite to
      ``{layer_name: <inner>}``. Tag the call for log/debug visibility.
    * Multi-entry dict OR single-entry dict whose key already matches
      ``layer_name`` -> return as-is. Multi-entry case preserves the
      caller's apparent intent of authoring popups across multiple
      layers in one call, though that path is unusual for add_*_layer.

    ``kind`` is a label used in the debug log line (``"aliases"`` /
    ``"omit"``) so the rewrite is grep-able if it ever surprises a caller.
    """
    if not sub_dict:
        return sub_dict
    if len(sub_dict) != 1:
        return sub_dict
    only_key = next(iter(sub_dict))
    if only_key == layer_name:
        return sub_dict
    LOGGER.debug(
        "popup_options.%s outer key %r != layer name %r; rewriting "
        "to use the layer name (LLM tolerance).",
        kind, only_key, layer_name,
    )
    return {layer_name: sub_dict[only_key]}


# ---------------------------------------------------------------------------
# WMS
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_wms_layer",
    description=(
        "Add a WMS service layer to an existing map. Required: map_uuid "
        "(from create_map_visualization), name, url, wms_layers (the "
        "WMS LAYERS parameter value, e.g. workspace:layerName). Optional "
        "params merges additional WMS parameters (STYLES, TIME, etc.) "
        "into source.props.params; supports ${variable_name} substitution "
        "for dashboard variable references. Returns a layer_update result."
    ),
    tags=["map", "layer", "wms"],
)
def add_wms_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="WMS service URL")],
    wms_layers: Annotated[str, Field(description=(
        "WMS LAYERS parameter value in workspace:layer format. "
        "Comma-separated for multiple layers."
    ))],
    params: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Additional WMS source parameters merged into source.props.params "
        "(e.g., STYLES, TIME, FORMAT). Supports ${variable_name} for dashboard "
        "variable references."
    ))] = None,
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description=(
        "Maps feature attribute names to dashboard variable names. When a "
        "feature is clicked, attribute values are published to the corresponding "
        "dashboard variables."
    ))] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 (transparent) to 1 (opaque)")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level at which the layer is visible")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level at which the layer is visible")] = None,
    visible: Annotated[Optional[bool], Field(description=(
        "Initial layer visibility. Default behavior renders the layer visible. "
        "Set to False to start the layer hidden in the layer control."
    ))] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description=(
        "Layer legend. 'default' uses the source's default legend, a URL string "
        "for a hosted legend image, or a dict for a custom legend definition."
    ))] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description=(
        "Layer style. URL string (style JSON hosted externally) or dict for inline."
    ))] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Advanced source-side properties merged into source.props (e.g., "
        "attributions, projection). Validated against the source-type allowlist."
    ))] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Advanced layer-level properties (opacity, minResolution, maxResolution, "
        "minZoom, maxZoom, minZoomQuery, visible). Conflicts with the equivalent "
        "flat parameters are rejected — pick one path per layer prop."
    ))] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a WMS service layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_wms_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "params": params,
        "attribute_variables": attribute_variables,
        "source_props": source_props,
        "layer_props": layer_props,
        "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    params = coercible["params"]
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="WMS",
        layer_props=layer_props,
        source_props=source_props,
        popup_options=popup_options,
        opacity=opacity,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    extra_params = params if isinstance(params, dict) else {}
    wms_params = {"LAYERS": wms_layers}
    wms_params.update(extra_params)
    flat_source_props = {"url": url, "params": wms_params}

    try:
        builder = LayerConfigurationBuilder(name, "WMS")
        builder.set_source_properties(**flat_source_props)
        if source_props:
            builder.set_source_properties(**source_props)
        # WMS attr_key normalization: React's getImageWMSLayerAttributes
        # keys alias maps by the LAYERS param value (e.g., "topp:states"),
        # not by the user-supplied display name. Resolve attr_key to the
        # first comma-separated entry of wms_layers so the persisted
        # attributeVariables / popup_options keys match what the popup-
        # render path queries. See _resolve_wms_attr_key for full rationale.
        attr_key = _resolve_wms_attr_key(wms_layers, name)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=attr_key,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# ESRI Image and Map Service
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_esri_image_layer",
    description=(
        "Add an ESRI Image and Map Service layer to an existing map. Required: "
        "map_uuid, name, url. Optional layer_id (a bare integer or comma-"
        "separated integer list is canonicalized to show: form on emit; to "
        "use a different directive, prefix with show, hide, include, or "
        "exclude). Optional params merges additional source parameters "
        "(LAYERS, TIME, LAYERDEFS, mosaicRule). When attribute_variables is "
        "provided, the ESRI service's actual layer name is fetched and used "
        "as the attribute-variables key."
    ),
    tags=["map", "layer", "esri-image"],
)
def add_esri_image_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="ArcGIS REST service URL")],
    layer_id: Annotated[Optional[str], Field(description=(
        "Layer identifier within the service. A bare integer or comma-separated "
        "integer list is canonicalized to show: form on emit. To use a different "
        "directive, prefix with show, hide, include, or exclude (e.g., hide:0,1)."
    ))] = None,
    params: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Additional source parameters merged into source.props.params "
        "(LAYERS, TIME, LAYERDEFS, mosaicRule). Supports ${variable_name}."
    ))] = None,
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description=(
        "Maps feature attribute names to dashboard variable names. The "
        "service's actual layer name is fetched from {url}?f=json and used "
        "as the attribute-variables key."
    ))] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level at which the layer is visible")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level at which the layer is visible")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add an ESRI Image and Map Service layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_esri_image_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "params": params, "attribute_variables": attribute_variables,
        "source_props": source_props, "layer_props": layer_props,
        "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    params = coercible["params"]
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="ESRI Image and Map Service",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    extra_params = params if isinstance(params, dict) else {}
    esri_params = {}
    if layer_id:
        esri_params["LAYERS"] = layer_id
    esri_params.update(extra_params)
    if "LAYERS" in esri_params:
        layers_value = esri_params["LAYERS"]
        if not isinstance(layers_value, str):
            layers_value = str(layers_value)
        layers_value = layers_value.strip()
        if layers_value and not layers_value.startswith(_RECOGNIZED_LAYERS_DIRECTIVES):
            esri_params["LAYERS"] = "show:" + layers_value
        else:
            esri_params["LAYERS"] = layers_value
    flat_source_props = {"url": url}
    if esri_params:
        flat_source_props["params"] = esri_params

    attr_key = name
    # Resolve attr_key to the ESRI service's sublayer name whenever either
    # attribute_variables OR popup_options is provided. Both code paths feed
    # the same React popup-render machinery, which keys alias maps by the
    # ESRI service's actual sublayer name (fetched from ?f=json), NOT by the
    # user-supplied display name. PR #11's outer-key normalization rewrites
    # popup_options.{aliases,omit} to attr_key, so attr_key must be the
    # resolved sublayer name for popup_options to land on the right key.
    # Originally this block fired only `if attribute_variables:` — that
    # missed the popup_options.aliases-alone case (2026-05-21 audit).
    if attribute_variables or popup_options:
        effective_layer_id = flat_source_props.get("params", {}).get("LAYERS")
        if effective_layer_id is None:
            # LAYERDEFS encodes the sublayer ID as the prefix before the
            # first colon: "<layer_id>:<where_clause>" (e.g.,
            # "0:rivercountry = 'China'"). When the LLM sets LAYERDEFS but
            # not LAYERS or the layer_id arg, derive the layer index from
            # LAYERDEFS so the ?f=json lookup can resolve the actual ESRI
            # sublayer name. Without this fallback, attributeVariables /
            # popup_options end up keyed by the user-facing display name
            # (the `name` arg), while the React popup-render path queries
            # by the ESRI service's sublayer name — silent click-time
            # lookup failure.
            layerdefs = flat_source_props.get("params", {}).get("LAYERDEFS")
            if isinstance(layerdefs, str) and ":" in layerdefs:
                candidate = layerdefs.split(":", 1)[0].strip()
                if candidate.isdigit():
                    effective_layer_id = candidate
        # Fall back to the layer_id arg itself when not derivable from params.
        # _resolve_esri_layer_name also handles layer_id=None by checking
        # whether the service publishes exactly one layer (the common case
        # for NOAA single-purpose MapServer URLs).
        if effective_layer_id is None:
            effective_layer_id = layer_id
        resolved = _resolve_esri_layer_name(url, effective_layer_id)
        if resolved:
            attr_key = resolved
        else:
            LOGGER.warning(
                "Could not resolve ESRI layer name; falling back to display name '%s'",
                name,
            )

    try:
        builder = LayerConfigurationBuilder(name, "ESRI Image and Map Service")
        builder.set_source_properties(**flat_source_props)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=attr_key,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# ESRI Feature Service
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_esri_feature_layer",
    description=(
        "Add an ESRI Feature Service layer to an existing map. Required: "
        "map_uuid, name, url, layer_id (the integer index of the layer within "
        "the service, passed as a string and cast to int internally). Optional "
        "params merges feature-query parameters (TIME, WHERE) into "
        "source.props.params."
    ),
    tags=["map", "layer", "esri-feature"],
)
def add_esri_feature_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="ArcGIS Feature Service URL")],
    layer_id: Annotated[str, Field(description=(
        "Integer layer index as a string (the int form is the persisted shape; "
        "this string-typed parameter is cast to int internally)."
    ))],
    params: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Feature-query parameters (TIME, WHERE) merged into source.props.params. "
        "Supports ${variable_name}."
    ))] = None,
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description=(
        "Maps feature attribute names to dashboard variable names."
    ))] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add an ESRI Feature Service layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_esri_feature_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "params": params, "attribute_variables": attribute_variables,
        "source_props": source_props, "layer_props": layer_props,
        "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    params = coercible["params"]
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="ESRI Feature Service",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    try:
        layer_index = int(layer_id)
    except (TypeError, ValueError):
        return {"error": (
            "layer_id must be an integer index (passed as string and cast). "
            f"Got: {layer_id!r}."
        )}

    extra_params = params if isinstance(params, dict) else {}
    flat_source_props = {"url": url, "layer": layer_index}
    if extra_params:
        flat_source_props["params"] = extra_params

    try:
        builder = LayerConfigurationBuilder(name, "ESRI Feature Service")
        builder.set_source_properties(**flat_source_props)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# GeoJSON
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_geojson_layer",
    description=(
        "Add a GeoJSON layer to an existing map. Required: map_uuid, name, "
        "and exactly one of geojson (inline FeatureCollection or Feature dict) "
        "or geojson_url (a URL the frontend fetches at render time). When "
        "the inline payload is missing a CRS field, EPSG:4326 is auto-assigned. "
        "Inline GeoJSON is subject to feature-count and byte-size caps "
        "(env-overridable via TETHYSDASH_MCP_GEOJSON_MAX_FEATURES and "
        "TETHYSDASH_MCP_GEOJSON_MAX_BYTES)."
    ),
    tags=["map", "layer", "geojson"],
)
def add_geojson_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    geojson: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Inline GeoJSON FeatureCollection or Feature object. Required when "
        "geojson_url is not provided."
    ))] = None,
    geojson_url: Annotated[Optional[str], Field(description=(
        "URL to a GeoJSON file. Required when geojson is not provided. "
        "The frontend fetches the URL at render time."
    ))] = None,
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description=(
        "Maps feature attribute names to dashboard variable names."
    ))] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a GeoJSON layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_geojson_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "geojson": geojson, "attribute_variables": attribute_variables,
        "source_props": source_props, "layer_props": layer_props,
        "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    geojson = coercible["geojson"]
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    if not geojson and not geojson_url:
        return {"error": (
            "add_geojson_layer requires either 'geojson' (inline) or "
            "'geojson_url' (URL). Pass exactly one."
        )}
    if geojson and geojson_url:
        return {"error": (
            "add_geojson_layer accepts geojson OR geojson_url, not both. "
            "Pick one."
        )}

    advanced_err = _validate_advanced_layer_dicts(
        source_type="GeoJSON",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    if geojson_url:
        geojson_payload = geojson_url
    else:
        if not isinstance(geojson, dict):
            return {"error": "geojson must be a dict (or JSON-string dict)."}
        # Apply size caps for inline GeoJSON.
        max_features = _env_int("TETHYSDASH_MCP_GEOJSON_MAX_FEATURES", GEOJSON_MAX_FEATURES)
        max_bytes = _env_int("TETHYSDASH_MCP_GEOJSON_MAX_BYTES", GEOJSON_MAX_BYTES)
        feature_count = (
            len(geojson.get("features", []))
            if isinstance(geojson.get("features"), list)
            else 0
        )
        if feature_count > max_features:
            return {"error": (
                f"GeoJSON exceeds inline feature cap ({feature_count} > "
                f"{max_features}). Host the GeoJSON externally and pass "
                f"geojson_url, or override TETHYSDASH_MCP_GEOJSON_MAX_FEATURES."
            )}
        try:
            payload_bytes = len(json.dumps(geojson).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            return {"error": f"GeoJSON is not JSON-serializable: {exc}."}
        if payload_bytes > max_bytes:
            return {"error": (
                f"GeoJSON exceeds inline byte cap ({payload_bytes} > "
                f"{max_bytes}). Host the GeoJSON externally and pass "
                f"geojson_url, or override TETHYSDASH_MCP_GEOJSON_MAX_BYTES."
            )}
        geojson_payload = dict(geojson)
        if "crs" not in geojson_payload:
            geojson_payload["crs"] = {
                "type": "name",
                "properties": {"name": "EPSG:4326"},
            }

    try:
        builder = LayerConfigurationBuilder(name, "GeoJSON")
        builder.set_geojson(geojson_payload)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# KML
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_kml_layer",
    description=(
        "Add a KML layer to an existing map. Required: map_uuid, name, url. "
        "KML layers do not accept a `params` argument. Use source_props for "
        "advanced source-side properties (attributions, projection)."
    ),
    tags=["map", "layer", "kml"],
)
def add_kml_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="KML URL")],
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description=(
        "Maps feature attribute names to dashboard variable names."
    ))] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a KML layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_kml_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "attribute_variables": attribute_variables, "source_props": source_props,
        "layer_props": layer_props, "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="KML",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    try:
        builder = LayerConfigurationBuilder(name, "KML")
        builder.set_source_properties(url=url)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# Image Tile
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_image_tile_layer",
    description=(
        "Add an Image Tile (XYZ-style raster tile) layer to an existing map. "
        "Required: map_uuid, name, url. Image Tile layers do not accept a "
        "`params` argument. Use source_props for advanced source-side properties."
    ),
    tags=["map", "layer", "image-tile"],
)
def add_image_tile_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="Image Tile URL template (with {x}, {y}, {z} placeholders)")],
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description="Attribute -> variable map")] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add an Image Tile layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_image_tile_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "attribute_variables": attribute_variables, "source_props": source_props,
        "layer_props": layer_props, "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="Image Tile",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    try:
        builder = LayerConfigurationBuilder(name, "Image Tile")
        builder.set_source_properties(url=url)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# Vector Tile
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_vector_tile_layer",
    description=(
        "Add a Vector Tile layer to an existing map. Required: map_uuid, name, "
        "url. The url at the tool surface is a single template (or comma-"
        "separated list of templates with {x}/{y}/{z} placeholders) and is "
        "stored as `urls` in the persisted source.props (renamed at the "
        "MCP boundary). Vector Tile layers do not accept a `params` argument."
    ),
    tags=["map", "layer", "vector-tile"],
)
def add_vector_tile_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description=(
        "Vector tile URL template (with {x}, {y}/{-y}, {z} placeholders) or "
        "comma-separated list of templates."
    ))],
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description="Attribute -> variable map")] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a Vector Tile layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_vector_tile_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "attribute_variables": attribute_variables, "source_props": source_props,
        "layer_props": layer_props, "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="Vector Tile",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    try:
        builder = LayerConfigurationBuilder(name, "Vector Tile")
        # Tool surface uses singular `url`; persisted shape uses `urls`.
        builder.set_source_properties(urls=url)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# PMTiles Vector
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_pmtiles_vector_layer",
    description=(
        "Add a PMTiles Vector layer to an existing map. Required: map_uuid, "
        "name, url. PMTiles Vector layers do not accept a `params` argument. "
        "Use source_props for tileSize and other advanced source-side properties."
    ),
    tags=["map", "layer", "pmtiles-vector"],
)
def add_pmtiles_vector_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="PMTiles Vector URL")],
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description="Attribute -> variable map")] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props (tileSize, attributions)")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a PMTiles Vector layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_pmtiles_vector_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "attribute_variables": attribute_variables, "source_props": source_props,
        "layer_props": layer_props, "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="PMTiles Vector",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    try:
        builder = LayerConfigurationBuilder(name, "PMTiles Vector")
        builder.set_source_properties(url=url)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# PMTiles Raster
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_pmtiles_raster_layer",
    description=(
        "Add a PMTiles Raster layer to an existing map. Required: map_uuid, "
        "name, url. PMTiles Raster layers do not accept a `params` argument. "
        "Use source_props for tileSize and other advanced source-side properties."
    ),
    tags=["map", "layer", "pmtiles-raster"],
)
def add_pmtiles_raster_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="PMTiles Raster URL")],
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description="Attribute -> variable map")] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props (tileSize, attributions)")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a PMTiles Raster layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_pmtiles_raster_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "attribute_variables": attribute_variables, "source_props": source_props,
        "layer_props": layer_props, "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="PMTiles Raster",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    try:
        builder = LayerConfigurationBuilder(name, "PMTiles Raster")
        builder.set_source_properties(url=url)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# GeoTIFF
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_geotiff_layer",
    description=(
        "Add a GeoTIFF (Cloud Optimized GeoTIFF) raster layer to an existing "
        "map. Required: map_uuid, name, and either url (for a single source) "
        "or source_props={'sources': [{'url': ...}, ...]} (for multi-source "
        "GeoTIFFs). The auto-legend ramp keys (ramp_name, ramp_min, ramp_max) "
        "are stored at source-top-level so Map.js can render the colorbar; "
        "OL WebGLTile derives the tile colorization at render time. "
        "GeoTIFF layers do not accept a `params` argument."
    ),
    tags=["map", "layer", "geotiff"],
)
def add_geotiff_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[Optional[str], Field(description=(
        "Cloud Optimized GeoTIFF URL. Either url or source_props.sources is required."
    ))] = None,
    bands: Annotated[Optional[str], Field(description=(
        "Comma-separated band indices to render (e.g., '1,2,3'). Parsed at render time."
    ))] = None,
    nodata: Annotated[Optional[float], Field(description="NoData sentinel value (number)")] = None,
    min: Annotated[Optional[float], Field(description="Minimum value for color scaling")] = None,
    max: Annotated[Optional[float], Field(description="Maximum value for color scaling")] = None,
    ramp_name: Annotated[Optional[str], Field(description=(
        "Color ramp name for auto-legend rendering and tile colorization "
        "(e.g., a registered ramp such as 'viridis'). Used when legend is 'default'."
    ))] = None,
    ramp_min: Annotated[Optional[float], Field(description="Minimum value for the auto-legend ramp")] = None,
    ramp_max: Annotated[Optional[float], Field(description="Maximum value for the auto-legend ramp")] = None,
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description="Attribute -> variable map")] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend (use 'default' for auto-legend)")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Advanced source.props. Use {'sources': [{'url': ...}]} for multi-source "
        "GeoTIFFs or to override flat fields. Flat params take precedence over "
        "source_props for the same key."
    ))] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a GeoTIFF raster layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_geotiff_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "attribute_variables": attribute_variables, "source_props": source_props,
        "layer_props": layer_props, "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="GeoTIFF",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    geotiff_sources = (
        source_props.get("sources") if isinstance(source_props, dict) else None
    )
    if geotiff_sources is not None:
        if not isinstance(geotiff_sources, list) or not any(
            isinstance(source, dict) and source.get("url")
            for source in geotiff_sources
        ):
            return {"error": (
                "add_geotiff_layer requires source_props.sources to be a "
                "non-empty list of source dictionaries with a 'url' value, "
                "or a single 'url' parameter."
            )}
    elif not url:
        return {"error": (
            "add_geotiff_layer requires either 'url' or "
            "source_props={'sources': [{'url': ...}]}."
        )}

    # Build the source.props dict (excluding the top-level ramp keys).
    flat_source_props: Dict[str, Any] = {}
    if geotiff_sources is None:
        flat_source_props["sources"] = [{"url": url}]
    if bands is not None:
        flat_source_props["bands"] = bands
    if nodata is not None:
        flat_source_props["nodata"] = nodata
    if min is not None:
        flat_source_props["min"] = min
    if max is not None:
        flat_source_props["max"] = max

    # Top-level ramp keys (siblings of `type`/`props` on source).
    top_level_props: Dict[str, Any] = {}
    if ramp_name is not None:
        top_level_props["rampName"] = ramp_name
    if ramp_min is not None:
        top_level_props["rampMin"] = ramp_min
    if ramp_max is not None:
        top_level_props["rampMax"] = ramp_max

    try:
        builder = LayerConfigurationBuilder(name, "GeoTIFF")
        if flat_source_props:
            builder.set_source_properties(**flat_source_props)
        if top_level_props:
            builder.set_source_top_level_props(**top_level_props)
        if source_props:
            # Merge advanced source_props. Split top-level vs nested (mirrors
            # umbrella behavior for `rampName`/`rampMin`/`rampMax`).
            _top_level_keys = {"rampName", "rampMin", "rampMax"}
            sp_top = {k: source_props[k] for k in _top_level_keys if k in source_props}
            sp_nested = {k: v for k, v in source_props.items() if k not in _top_level_keys}
            if sp_nested:
                builder.set_source_properties(**sp_nested)
            if sp_top:
                builder.set_source_top_level_props(**sp_top)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# Static Image
# ---------------------------------------------------------------------------


@mcp.tool(
    name="add_static_image_layer",
    description=(
        "Add a Static Image overlay layer to an existing map. Required: "
        "map_uuid, name, url, projection (an EPSG code string), image_extent "
        "(comma-separated bounding-box string in the form 'minX,minY,maxX,maxY' "
        "in the projection's coordinate system). Static Image layers do not "
        "accept a `params` argument; projection and image_extent are flat "
        "parameters at this tool's surface (renamed to `imageExtent` in the "
        "persisted shape)."
    ),
    tags=["map", "layer", "static-image"],
)
def add_static_image_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    url: Annotated[str, Field(description="Image URL")],
    projection: Annotated[str, Field(description="EPSG code (e.g., the projection of the bounding box)")],
    image_extent: Annotated[str, Field(description=(
        "Bounding box as a comma-separated string 'minX,minY,maxX,maxY' "
        "in the projection's coordinate system."
    ))],
    queryable: Annotated[bool, Field(description="Enable click-to-query on this layer")] = False,
    attribute_variables: Annotated[Optional[Union[Dict[str, str], str]], Field(description="Attribute -> variable map")] = None,
    opacity: Annotated[Optional[float], Field(description="Layer opacity from 0 to 1")] = None,
    min_zoom: Annotated[Optional[int], Field(description="Minimum zoom level")] = None,
    max_zoom: Annotated[Optional[int], Field(description="Maximum zoom level")] = None,
    visible: Annotated[Optional[bool], Field(description="Initial layer visibility")] = None,
    legend: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer legend")] = None,
    style: Annotated[Optional[Union[str, Dict[str, Any]]], Field(description="Layer style")] = None,
    source_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced source.props (attributions, etc.)")] = None,
    layer_props: Annotated[Optional[Union[Dict[str, Any], str]], Field(description="Advanced layer-level props")] = None,
    popup_options: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Click-popup options. Accepts {'aliases': {layer_name: {field: alias}}} "
        "and {'omit': {layer_name: [field, ...]}} sub-dicts. The outer "
        "layer_name key MUST be the same string passed as this tool's `name` "
        "arg (the layer's display name) — NOT a sublayer ID from params, "
        "params.LAYERDEFS, layer_id, or any other identifier. The server "
        "auto-corrects single-entry sub-dicts with mismatched outer keys, "
        "but the LLM should still emit the correct shape."
    ))] = None,
) -> Dict[str, Any]:
    """Add a Static Image overlay layer to an existing map."""
    uuid_error = _validate_uuid_arg(map_uuid, "map_uuid", "create_map_visualization")
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info("add_static_image_layer: map_uuid=%s, name=%s", map_uuid, name)

    coercible = {
        "attribute_variables": attribute_variables, "source_props": source_props,
        "layer_props": layer_props, "popup_options": popup_options,
    }
    err = _coerce_json_strings(coercible)
    if err:
        return err
    attribute_variables = coercible["attribute_variables"]
    source_props = coercible["source_props"]
    layer_props = coercible["layer_props"]
    popup_options = coercible["popup_options"]

    advanced_err = _validate_advanced_layer_dicts(
        source_type="Static Image",
        layer_props=layer_props, source_props=source_props,
        popup_options=popup_options, opacity=opacity,
        min_zoom=min_zoom, max_zoom=max_zoom,
    )
    if advanced_err:
        return advanced_err

    flat_source_props = {
        "url": url,
        "projection": projection,
        "imageExtent": image_extent,
    }

    try:
        builder = LayerConfigurationBuilder(name, "Static Image")
        builder.set_source_properties(**flat_source_props)
        if source_props:
            builder.set_source_properties(**source_props)
        _apply_common_layer_options(
            builder,
            opacity=opacity, min_zoom=min_zoom, max_zoom=max_zoom,
            visible=visible, queryable=queryable, legend=legend, style=style,
            attribute_variables=attribute_variables, attr_key=name,
            layer_props=layer_props, popup_options=popup_options,
        )
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {"layer_update": {"map_uuid": map_uuid, "layer": layer_config}}


# ---------------------------------------------------------------------------
# (Reserved space — `add_map_service_layer` umbrella was deleted here when
# per-source-type tools replaced it. Tests previously targeting the umbrella
# have been migrated to per-tool oracle tests in
# test_per_source_type_layer_tools.py.)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Generic update protocol — patch_visualization (R1–R10)
# ---------------------------------------------------------------------------
#
# Validates RFC 6902 JSON Patch envelopes and returns them for client-side
# apply. The server does NOT persist or apply patches — it enforces the
# envelope contract (shape, whitelist, layer-construction boundary) and
# emits a {patch_update} envelope the engine accumulates and the reducer
# applies in DashboardLayout.js.
#
# Non-goals here:
#   - Server-side state access (the server is stateless re: grid items).
#   - Server-side patch apply (the reducer owns that, using rfc6902).
#   - Runtime target-UUID existence check (the reducer returns target_missing
#     if the UUID isn't in current React state).

_ALLOWED_OPS = {"add", "replace", "remove", "move", "test"}
_BARE_LAYER_INDEX = re.compile(r"^/args/layers/\d+$")

# Paths where the persisted value must be a specific Python shape. Enforced
# at the MCP boundary so the LLM gets a clear structured error rather than
# crashing the renderer (e.g., Card.js / DataTable.js both assume
# ``data.length`` works, which blows up for non-array payloads). Keep the
# list tight — each entry is a CONTRACT with a specific renderer.
_VALUE_SHAPE_RULES: Dict[tuple, tuple] = {
    # (source, path) -> (expected_type_tuple, type_label)
    ("Inline Plotly", "/args/inlineData/data"): ((list,), "list"),
    ("Inline Plotly", "/args/inlineData/layout"): ((dict,), "object"),
    ("Inline Plotly", "/args/inlineData/config"): ((dict,), "object"),
    ("Inline Table", "/args/inlineData/data"): ((list,), "list"),
    ("Inline Card", "/args/inlineData/data"): ((list,), "list"),
}


def _validate_value_shapes(source: str, ops: List[Dict[str, Any]]) -> Optional[str]:
    """Reject ops whose value type would later crash the renderer.

    Only runs on ``add`` / ``replace`` / ``test`` (ops that carry a value).
    ``remove`` and ``move`` are skipped. Matches paths exactly — deeper
    ops inside a constrained subtree are left to RFC 6902 semantics.
    """
    for i, op in enumerate(ops):
        if op.get("op") not in ("add", "replace", "test"):
            continue
        rule = _VALUE_SHAPE_RULES.get((source, op.get("path")))
        if not rule:
            continue
        expected, label = rule
        if not isinstance(op.get("value"), expected):
            return (
                f"op {i} at {op['path']!r} requires value type {label}, "
                f"got {type(op.get('value')).__name__}. The renderer for "
                f"{source!r} depends on this shape."
            )
    return None


def _coerce_known_values(source: str, ops: List[Dict[str, Any]]) -> None:
    """Mirror create-tool value coercions on matching patch ops.

    Keeps patch-vs-create behavior symmetric: anything the LLM can pass to a
    create tool should land the same way via patch. Mutates ``ops``
    in-place (the envelope is about to be returned verbatim to the client).
    """
    for op in ops:
        if op.get("op") not in ("add", "replace", "test"):
            continue
        # Mirror create_map_visualization's BASE_MAPS shorthand resolution
        # so ``{"op":"replace","path":"/args/baseMap","value":"imagery"}``
        # becomes the full ArcGIS MapServer URL before the reducer sees it.
        if source == "Map" and op.get("path") == "/args/baseMap":
            value = op.get("value")
            if isinstance(value, str):
                op["value"] = BASE_MAPS.get(value, value)


def _coerce_replace_to_add_on_layer_paths(
    source: str, ops: List[Dict[str, Any]]
) -> None:
    """Op-level normalization: ``replace`` -> ``add`` for Map layer-internal
    paths whose leaf segment is an object key (not an array index).

    RFC 6902 ``replace`` requires the target to exist; ``add`` creates if
    missing or replaces if present. For object-key paths these are
    functionally identical when the target exists. LLMs frequently emit
    ``replace`` for fields they want to "set" but that may be absent on
    layers created without them (e.g., ``params`` on an ESRI Feature
    Service layer created without filters).

    Bounded to:
      * ``Map`` source only — non-Map sources have their own internal
        shapes and the missing-target failure mode hasn't surfaced there.
      * Path under ``/args/layers/N/...`` (depth >= 4 segments) — the
        layer-construction boundary already rejects bare-index and
        whole-array forms before this runs, so any remaining ``replace``
        on Map at this depth is targeting a field within an existing
        layer.
      * Leaf segment is not a numeric index or ``-`` — those are JSON
        Pointer markers for arrays, where ``add`` and ``replace`` differ
        materially (insert vs substitute). Coercing them would change the
        semantics of legitimate array-element edits.

    Mutates ``ops`` in-place. No I/O, no telemetry — silent
    transformation that matches sibling value-coercion precedent.

    Sibling to ``_coerce_known_values`` in this module; both are called
    from ``patch_visualization`` after validation, before returning the
    envelope.
    """
    if source != "Map":
        return
    for op in ops:
        if op.get("op") != "replace":
            continue
        path = op.get("path", "")
        if not path.startswith("/args/layers/"):
            continue
        segments = path.split("/")
        # ``"/args/layers/0/x".split("/")`` -> ``["", "args", "layers", "0", "x"]``;
        # length 5 means one segment past ``/args/layers/N``.
        if len(segments) < 5:
            continue
        leaf = segments[-1]
        if leaf == "-" or leaf.isdigit():
            continue
        op["op"] = "add"


def _validate_patch_envelope_shape(ops):
    """R1/R2: structural validation.

    Returns an error string or None.
    """
    if not isinstance(ops, list):
        return "`ops` must be a list of operations"
    if len(ops) == 0:
        return "`ops` list is empty — envelopes must contain at least one op"
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            return f"op {i} is not an object"
        if "op" not in op:
            return f"op {i} missing required field `op`"
        if op["op"] not in _ALLOWED_OPS:
            return (
                f"op {i} has unsupported op {op['op']!r} "
                f"(supported: {sorted(_ALLOWED_OPS)}; `copy` intentionally excluded)"
            )
        if "path" not in op:
            return f"op {i} missing required field `path`"
        if not isinstance(op["path"], str):
            return f"op {i} field `path` must be a string"
        if not op["path"].startswith("/"):
            return f"op {i} path {op['path']!r} must be an absolute JSON Pointer starting with `/`"
        # Op-type-specific field requirements per RFC 6902
        if op["op"] in {"add", "replace", "test"} and "value" not in op:
            return f"op {i} ({op['op']}) missing required field `value`"
        if op["op"] == "move":
            if "from" not in op:
                return f"op {i} (move) missing required field `from`"
            if not isinstance(op["from"], str):
                return f"op {i} (move) field `from` must be a string"
            if not op["from"].startswith("/"):
                return (
                    f"op {i} (move) `from` {op['from']!r} must be an absolute "
                    f"JSON Pointer starting with `/`"
                )
    return None


def _check_r5c_array_collision(ops):
    """R5c: reject multi-op envelopes with >1 index-shifting op targeting the
    same array parent.

    Sequential-index semantics shift indices after each op — LLMs get this
    wrong. The LLM's recovery path is a single `replace` on the parent array.

    Applies to any indexed array parent (not just /args/layers). `move` is
    treated as `remove + add` and participates in the collision check at both
    its `from` and `path`. `replace` at an index is index-stable and does NOT
    participate.

    Returns an error string or None.
    """
    add_remove_by_parent = {}

    def _add_collision_candidate(parent_path, op_index):
        """Register this op's parent path if it ends in an array-index segment."""
        segments = parent_path.split("/")
        if len(segments) < 2:
            return
        last = segments[-1]
        # Only group when the trailing segment is an array-index marker
        if last == "-" or last.isdigit():
            parent = "/".join(segments[:-1])
            add_remove_by_parent.setdefault(parent, []).append(op_index)

    for i, op in enumerate(ops):
        op_name = op.get("op")
        if op_name == "add" or op_name == "remove":
            _add_collision_candidate(op.get("path", ""), i)
        elif op_name == "move":
            # A `move` is semantically `remove(from) + add(path)`. Both endpoints
            # participate in index-shift problems.
            _add_collision_candidate(op.get("from", ""), i)
            _add_collision_candidate(op.get("path", ""), i)
    for parent, indices in add_remove_by_parent.items():
        if len(indices) > 1:
            return (
                f"multiple add/remove ops target the same array parent {parent!r} "
                f"(op indices {indices}). RFC 6902 applies ops sequentially so "
                f"array indices shift after each op — this produces incorrect "
                f"results. Split the changes into separate turns so each turn "
                f"applies to a stable index, or use the appropriate create/"
                f"remove tool for new and deleted entries."
            )
    return None


def _check_layer_construction_boundary(source, ops):
    """R9/R10: Map layer construction is reserved for the `add_*_layer` tools
    (add_wms_layer, add_geojson_layer, add_esri_image_layer, etc.).

    Reject `add`/`replace` at /args/layers (whole array — the LLM's wrong-
    shape escape that produced the duplicate-layer bug).
    Reject `add` at /args/layers/- or /args/layers/N (creates new layer).
    Reject `replace` at /args/layers/N (replaces whole layer object).
    Allow field-level ops under an existing layer (e.g., /args/layers/N/visible).

    Returns an error string or None.
    """
    if source != "Map":
        return None
    for i, op in enumerate(ops):
        op_name = op.get("op")
        path = op.get("path", "")
        # Whole-array path. Catch this before the per-index branches so the
        # error names the more general failure mode (writing the entire
        # layers array vs. constructing a single new layer).
        if op_name in ("add", "replace") and path == "/args/layers":
            return (
                f"op {i} `{op_name}` at '/args/layers' would replace the "
                f"whole layers array, which is not permitted via "
                f"patch_visualization. To add a new layer use the matching "
                f"`add_*_layer` tool (add_wms_layer, add_geojson_layer, "
                f"add_esri_image_layer, etc.). To modify an existing layer's "
                f"fields, patch under '/args/layers/N/...'. To remove a "
                f"layer, use a `remove` op at '/args/layers/N'."
            )
        if op_name == "add":
            if path == "/args/layers/-" or _BARE_LAYER_INDEX.match(path):
                return (
                    f"op {i} `add` at {path!r} would construct a new map layer, "
                    f"which is not permitted via patch_visualization. Use the "
                    f"matching `add_*_layer` tool (add_wms_layer, add_geojson_layer, "
                    f"add_esri_image_layer, add_esri_feature_layer, add_kml_layer, "
                    f"add_image_tile_layer, add_vector_tile_layer, "
                    f"add_pmtiles_vector_layer, add_pmtiles_raster_layer, "
                    f"add_geotiff_layer, add_static_image_layer, or "
                    f"add_dynamic_map_layer) with its required flat parameters."
                )
        if op_name == "replace":
            if _BARE_LAYER_INDEX.match(path):
                return (
                    f"op {i} `replace` at {path!r} would replace a whole layer "
                    f"object, which is not permitted. Either patch individual "
                    f"fields within the existing layer (e.g., "
                    f"{path}/configuration/props/opacity or "
                    f"{path}/visible) or use the matching `add_*_layer` tool "
                    f"(add_wms_layer, add_geojson_layer, etc.) to add a new layer."
                )
    return None


def _emit_rejection_telemetry(
    *,
    reason: str,
    source: str,
    path: str,
    op_index: Optional[int] = None,
    **extra: Any,
) -> None:
    """Emit a structured INFO log record for a patch_visualization rejection.

    Feeds a downstream dataset that informs future R10 pattern-deny-list
    extensions. Kept intentionally minimal: no arg values, no external
    sink. The log-aggregation side is out of scope for this iteration.

    Keys are stable for log-parser consumers:
      * event: "whitelist_rejected"
      * source: the viz source name
      * path: the rejected JSON Pointer
      * op_index: zero-based op index when applicable
      * reason: machine-readable cause category
    """
    payload = {
        "event": "whitelist_rejected",
        "source": source,
        "path": path,
        "reason": reason,
    }
    if op_index is not None:
        payload["op_index"] = op_index
    if extra:
        payload.update(extra)
    LOGGER.info("patch_rejection %s", payload)


# ---------------------------------------------------------------------------
# configure_popup_modal_layer — Pydantic models + tool
# ---------------------------------------------------------------------------
#
# Configures the custom-popup-modal feature shipped on the tethysdash side
# 2026-05-13. Each map layer's persisted config can carry a popupConfig
# object that drives a click-popup modal with an embedded grid of
# visualizations. The MCP tool emits a single-op {patch_update} envelope
# targeting /args/layers/<layer_index>/popupConfig, re-using the existing
# apply_patch dispatch path in chatbox-core + DashboardLayout (no engine
# wiring change).
#
# Models below mirror the canonical persisted shape consumed by
# PopupConfigPane.js + DashboardLayout, so round-trip parity with a
# UI-configured popup is trivial. Server-side normalization fills in the
# fields the LLM shouldn't have to know about (uuid, i, id, stringified
# args/metadata).


class _PopupConfigPosition(BaseModel):
    """Modal popup position as percentage of viewport (matches PopupConfigPane.js)."""

    model_config = ConfigDict(extra="forbid")

    leftPct: Annotated[float, Field(ge=0, le=100)]
    topPct: Annotated[float, Field(ge=0, le=100)]
    widthPct: Annotated[float, Field(ge=20, le=100)]
    heightPct: Annotated[float, Field(ge=20, le=100)]


class _PopupConfigGridItemInput(BaseModel):
    """One embedded visualization inside a popup modal (LLM-facing shape).

    The server transforms this into the canonical persisted shape
    (``source``, ``args_string``, ``metadata_string``, ``uuid``, ``i``,
    ``id``, ``x/y/w/h``) before writing into the layer config. Per-source-
    type ``args`` validation is intentionally NOT enforced here — runtime
    errors surface via the existing per-tile error boundary.
    """

    model_config = ConfigDict(extra="forbid")

    source: Annotated[str, Field(min_length=1)]
    args: Dict[str, Any]
    x: Annotated[int, Field(ge=0)]
    y: Annotated[int, Field(ge=0)]
    w: Annotated[int, Field(ge=1)]
    h: Annotated[int, Field(ge=1)]
    metadata: Optional[Dict[str, Any]] = None


class _PopupConfigPayload(BaseModel):
    """Custom popup modal config for a single map layer.

    Mode is locked to ``"modal"`` in v1; the table-mode popup
    configuration uses the existing ``popup_options.{aliases, omit}``
    path on the ``add_*_layer`` tools.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["modal"]
    position: Optional[_PopupConfigPosition] = None
    titleTemplate: str = ""
    gridItems: Annotated[List[_PopupConfigGridItemInput], Field(min_length=1)]


# Centered 60x60 viewport rect — matches DEFAULT_POSITION at
# reactapp/components/modals/MapLayer/PopupConfigPane.js:24.
_DEFAULT_POPUP_POSITION: Dict[str, float] = {
    "leftPct": 20.0,
    "topPct": 20.0,
    "widthPct": 60.0,
    "heightPct": 60.0,
}

# Matches the React UI default for newly-authored popup gridItems
# (PopupLayoutEditor.js:182).
_DEFAULT_GRID_ITEM_METADATA: Dict[str, Any] = {"refreshRate": 0}


def _fetch_plugin_arg_names(source: str) -> Optional[List[str]]:
    """Fetch the declared ``arg_names`` for a registered viz source.

    Hits the same backend endpoint as ``list_intake_plugins`` but returns
    just the arg-name list for the given source. Used by
    ``configure_popup_modal_layer`` to case-normalize LLM-emitted
    ``gridItems[].args`` keys against the plugin's authoritative
    declared arg names.

    Returns:
        list[str] of arg_names when the source is found and has declared args.
        Empty list when the source is found but has no declared args (e.g.,
        Default registry types like Map/Text whose args are open-shape).
        ``None`` when the source isn't in the registry OR the fetch failed.
        Callers should treat ``None`` as "skip normalization" (don't reject
        the call — the LLM may be using a source name we don't know about).
    """
    if not TETHYSDASH_BASE_URL:
        return None
    try:
        response = http_requests.get(
            f"{TETHYSDASH_BASE_URL}/visualizations/list/",
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        groups = (
            data.get("visualizations", []) if isinstance(data, dict)
            else data if isinstance(data, list)
            else []
        )
        for group in groups:
            options = group.get("options", []) if isinstance(group, dict) else []
            for opt in options:
                if opt.get("source") != source:
                    continue
                args = opt.get("args", {})
                if not isinstance(args, dict):
                    return []
                return list(args.keys())
        return None
    except Exception as exc:
        LOGGER.warning(
            "Failed to fetch arg_names for source %r from %s: %s",
            source, TETHYSDASH_BASE_URL, exc,
        )
        return None


def _normalize_args_case(
    args: Dict[str, Any], arg_names: Optional[List[str]]
) -> Optional[Dict[str, Any]]:
    """Case-fold-match LLM-emitted ``args`` keys against the source's ``arg_names``.

    LLMs (especially small models like gemini-flash) aggressively
    snake-case identifiers regardless of explicit "case-sensitive"
    instructions — e.g., the user prompt "River ID" becomes
    ``{"river_id": ...}`` even when the plugin declared ``river_ID``.
    This helper rewrites mismatched keys to the canonical case.

    Rules:
    * ``arg_names`` is ``None`` (source not found / fetch failed) -> return
      ``args`` unchanged. Don't reject — the LLM may know something we don't.
    * ``arg_names`` is empty (source has no declared args, e.g., Default
      registry types) -> return ``args`` unchanged.
    * Exact match exists -> keep the key as-is.
    * Case-fold match exists and the canonical name is different -> rewrite.
    * No match for a key -> keep as-is (don't reject; surface as runtime
      error per the existing "cheap path" KTD #2 if the plugin rejects it).
    * Two LLM-emitted keys collide under case-fold (e.g., both
      ``river_id`` and ``RIVER_ID`` map to the same canonical
      ``river_ID``) -> return ``None`` to signal a structured error.

    Returns:
        The (possibly rewritten) args dict, OR ``None`` when two
        LLM-emitted keys collide case-insensitively. Caller converts
        ``None`` into a structured ``{error, fix_hint}`` envelope.
    """
    if arg_names is None or not arg_names:
        return args
    canonical_by_lower = {n.lower(): n for n in arg_names}
    seen_lower: Dict[str, str] = {}
    rewritten: Dict[str, Any] = {}
    for k, v in args.items():
        canonical = canonical_by_lower.get(k.lower(), k)
        if canonical in seen_lower.values():
            return None  # collision under case-fold
        seen_lower[k] = canonical
        rewritten[canonical] = v
    return rewritten


def _popup_validation_error_envelope(ve: ValidationError) -> Dict[str, Any]:
    """Convert a Pydantic ValidationError into a structured tool envelope.

    Names the failing field locations + the validator that fired so the
    LLM can self-repair in one cycle (Plan SC3, R6). Caps the number of
    enumerated errors to keep the envelope compact.
    """
    errors = []
    for err in ve.errors()[:8]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        errors.append({
            "field": loc or "<root>",
            "type": err.get("type", "value_error"),
            "msg": err.get("msg", "validation failed"),
        })
    return {
        "error": "invalid_popup_config: " + "; ".join(
            f"{e['field']} {e['msg']}" for e in errors
        ),
        "fix_hint": (
            "popup_config must be {mode: 'modal', gridItems: [...non-empty...], "
            "position?: {leftPct, topPct, widthPct, heightPct}, "
            "titleTemplate?: str}. Each gridItem needs {source, args (dict), "
            "x, y, w, h, metadata?}. See tool description for the full shape."
        ),
        "errors": errors,
    }


@mcp.tool(
    name="configure_popup_modal_layer",
    description=(
        "USE THIS TOOL for FIRST-TIME popup-modal setup on a map layer. "
        "It is THE correct tool whenever the user asks to 'enable', 'add', "
        "'configure', 'create', or 'set up' a Custom Popup Modal on an "
        "existing map layer — even though the map layer itself already "
        "exists. Adding a popupConfig to a layer for the first time is "
        "NOT 'modifying an existing visualization' in the patch_visualization "
        "sense; it's a structured setup operation with its own tool. "
        "DO NOT use patch_visualization to add a popupConfig from scratch — "
        "the popupConfig shape is non-trivial (mode, position, titleTemplate, "
        "gridItems with source/args/x/y/w/h), the canonical path is "
        "/args/layers/N/popupConfig (not /args/layers/N/configuration/...), "
        "and this tool builds the persisted shape correctly. "
        "Use patch_visualization ONLY for partial edits to an ALREADY-EXISTING "
        "popupConfig (e.g., changing just the titleTemplate via "
        "/args/layers/N/popupConfig/titleTemplate). Re-calling this tool "
        "REPLACES the entire popupConfig including all gridItem UUIDs. "
        "What it does: builds a custom click-popup that renders embedded "
        "visualizations (plots, tables, cards, text) with feature-attribute-"
        "substituted props, instead of (or alongside) the default attribute-"
        "table popup. "
        "Required: map_uuid (from create_map_visualization or dashboard_state), "
        "layer_index (0-based index into the map's layers, read from "
        "dashboard_state), and popup_config. Returns a patch_update envelope "
        "the chatbox engine applies via the existing apply_patch dispatch path. "
        "Configure the popup in a turn AFTER the layer was added by an "
        "add_*_layer tool — a same-turn add does NOT yet appear in "
        "dashboard_state, so layer_index would be stale and the patch would "
        "land on the wrong layer. "
        "Template strings inside popup_config (titleTemplate and any string "
        "value inside a gridItem's args dict) may embed ${feature.<key>} "
        "tokens that substitute against the clicked feature's attributes at "
        "popup-render time; missing keys resolve to empty string. "
        "Discover valid gridItem source names via list_available_visualizations "
        "(Default registry — Map, Text, Card, etc.), list_intake_plugins "
        "(intake-backed plugins), or register_runtime_plugin (runtime/MFE plugins)."
    ),
    tags=["map", "layer", "popup", "modal", "configure", "click", "feature"],
)
def configure_popup_modal_layer(
    map_uuid: Annotated[
        str,
        Field(description=(
            "UUID of the target map visualization. Source: dashboard_state "
            "or the return value of create_map_visualization."
        )),
    ],
    layer_index: Annotated[
        int,
        Field(ge=0, description=(
            "0-based index into the map's layers array (read from "
            "dashboard_state). Configure popups in a turn AFTER the layer "
            "was added by add_*_layer — a same-turn add does not yet appear "
            "in dashboard_state."
        )),
    ],
    popup_config: Annotated[
        Union[Dict[str, Any], str],
        Field(description=(
            "Full popup modal payload. Shape: {mode: 'modal', position?: "
            "{leftPct, topPct, widthPct, heightPct} percentages 0-100 "
            "(widthPct/heightPct clamped >= 20), titleTemplate?: str (may "
            "embed ${feature.<key>} tokens), gridItems: list of at least one "
            "{source: str, args: dict, x: int >=0, y: int >=0, w: int >=1, "
            "h: int >=1, metadata?: dict}}. Accepts both Dict and JSON-string Dict. "
            "CRITICAL — gridItems[].args keys are case-sensitive and MUST match "
            "the exact arg_names returned by list_intake_plugins / "
            "list_available_visualizations verbatim. The arg_names field is the "
            "authoritative source; do NOT normalize to snake_case from the "
            "user's natural-language phrasing or label. If list_intake_plugins "
            "returns arg_names containing river_ID (capital ID), the key MUST "
            "be river_ID — NOT river_id, River ID, or riverId. String values "
            "inside args may embed ${feature.<key>} tokens. "
            "CRITICAL — gridItems[].position fields (x, y, w, h) are cells in "
            "the popup's 100-column react-grid-layout (the SAME grid system "
            "as the main tethysdash dashboard — NOT Bootstrap's 12-column "
            "grid). Valid range: x, y >= 0; w, h >= 1; w <= 100. To make a "
            "single gridItem fill the popup width use w=100. Pick h based on "
            "expected content: h~25-30 for a single time-series plot "
            "(common case), h~35-40 for a tall plot or one with thick "
            "legends, h~15-20 for a card or short text block. Avoid h>50 "
            "unless the popup will scroll."
        )),
    ],
) -> Dict[str, Any]:
    """Build a {patch_update} envelope writing popup_config into the layer.

    Server-side validation flow:
      1. UUID format check on map_uuid
      2. JSON-string -> dict coercion for popup_config
      3. Pydantic shape validation against _PopupConfigPayload
      4. Server-side normalization: mint uuid4/i/id, stringify args/metadata
      5. Construct single RFC 6902 add op at /args/layers/<layer_index>/popupConfig

    Returns ``{patch_update: {uuid, source: 'Map', ops: [single_add_op]}}``
    on success or ``{error: "...", fix_hint: "..."}`` on validation failure.
    """
    uuid_error = _validate_uuid_arg(
        map_uuid,
        "map_uuid",
        "create_map_visualization (or dashboard_state)",
    )
    if uuid_error:
        return {"error": uuid_error}

    coercible = {"popup_config": popup_config}
    coerce_err = _coerce_json_strings(coercible)
    if coerce_err:
        return coerce_err
    popup_config_dict = coercible["popup_config"]

    if not isinstance(popup_config_dict, dict):
        return {
            "error": "invalid_popup_config: must be a dict (or JSON-string dict).",
            "fix_hint": (
                "Pass popup_config as a JSON object with at minimum {mode: 'modal', "
                "gridItems: [...]} plus optional position and titleTemplate."
            ),
        }

    try:
        payload = _PopupConfigPayload(**popup_config_dict)
    except ValidationError as ve:
        return _popup_validation_error_envelope(ve)

    position_dict = (
        payload.position.model_dump()
        if payload.position is not None
        else dict(_DEFAULT_POPUP_POSITION)
    )

    gridItems_persisted: List[Dict[str, Any]] = []
    for idx, item in enumerate(payload.gridItems):
        metadata_dict = (
            item.metadata if item.metadata is not None else dict(_DEFAULT_GRID_ITEM_METADATA)
        )
        # Server-side case-insensitive normalization for args keys against
        # the source's declared arg_names. Catches the LLM's near-universal
        # snake-case-normalization habit (e.g., emitting "river_id" when
        # the plugin declared "river_ID"). When the source has no declared
        # arg_names or isn't found in the registry, args pass through
        # unchanged. See _normalize_args_case for the full rule table.
        plugin_arg_names = _fetch_plugin_arg_names(item.source)
        normalized_args = _normalize_args_case(item.args, plugin_arg_names)
        if normalized_args is None:
            return {
                "error": (
                    f"invalid_popup_config: gridItems[{idx}].args contains keys "
                    f"that collide case-insensitively after matching against the "
                    f"plugin's declared arg_names. Source: {item.source!r}."
                ),
                "fix_hint": (
                    "Two of your args keys map to the same canonical arg_name "
                    "under case-fold (e.g., 'river_id' AND 'River_ID' both "
                    "match 'river_ID'). Keep only one entry per declared "
                    "arg_name. Use list_intake_plugins to confirm the exact "
                    "arg_names for this source."
                ),
            }
        gridItems_persisted.append({
            "i": str(idx + 1),
            "uuid": str(uuid.uuid4()),
            "id": None,
            "source": item.source,
            "args_string": json.dumps(normalized_args),
            "metadata_string": json.dumps(metadata_dict),
            "x": item.x,
            "y": item.y,
            "w": item.w,
            "h": item.h,
        })

    popup_config_persisted = {
        "mode": payload.mode,
        "position": position_dict,
        "titleTemplate": payload.titleTemplate,
        "gridItems": gridItems_persisted,
    }

    LOGGER.info(
        "configure_popup_modal_layer map_uuid=%s layer_index=%d gridItems=%d",
        map_uuid,
        layer_index,
        len(gridItems_persisted),
    )

    return {
        "patch_update": {
            "uuid": map_uuid,
            "source": "Map",
            "ops": [
                {
                    "op": "add",
                    "path": f"/args/layers/{layer_index}/popupConfig",
                    "value": popup_config_persisted,
                }
            ],
        }
    }


@mcp.tool(
    name="patch_visualization",
    description=(
        "Update an existing visualization in place via RFC 6902 JSON Patch ops. "
        "Use this to change properties on a visualization the user has already created — "
        "e.g., rename a plot, toggle a map's legend, remove a layer, replace table data. "
        "DO NOT use this for a visualization created in the same turn; include the change "
        "in the create call, or patch in a subsequent turn once the new UUID appears in "
        "dashboard_state. "
        "Look up the target UUID and its source type from the dashboard_state injection in "
        "your system context. The same injection carries `editable_paths_by_source` — the "
        "whitelist of allowed path prefixes for each viz source. Every path MUST start with "
        "`/args/...` (the persisted viz wraps its configuration in an `args` object). Each "
        "whitelist entry is a PREFIX you can extend: if `/args/inlineData` is allowed, then "
        "`/args/inlineData/layout/title`, `/args/inlineData/data/0/x`, and similar deeper "
        "paths are all allowed. "
        "Paths use JSON Pointer syntax (RFC 6901): literal `.` in a segment is preserved "
        "as-is (do not escape). Supported ops: add, replace, remove, move, test. "
        "Copy is intentionally excluded. "
        "To CREATE a new map layer, use the matching `add_*_layer` tool "
        "(add_wms_layer, add_geojson_layer, add_esri_image_layer, etc.) — this tool is for edits only."
    ),
    tags=["visualization", "patch", "update"],
)
def patch_visualization(
    uuid: Annotated[
        str,
        Field(description="UUID of the target visualization (from dashboard_state)."),
    ],
    source: Annotated[
        str,
        Field(description=(
            "Registry source name of the target visualization — must match the "
            "source shown in dashboard_state for this UUID."
        )),
    ],
    ops: Annotated[
        Union[List[Dict[str, Any]], str],
        Field(description=(
            "Array of RFC 6902 operations. Each operation is an object with "
            "`op` (one of: add, replace, remove, move, test), `path` (JSON "
            "Pointer string starting with '/'), and — depending on op — "
            "`value` (for add/replace/test) or `from` (for move). "
            "The server coerces a JSON-string payload to a list for callers "
            "that serialize arrays as strings."
        )),
    ],
    description: Annotated[
        Optional[str],
        Field(description=(
            "Optional natural-language summary of the change for audit + logging."
        )),
    ] = None,
) -> Dict[str, Any]:
    """Apply an RFC 6902 JSON Patch envelope against an existing visualization.

    Server-side validation only — envelope shape, R5c array-op collision,
    per-viz path whitelist, R9 layer-construction boundary. Returns
    `{patch_update: {uuid, source, ops}}` on success; the engine accumulates
    and the reducer applies client-side via rfc6902.

    Returns `{error: "..."}` with a structured error class prefix on failure:
    `invalid_envelope`, `whitelist_rejected`, `invalid_uuid`.
    """
    # Reject template placeholders / malformed UUIDs before any other work.
    # Cheapest reject; most actionable error for the LLM (the prior tool's
    # create_* return value is the recovery source of truth).
    uuid_error = _validate_uuid_arg(
        uuid,
        "uuid",
        "the create_* tool that returned this UUID",
    )
    if uuid_error:
        return {"error": uuid_error}

    # Dict-coercion pattern (see docs/solutions/best-practices/mcp-tool-dict-parameter-coercion)
    if isinstance(ops, str):
        try:
            ops = json.loads(ops)
        except json.JSONDecodeError as e:
            return {"error": f"invalid_envelope: `ops` is not valid JSON: {e}"}

    # R1/R2: envelope shape
    shape_error = _validate_patch_envelope_shape(ops)
    if shape_error:
        return {"error": f"invalid_envelope: {shape_error}"}

    # R5c: multi-op array collision
    r5c_error = _check_r5c_array_collision(ops)
    if r5c_error:
        return {"error": f"invalid_envelope: {r5c_error}"}

    # R7 / R9: per-source path whitelist (fail-closed). Dispatch by source:
    # static built-in viz types use the JSON-backed whitelist; Intake plugin
    # sources resolve at patch-time via editable_schemas_plugin.resolve_editable_paths.
    # Include the allowed prefixes in the error so the LLM can recover in one
    # round even if the initial dashboard_state injection was dropped or truncated.
    if source in LLM_EDITABLE_PATHS:
        allowed_prefixes = LLM_EDITABLE_PATHS[source]
        def _path_allowed(path: str) -> bool:
            return is_path_allowed(source, path)
    else:
        allowed_prefixes = resolve_editable_paths(source)
        def _path_allowed(path: str) -> bool:
            return is_path_allowed_plugin(source, path)
    for i, op in enumerate(ops):
        if not _path_allowed(op["path"]):
            _emit_rejection_telemetry(
                reason=(
                    "pattern_or_author_denied" if allowed_prefixes
                    else "resolution_failure_or_unknown_source"
                ),
                source=source,
                path=op["path"],
                op_index=i,
            )
            return {"error": (
                f"whitelist_rejected: op {i} path {op['path']!r} is not editable "
                f"for viz source {source!r}. Every path must start with `/args/...` "
                f"and fall under one of the allowed prefixes for this source: "
                f"{allowed_prefixes}. Each entry is a prefix you can extend "
                f"(e.g., if `/args/inlineData` is allowed, then "
                f"`/args/inlineData/layout/title` is allowed too)."
            )}
        # `move` reads from a second pointer and removes the node there.
        # The `from` path must also be in the whitelist, otherwise a
        # whitelisted `path` would surface arbitrary internal fields.
        if op["op"] == "move" and not _path_allowed(op["from"]):
            _emit_rejection_telemetry(
                reason=(
                    "pattern_or_author_denied" if allowed_prefixes
                    else "resolution_failure_or_unknown_source"
                ),
                source=source,
                path=op["from"],
                op_index=i,
                op="move.from",
            )
            return {"error": (
                f"whitelist_rejected: op {i} `from` {op['from']!r} is not "
                f"editable for viz source {source!r}. A `move` op is a read "
                f"followed by a write; both ends must fall under one of the "
                f"allowed prefixes for this source: {allowed_prefixes}."
            )}

    # R9/R10: layer-construction boundary (Map only)
    layer_error = _check_layer_construction_boundary(source, ops)
    if layer_error:
        return {"error": f"whitelist_rejected: {layer_error}"}

    # Value-shape validation: enforce renderer contracts that create tools
    # already enforce via Pydantic. Prevents a valid-envelope patch from
    # silently crashing Card.js / DataTable.js with a non-array payload.
    shape_error = _validate_value_shapes(source, ops)
    if shape_error:
        return {"error": f"invalid_envelope: {shape_error}"}

    # Value coercions that mirror the create tools (e.g., baseMap shorthand).
    # Run AFTER validation so shape rules apply to the pre-coercion value.
    _coerce_known_values(source, ops)

    # Op-level coercion: replace -> add for Map layer-internal paths.
    # Runs after the boundary check (so bare-index `replace` is already
    # rejected and never reaches this point) and after value coercion (so
    # the value rules apply to the pre-coercion shape if any field-specific
    # rules ever target an op-coerced path in the future).
    _coerce_replace_to_add_on_layer_paths(source, ops)

    LOGGER.info(
        "patch_visualization: uuid=%s source=%s ops=%d description=%s",
        uuid, source, len(ops), description,
    )
    return {
        "patch_update": {
            "uuid": uuid,
            "source": source,
            "ops": ops,
        }
    }


# ---------------------------------------------------------------------------
# Variable input + intake plugin tools
# ---------------------------------------------------------------------------

@mcp.tool(
    name="create_variable_input",
    description=(
        "Create a NEW interactive variable-input tile that other "
        "visualizations can reference with ${variable_name} syntax. Use "
        "the exact variable_name requested by the user; do not rename it "
        "or add suffixes unless the user explicitly asks. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing variable input — use `patch_visualization` "
        "instead (e.g., to change `initial_value`, `options`, or the "
        "input type). Only call this for a NEW variable input that does "
        "not yet exist on the dashboard."
    ),
    tags=["visualization", "dashboard", "variable"],
)
def create_variable_input(
    variable_name: Annotated[str, Field(description=(
        "Exact variable name used in ${...} references by other visualizations. "
        "Preserve the user's requested snake_case identifier exactly."
    ))],
    variable_type: Annotated[str, Field(description=(
        "Input type: 'text', 'number', 'checkbox', 'date', 'dropdown', "
        "'slider', 'date-range', or 'csv-uploader'"
    ))] = "text",
    initial_value: Annotated[str, Field(description="Default value for the variable input")] = "",
    options: Annotated[Optional[Union[List[str], str]], Field(description=(
        "Options for dropdown type. Provide as a comma-separated string "
        "or a list of strings."
    ))] = None,
    slider_min: Annotated[Optional[float], Field(description="Minimum value for slider type")] = None,
    slider_max: Annotated[Optional[float], Field(description="Maximum value for slider type")] = None,
    slider_step: Annotated[Optional[float], Field(description="Step increment for slider type")] = None,
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 25 = quarter-width (variable inputs are usually narrow)."
            ),
            ge=1,
            le=100,
        ),
    ] = 25,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 12 ≈ 60-120px tall, "
                "suitable for a single input control. Do NOT pass values under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 12,
) -> Dict[str, Any]:
    """Create a variable input on the dashboard.

    Variable inputs are interactive controls (text fields, dropdowns, sliders)
    that other visualizations can reference using ${variable_name} syntax.
    When the user changes the input, all linked visualizations auto-refresh.
    To link a plugin to this variable, call list_intake_plugins to get the source name,
    then render_plugin with args referencing ${variable_name}.
    """
    valid_types = [
        "text", "number", "checkbox", "date",
        "dropdown", "slider", "date-range", "csv-uploader",
    ]
    # Coerce string options to list (LLMs may pass "A,B,C" instead of ["A","B","C"])
    if isinstance(options, str):
        options = [o.strip() for o in options.split(",")]

    if variable_type == "text" and options:
        variable_type = "dropdown"

    if variable_type not in valid_types:
        return {"error": f"Invalid variable_type '{variable_type}'. Must be one of: {valid_types}"}

    # Validate type-specific requirements
    if variable_type == "dropdown" and not options:
        return {"error": "variable_type 'dropdown' requires the 'options' parameter"}
    if variable_type == "slider" and (slider_min is None or slider_max is None):
        return {"error": "variable_type 'slider' requires 'slider_min' and 'slider_max' parameters"}

    LOGGER.info("create_variable_input: name=%s, type=%s, initial=%s", variable_name, variable_type, initial_value)

    args = {
        "variable_name": variable_name,
        "initial_value": initial_value,
    }

    if variable_type == "dropdown":
        # Dropdown: variable_options_source is the array of option strings.
        # The frontend renders a select/dropdown when it receives an array.
        args["variable_options_source"] = options
    elif variable_type == "slider":
        args["variable_options_source"] = "slider"
        # Slider metadata goes in the dotted key that Base.js reads at line 336:
        # metadata: args["variable_options_source.metadata"]
        try:
            initial_num = float(initial_value) if initial_value else slider_min
        except (ValueError, TypeError):
            initial_num = slider_min
        args["variable_options_source.metadata"] = {
            "min": slider_min,
            "max": slider_max,
            "step": slider_step or 1,
            "dataType": "Number",
            "initialValue": initial_num,
            "outputFormat": "{{n}}",
        }
    else:
        args["variable_options_source"] = variable_type

    return {
        "visualization": {
            "source": "Variable Input",
            "vizType": "variableInput",
            "uuid": str(uuid.uuid4()),
            "args": args,
            "w": w,
            "h": h,
        }
    }


@mcp.tool(
    name="list_intake_plugins",
    description="List all installed backend plugins. Returns source (intake driver name), label, type, and argument names for each plugin. Use the 'source' field when calling render_plugin.",
    tags=["discovery", "plugin"],
)
def list_intake_plugins() -> Dict[str, Any]:
    """List all installed Python intake-driver plugins available in TethysDash.

    Returns a compact list of plugins with:
    - source: the intake driver name (USE THIS in render_plugin)
    - label: display name
    - type: visualization type (plotly, table, map, etc.)
    - arg_names: list of argument names the plugin accepts

    IMPORTANT: When calling render_plugin, use the 'source' field,
    NOT the 'label' field. The source is the Python driver name that
    the backend uses to instantiate the plugin.

    Note: Requires the TethysDash Django server to be running and
    TETHYSDASH_BASE_URL to be configured.
    """
    if not TETHYSDASH_BASE_URL:
        return _backend_not_configured_envelope()
    try:
        response = http_requests.get(
            f"{TETHYSDASH_BASE_URL}/visualizations/list/",
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
        # Django API returns {"visualizations": [...groups...]}, not a bare list
        groups = data.get("visualizations", []) if isinstance(data, dict) else data if isinstance(data, list) else []
        LOGGER.info("list_intake_plugins: fetched %d groups with %d total plugins",
                     len(groups),
                     sum(len(g.get("options", [])) for g in groups if isinstance(g, dict)))

        # Return compact format: source + label + type + arg names only.
        # Full argument schemas are large (~10-100KB) and cause the result
        # to be truncated, preventing the LLM from seeing all plugins.
        compact = []
        for group in groups:
            options = group.get("options", []) if isinstance(group, dict) else []
            for opt in options:
                entry = {
                    "source": opt.get("source", ""),
                    "label": opt.get("label", ""),
                    "type": opt.get("type", ""),
                    # Surfaces eligibility for add_dynamic_map_layer.
                    # A plugin is callable through that tool only when
                    # type=='map_layer' AND dynamic_map_layer=True.
                    "dynamic_map_layer": bool(
                        opt.get("dynamic_map_layer", False)
                    ),
                }
                # Extract just argument names from the args schema
                args = opt.get("args", {})
                if isinstance(args, dict):
                    entry["arg_names"] = list(args.keys())
                compact.append(entry)

        return {"intake_plugins": compact}
    except http_requests.RequestException as e:
        LOGGER.error("Failed to fetch intake plugins from Django: %s", e)
        return {"error": f"Failed to fetch intake plugins from TethysDash: {e}"}


def _resolve_dynamic_map_layer_plugin(source: str) -> Dict[str, Any]:
    """Look up a runtime map-layer plugin by source name.

    Returns a dict with one of:
      - {"plugin": <metadata_dict>}: the source resolved to a plugin
        flagged as a runtime map-layer plugin.
      - {"error": <message>}: source unknown OR resolved plugin is not
        a runtime map-layer (wrong type, or dynamic_map_layer=False).
      - {"error": "backend_not_configured", ...}: TETHYSDASH_BASE_URL
        is unset.
    """
    if not TETHYSDASH_BASE_URL:
        return _backend_not_configured_envelope()
    try:
        response = http_requests.get(
            f"{TETHYSDASH_BASE_URL}/visualizations/list/",
            timeout=10,
        )
        response.raise_for_status()
        # response.json() must stay inside the try: a 200 with non-JSON body
        # (Tethys session-expired login redirect, reverse-proxy maintenance
        # page) raises requests.exceptions.JSONDecodeError, which is NOT a
        # RequestException subclass and would otherwise propagate uncaught.
        data = response.json()
    except (http_requests.RequestException, ValueError) as exc:
        return {"error": f"Failed to fetch plugin metadata: {exc}"}

    groups = data.get("visualizations", []) if isinstance(data, dict) else []

    for group in groups:
        for opt in group.get("options", []) if isinstance(group, dict) else []:
            if opt.get("source") == source:
                if opt.get("type") != "map_layer":
                    return {
                        "error": (
                            f"Plugin {source!r} is type {opt.get('type')!r}; "
                            f"add_dynamic_map_layer requires type=='map_layer'."
                        )
                    }
                if not opt.get("dynamic_map_layer"):
                    return {
                        "error": (
                            f"Plugin {source!r} is a static map_layer plugin "
                            f"(dynamic_map_layer=False). Use the matching "
                            f"`add_*_layer` tool (add_wms_layer, add_geojson_layer, "
                            f"add_esri_image_layer, etc.) with the plugin's pre-baked "
                            f"layer config instead."
                        )
                    }
                return {"plugin": opt}

    return {"error": f"Unknown plugin source: {source!r}"}


@mcp.tool(
    name="add_dynamic_map_layer",
    description=(
        "Add a runtime plugin-backed map layer to an existing map. The plugin "
        "must be installed and flagged as a runtime map-layer plugin "
        "(type='map_layer' AND dynamic_map_layer=True). The MCP tool persists "
        "a GeoJSON scaffold layer with a pluginSource block; the plugin's "
        "fetch_features method is invoked at render time. Use list_intake_plugins "
        "to discover available plugin source names; only plugins with type "
        "'map_layer' are accepted here."
    ),
    tags=["map", "layer", "plugin", "runtime"],
)
def add_dynamic_map_layer(
    map_uuid: Annotated[str, Field(description="UUID returned by create_map_visualization")],
    source: Annotated[str, Field(description=(
        "Intake plugin source name (the 'source' field from list_intake_plugins). "
        "Must resolve to a plugin with type=='map_layer' and "
        "dynamic_map_layer=True; other plugins are rejected."
    ))],
    name: Annotated[str, Field(description="Display name for the layer in the layer control")],
    args: Annotated[Optional[Union[Dict[str, Any], str]], Field(description=(
        "Plugin args dict passed to fetch_features at render time. Keys MUST "
        "match the exact arg_names returned by list_intake_plugins for this "
        "source — case-sensitive. Do NOT normalize to snake_case from the "
        "user's natural-language phrasing. If list_intake_plugins returns "
        "arg_names containing region_ID (capital ID), the key MUST be "
        "region_ID — NOT region_id. The server auto-corrects case-fold-"
        "matching keys when TETHYSDASH_BASE_URL is configured. "
        "Supports ${variable_name} syntax in arg VALUES for dashboard "
        "variable references — these are preserved verbatim at persist time. "
        "Pass None or omit when the plugin takes no args."
    ))] = None,
) -> Dict[str, Any]:
    """Add a runtime plugin-backed map layer.

    Builder constructs a GeoJSON scaffold (empty FeatureCollection if no
    inline data) with configuration.props.pluginSource set, matching the
    persisted shape the UI's runtime-plugin selector produces. Render-time
    fetch failures surface through Map.js's existing visualization-error
    path (no new error handling here).
    """
    # Same UUID validation as the per-source-type add_* tools — reject
    # template placeholders / malformed UUIDs at the boundary.
    uuid_error = _validate_uuid_arg(
        map_uuid, "map_uuid", "create_map_visualization"
    )
    if uuid_error:
        return {"error": uuid_error}
    LOGGER.info(
        "add_dynamic_map_layer: map_uuid=%s, source=%s, name=%s",
        map_uuid, source, name,
    )

    # Coerce JSON-string args (consistent with the existing dict-parameter
    # coercion pattern used across these tools). Wrap json.loads so a
    # malformed string yields a clean MCP error rather than an opaque
    # ToolError from FastMCP.
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            return {
                "error": (
                    f"args is not valid JSON: {exc.msg} "
                    f"(line {exc.lineno}, col {exc.colno})."
                )
            }
    # set_plugin_source raises on non-dict args; coerce None → {} at the
    # boundary so callers don't need to know that detail.
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return {"error": "args must be a dict (or JSON-string dict) or None."}

    resolution = _resolve_dynamic_map_layer_plugin(source)
    if "error" in resolution:
        return resolution

    # Server-side case-insensitive normalization for args keys against the
    # source's declared arg_names. Catches the LLM's snake-case-
    # normalization habit (same root cause as configure_popup_modal_layer
    # and render_plugin). Soft-fails to pass-through when the source isn't
    # found or TETHYSDASH_BASE_URL is unset.
    plugin_arg_names = _fetch_plugin_arg_names(source)
    normalized_args = _normalize_args_case(args, plugin_arg_names)
    if normalized_args is None:
        return {
            "error": (
                f"invalid_args: keys collide case-insensitively after "
                f"matching against the plugin's declared arg_names. "
                f"Source: {source!r}."
            ),
            "fix_hint": (
                "Two of your args keys map to the same canonical arg_name "
                "under case-fold. Keep only one entry per declared arg_name. "
                "Use list_intake_plugins to confirm the exact arg_names."
            ),
        }

    try:
        builder = LayerConfigurationBuilder(name, "GeoJSON")
        builder.set_plugin_source(source, normalized_args)
        layer_config = builder.build()
    except ValueError as err:
        return {"error": str(err)}

    return {
        "layer_update": {
            "map_uuid": map_uuid,
            "layer": layer_config,
        }
    }


@mcp.tool(
    name="render_plugin",
    description=(
        "Create a NEW visualization tile using an installed backend plugin. "
        "Call list_intake_plugins first to discover available plugins and their args. "
        "Use the 'source' field from the results. "
        "To link to a dashboard variable input, use ${variable_name} syntax in arg values — "
        "the visualization auto-refreshes when the variable changes. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing plugin tile — use `patch_visualization` "
        "instead (e.g., to change a plugin arg). Only call this for a "
        "NEW plugin tile that does not yet exist on the dashboard."
    ),
    tags=["visualization", "dashboard", "plugin"],
)
def render_plugin(
    source: Annotated[str, Field(description="Intake driver name from the 'source' field in list_intake_plugins results. Always call list_intake_plugins first to get the exact source name. Do NOT guess or invent source names — using a wrong name causes a 'not installed' error.")],
    args: Annotated[Dict[str, Any], Field(description=(
        "Plugin arguments. Keys MUST match the exact arg_names returned by "
        "list_intake_plugins for this source — case-sensitive. The arg_names "
        "field is the authoritative source; do NOT normalize to snake_case "
        "from the user's natural-language phrasing or label. If list_intake_plugins "
        "returns arg_names containing river_ID (capital ID), the key MUST be "
        "river_ID — NOT river_id, River ID, or riverId. The server auto-corrects "
        "case-fold-matching keys when TETHYSDASH_BASE_URL is configured, but "
        "the LLM should still emit the correct shape. Use ${variable_name} "
        "syntax in arg VALUES to reference dashboard variable inputs."
    ))],
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 50 = half-width."
            ),
            ge=1,
            le=100,
        ),
    ] = 50,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 25 ≈ 125-250px tall. "
                "Do NOT pass values under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 25,
) -> Dict[str, Any]:
    """Create a visualization using a Python intake-driver plugin.

    IMPORTANT: Always call list_intake_plugins first to get the exact source name.
    The source parameter must match the 'source' field from list_intake_plugins results.
    Using a wrong or guessed name causes a 'Visualization is not installed' error.

    Args can reference variable inputs using ${variable_name} syntax —
    when the referenced variable changes, the visualization auto-refreshes.
    """
    LOGGER.info("render_plugin: source=%s, args=%s", source, args)

    # Server-side case-insensitive normalization for args keys against the
    # source's declared arg_names. Catches the LLM's near-universal
    # snake-case-normalization habit (e.g., emitting "river_id" when the
    # plugin declared "river_ID"). When the source isn't found in the
    # registry or TETHYSDASH_BASE_URL is unset, args pass through
    # unchanged. See _normalize_args_case for the full rule table.
    plugin_arg_names = _fetch_plugin_arg_names(source)
    normalized_args = _normalize_args_case(args, plugin_arg_names)
    if normalized_args is None:
        return {
            "error": (
                f"invalid_args: keys collide case-insensitively after "
                f"matching against the plugin's declared arg_names. "
                f"Source: {source!r}."
            ),
            "fix_hint": (
                "Two of your args keys map to the same canonical arg_name "
                "under case-fold (e.g., 'river_id' AND 'River_ID' both "
                "match 'river_ID'). Keep only one entry per declared "
                "arg_name. Use list_intake_plugins to confirm the exact "
                "arg_names for this source."
            ),
        }

    return {
        "visualization": {
            "source": source,
            "vizType": "intake_plugin",
            "uuid": str(uuid.uuid4()),
            "args": normalized_args,
            "w": w,
            "h": h,
        }
    }


# ---------------------------------------------------------------------------
# MFE rendering tools
# ---------------------------------------------------------------------------

# Hidden from LLM — not registered as an MCP tool.
# Kept as internal function for backward compatibility.
# Can be re-exposed later via BM25SearchTransform.
def render_mfe(
    url: Annotated[str, Field(description="URL to the MFE's remoteEntry.js")],
    scope: Annotated[str, Field(description="Module Federation scope name")],
    module: Annotated[str, Field(description="Module path starting with './' (e.g., './ChartPanel')")],
    remote_type: Annotated[str, Field(description="Federation type: 'vite-esm' or 'webpack'")] = "vite-esm",
    props: Annotated[Optional[Dict[str, Any]], Field(description="Props to pass to the MFE component")] = None,
    w: Annotated[int, Field(description="Grid width in columns (out of 100)")] = 50,
    h: Annotated[int, Field(description="Grid height in row units")] = 25,
) -> Dict[str, Any]:
    """Render a custom Module Federation microfrontend component on the dashboard.

    NOTE: This function is hidden from the LLM tool registry.
    Use render_custom_visualization for plugins listed in
    list_available_visualizations.
    """
    # Normalize module path — must start with './'
    normalized_module = module if module.startswith("./") else f"./{module.lstrip('/')}"

    return {
        "visualization": {
            "source": "Client Custom",
            "vizType": "custom",
            "args": {
                "url": url,
                "scope": scope,
                "module": normalized_module,
                "remoteType": remote_type,
                "initialData": props or {},
            },
            "w": w,
            "h": h,
        }
    }


# ---------------------------------------------------------------------------
# Client plugin rendering
# ---------------------------------------------------------------------------

def _validate_plugin_props(source: str, props: Dict) -> Optional[str]:
    """Validate props against a plugin's declared arg schema. Returns error string or None.

    When schema is non-empty:
    - Strips unknown props not in schema
    - Validates types for known props
    - Requires non-checkbox args

    When schema is empty: accepts any props (backward compat).
    """
    all_plugins = _get_all_plugins()
    plugin = next((p for p in all_plugins if p["source"] == source), None)
    if not plugin:
        available = [p["source"] for p in all_plugins]
        return f"Plugin '{source}' not found. Available: {available}"

    schema = plugin.get("args", {})

    # Empty schema: permissive mode (backward compat)
    if not schema:
        return None

    # Strip unknown props
    unknown = [k for k in props if k not in schema]
    if unknown:
        LOGGER.warning(
            "Stripping unknown props for '%s': %s (declared: %s)",
            source, unknown, list(schema.keys()),
        )
        for k in unknown:
            del props[k]

    # Validate declared args
    for arg_name, arg_spec in schema.items():
        value = props.get(arg_name)
        if value is None:
            continue
        if isinstance(arg_spec, list) and value not in arg_spec:
            return f"Invalid value '{value}' for '{arg_name}'. Must be one of: {arg_spec}"
        if isinstance(arg_spec, dict):
            # Rich schema: validate based on inner type field
            inner_type = arg_spec.get("type")
            if inner_type == "array" and not isinstance(value, list):
                return f"'{arg_name}' must be an array, got: {type(value).__name__}"
            if inner_type == "object" and not isinstance(value, dict):
                return f"'{arg_name}' must be an object, got: {type(value).__name__}"
            if inner_type == "string" and not isinstance(value, str):
                return f"'{arg_name}' must be a string, got: {type(value).__name__}"
            if inner_type == "number" and not isinstance(value, (int, float)):
                return f"'{arg_name}' must be a number, got: {type(value).__name__}"
            continue
        if arg_spec == "number" and not isinstance(value, (int, float)):
            return f"'{arg_name}' must be a number, got: {type(value).__name__}"
        if arg_spec == "text" and not isinstance(value, str):
            return f"'{arg_name}' must be a string, got: {type(value).__name__}"
        if arg_spec == "object" and not isinstance(value, dict):
            return f"'{arg_name}' must be an object, got: {type(value).__name__}"
        if arg_spec == "array" and not isinstance(value, list):
            return f"'{arg_name}' must be an array, got: {type(value).__name__}"

    # Check required args — rich schemas can declare required: false
    for arg_name, arg_spec in schema.items():
        if arg_name in props:
            continue
        # Rich schema objects: check required field (default: true)
        if isinstance(arg_spec, dict) and not arg_spec.get("required", True):
            continue
        # Simple type strings: checkbox is optional, everything else required
        if arg_spec == "checkbox":
            continue
        return f"Missing required arg: '{arg_name}'"

    return None


@mcp.tool(
    name="render_custom_visualization",
    description=(
        "Render a NEW registered custom-visualization component as a tile "
        "on the dashboard. Call list_available_visualizations first to "
        "see available custom plugins. "
        "DO NOT call this when the user named an existing visualization "
        "UUID (from `dashboard_state`) OR asked to add to / modify / "
        "update an existing custom-component tile — use "
        "`patch_visualization` instead (e.g., to change a prop on the "
        "rendered component). Only call this for a NEW custom-component "
        "tile that does not yet exist on the dashboard."
    ),
    tags=["dashboard", "visualization", "custom"],
)
def render_custom_visualization(
    source: Annotated[str, Field(description="Client plugin source name from list_available_visualizations")],
    props: Annotated[Optional[Dict[str, Any]], Field(description="Props to pass to the plugin component. Check list_available_visualizations for each plugin's required args and valid values.")] = None,
    w: Annotated[
        int,
        Field(
            description=(
                "Tile WIDTH as a fraction of dashboard width (1-100). "
                "Default 50 = half-width."
            ),
            ge=1,
            le=100,
        ),
    ] = 50,
    h: Annotated[
        int,
        Field(
            description=(
                "Tile HEIGHT in grid row-units. Default 30 ≈ 150-300px "
                "tall. Do NOT pass values under 10."
            ),
            ge=10,
            le=100,
        ),
    ] = 30,
) -> Dict[str, Any]:
    """Render a registered client plugin on the dashboard.

    ALWAYS use this tool for plugins listed in
    list_available_visualizations. This tool reads the correct URL, scope,
    module, and validates props against the plugin's declared arg schema.
    """
    safe_props = props or {}
    all_plugins = _get_all_plugins()

    LOGGER.info("render_custom_visualization called: source=%s, props=%s", source, safe_props)

    validation_error = _validate_plugin_props(source, safe_props)
    if validation_error:
        return {"error": validation_error}

    plugin = next((p for p in all_plugins if p["source"] == source), None)
    if plugin is None:
        available = [p["source"] for p in all_plugins]
        LOGGER.warning("Plugin '%s' not found. Available: %s", source, available)
        return {"error": f"Plugin '{source}' not found. Available: {available}"}

    # Runtime MFE plugins: return Module Federation coordinates.
    # The frontend resolves the remoteEntry.js URL (see chatbox.jsx handleResult).
    if plugin.get("type") == "client_custom_remote":
        viz_spec = {
            "source": "Client Custom",
            "vizType": "custom",
            "uuid": str(uuid.uuid4()),
            "scope": plugin.get("scope"),
            "module": plugin.get("module"),
            "remoteType": plugin.get("remoteType", "vite-esm"),
            "args": safe_props,
            "w": w,
            "h": h,
        }
        if plugin.get("url"):
            viz_spec["url"] = plugin["url"]
        if plugin.get("dataKey"):
            viz_spec["dataKey"] = plugin["dataKey"]
        return {"visualization": viz_spec}

    # Plugin is registered but not a runtime remote. Fail closed.
    plugin_type = plugin.get("type")
    LOGGER.warning(
        "Plugin '%s' has unsupported type '%s' (only 'client_custom_remote' is renderable).",
        source,
        plugin_type,
    )
    return {
        "error": (
            f"Plugin '{source}' has unsupported type '{plugin_type}'. "
            f"Only 'client_custom_remote' plugins can be rendered."
        )
    }


# ---------------------------------------------------------------------------
# Runtime plugin registration
# ---------------------------------------------------------------------------

@mcp.tool(
    name="register_runtime_plugin",
    description="Register a runtime MFE plugin so it appears in available visualizations and can be rendered with render_custom_visualization",
    tags=["dashboard", "plugin", "register"],
)
def register_runtime_plugin(
    url: Annotated[str, Field(description="URL to the remoteEntry.js file")],
    scope: Annotated[str, Field(description="Module Federation scope name")],
    module: Annotated[str, Field(description="Exposed module path (e.g., './MyPanel')")],
    label: Annotated[str, Field(description="Display name for the plugin")],
    remote_type: Annotated[str, Field(description="Remote type: 'vite-esm' or 'webpack'")] = "vite-esm",
    description: Annotated[str, Field(description="Human-readable description")] = "",
    group: Annotated[str, Field(description="Visualization group/category")] = "Custom",
    data_key: Annotated[str, Field(description="variableInputValues key this panel reads")] = "",
) -> Dict[str, Any]:
    """Register a runtime MFE plugin so it appears in available visualizations.

    Not supported in standalone mode. The standalone MCP server reads the
    runtime plugin registry from tethysdash over HTTP and has no
    authenticated write path; registration must go through the browser-
    side chatbox UI (which posts to tethysdash's ``runtime-plugins/sync/``
    endpoint with the user's session credential). Re-enables once auth
    plus a write-side HTTP path land (deferred).

    The arguments are preserved on the tool signature so the prompt /
    slash-command surface (``test_prompts.py`` parity contract) keeps
    working; we simply refuse the call with a structured envelope rather
    than attempting a write the standalone cannot perform.
    """
    LOGGER.info(
        "register_runtime_plugin called in standalone mode (not supported): "
        "url=%s, scope=%s, module=%s, label=%s",
        url, scope, module, label,
    )
    return {
        "error": "registration_not_supported",
        "message": (
            "Runtime plugin registration via the MCP tool is not available "
            "in the standalone tethysdash MCP server. Use the chatbox UI's "
            "plugin-registration flow (which posts to tethysdash's "
            "/runtime-plugins/sync/ endpoint with the user's browser "
            "session) to register the plugin; once registered, this server "
            "will see it on its next list_available_visualizations call."
        ),
    }


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------

def _collect_intake_plugin_whitelists() -> Dict[str, List[str]]:
    """Return {source: resolved_paths} for every registered Intake plugin.

    Iterates ``intake.source.registry`` and resolves each source through the
    shared editable-paths resolver. Used by list_available_visualizations to
    feed the client-side dashboard_state injection with authoritative
    server-computed whitelists (see Unit C2 of the plan).
    """
    try:
        import intake
        registry = intake.source.registry
    except (ImportError, AttributeError):
        return {}
    results: Dict[str, List[str]] = {}
    try:
        source_names = list(registry)
    except TypeError:
        return {}
    for source in source_names:
        # Skip obvious non-plugin drivers. The canonical TethysDash plugin
        # signal is whether resolve_editable_paths returns a non-empty list
        # OR the plugin is in the TethysDashPlugin family. We surface every
        # source the resolver recognizes; those that are not TethysDashPlugin
        # subclasses will return empty lists, which we still emit so the
        # client can distinguish "known but no paths" from "unknown".
        results[source] = resolve_editable_paths(source)
    return results


@mcp.tool(
    name="list_available_visualizations",
    description="List all visualization types: native (charts, tables, maps), registered custom visualizations, and MFE components",
    tags=["discovery"],
)
def list_available_visualizations() -> Dict[str, Any]:
    """List all visualization types available for creating dashboard items.

    Returns built-in types (plotly, table, map, card, text, image),
    installed client plugins with argument schemas, and MFE rendering info.
    """
    return {
        "builtin": [
            {
                "type": "plotly",
                "name": "Plotly Chart",
                "tool": "create_plotly_chart",
                "description": "Interactive line/bar/scatter charts via Plotly.js",
                "prefer_native": True,
            },
            {
                "type": "table",
                "name": "Data Table",
                "tool": "create_data_table",
                "description": "Tabular data display with headers and rows",
                "prefer_native": True,
            },
            {
                "type": "map",
                "name": "Map",
                "tool": "create_map_visualization",
                "description": (
                    "OpenLayers map. Supports WMS, GeoJSON, KML, ESRI "
                    "services, Image/Vector tiles, PMTiles, GeoTIFF, "
                    "Static Image"
                ),
                "prefer_native": True,
            },
            {
                "type": "card",
                "name": "Card",
                "tool": "create_card",
                "description": "Simple card with title, description, and data value",
                "prefer_native": True,
            },
            {
                "type": "image",
                "name": "Custom Image",
                "tool": "create_custom_image",
                "description": "Display an image from a URL",
                "prefer_native": True,
            },
            {
                "type": "text",
                "name": "Text",
                "tool": "create_text",
                "description": "Static text content",
                "prefer_native": True,
            },
        ],
        "client_plugins": [
            {
                "source": plugin["source"],
                "label": plugin.get("label", plugin["source"]),
                "group": plugin.get("group", "Client Plugins"),
                "description": plugin.get("description", ""),
                "tags": plugin.get("tags", []),
                "args_schema": _convert_plugin_args_to_schema(plugin.get("args", {})),
                "tool": "render_custom_visualization",
                # Server-authoritative whitelist for LLM guidance. Reflects the
                # full composition: registered args filtered by author
                # declarations then by the R10 sensitive-name pattern deny-list.
                # Client-side dashboard_state injection consumes this to tell
                # the LLM which /args/* paths are actually patchable.
                "llm_editable_paths": resolve_editable_paths(plugin["source"]),
            }
            for plugin in _get_all_plugins()
        ],
        # Per-source whitelists for every registered Intake plugin. Same
        # composition as client_plugins above; separate field because
        # list_intake_plugins still proxies to Django for the full arg
        # spec, so we surface only the editable-paths information here.
        "intake_plugin_editable_paths": _collect_intake_plugin_whitelists(),
        "mfe": {
            "tool": "render_custom_visualization",
            "description": "Module Federation components are rendered via render_custom_visualization. Use register_runtime_plugin to add new MFE plugins, then render them with render_custom_visualization.",
            "note": "The legacy render_mfe tool is hidden. Use render_custom_visualization instead.",
        },
        "variable_inputs": {
            "tool": "create_variable_input",
            "description": "Create interactive variable inputs (text, number, date, slider, checkbox). Other visualizations reference them with ${variable_name} syntax.",
        },
        "intake_plugins": {
            "tool": "list_intake_plugins",
            "description": "Python backend plugins installed via intake. Call list_intake_plugins to discover available plugins and their args, then use render_plugin to create visualizations.",
        },
    }


# ---------------------------------------------------------------------------
# Slash-command prompt templates (Phase 3a) — discovery + render pair.
#
# Pattern mirrors mcp/nrds_mcps/nextgen_mcp/mcp_server.py (Phases 2a/2b):
#   - Zero-arg prompts for the two list_* tools.
#   - Multi-arg prompts for the two render_* tools, with EVERY surfaced
#     arg typed Annotated[str, Field(...)]. The chatbox-core slash
#     popover synthesizes "[hint]" string tokens; FastMCP would reject
#     those on Dict/int-typed prompt args with PromptError before
#     render. The LLM consuming the rendered template translates the
#     bracket-filled string back to the correct Dict/int when invoking
#     the underlying tool.
#   - Layout dims (w/h) are NOT surfaced — they're documented defaults,
#     not routing decisions. Surfacing them would add bracket tokens
#     for zero error-prevention benefit (see Phase 3a plan R7).
#   - Argument NAMES mirror the underlying tool's exactly (R2 / parity
#     test in test_prompts.py).
#   - Hint copy is drawn from each tool's Field(description=...) with
#     concrete-example values stripped per CLAUDE.md "MCP tool
#     descriptions" rule. LOCKSTEP: when the tool's Field description
#     changes, update the matching prompt arg description here.
# ---------------------------------------------------------------------------


# Python function names carry a ``_prompt_`` prefix so they don't shadow
# the underlying tool functions defined earlier in this file (same name
# binding rule). The ``name=`` arg on ``@mcp.prompt`` is the slash-command
# name the chatbox-core popover surfaces — that name intentionally equals
# the tool name so the slash menu reads "/list_intake_plugins" etc.,
# matching the plan's documented contract (Phase 3a plan R2/R3).


@mcp.prompt(name="list_intake_plugins")
def _prompt_list_intake_plugins() -> str:
    """List installed backend intake plugins.

    Drives the ``list_intake_plugins`` tool. Zero-arg by design.
    FastMCP 3.2.x silently ignores extra kwargs on no-arg prompts
    (test pinned).
    """
    return "List the installed backend intake plugins."


@mcp.prompt(name="list_available_visualizations")
def _prompt_list_available_visualizations() -> str:
    """List all available visualization types.

    Drives the ``list_available_visualizations`` tool. Returns native
    builtins (charts, tables, maps, cards, text, images), registered
    client plugins, and MFE rendering info. Zero-arg.
    """
    return "List all available visualization types."


@mcp.prompt(name="render_plugin")
def _prompt_render_plugin(
    source: Annotated[
        str,
        Field(
            description=(
                "Intake driver name from the 'source' field in "
                "list_intake_plugins results."
            ),
        ),
    ],
    args: Annotated[
        str,
        Field(
            description=(
                "Plugin arguments as a JSON object. Use ${variable_name} "
                "to reference dashboard variable inputs (auto-refreshes "
                "when the variable changes)."
            ),
        ),
    ],
) -> str:
    """Render a backend intake-plugin visualization on the dashboard.

    Drives the ``render_plugin`` tool. ``source`` and ``args`` are the
    truly-required routing args; layout w/h use the tool's documented
    defaults (50/25) and are not surfaced. The ``args`` arg is typed
    ``str`` on the prompt — the LLM translates the rendered string
    into a Dict when calling the underlying tool.
    """
    return (
        f"Render the {source} intake plugin on the dashboard with "
        f"args {args}."
    )


@mcp.prompt(name="render_custom_visualization")
def _prompt_render_custom_visualization(
    source: Annotated[
        str,
        Field(
            description=(
                "Client plugin source name from list_available_visualizations."
            ),
        ),
    ],
) -> str:
    """Render a registered client-side custom visualization on the dashboard.

    Drives the ``render_custom_visualization`` tool. ``source`` is the
    only truly-required routing arg; ``props`` is optional and layout
    w/h use the tool's documented defaults (50/30). None of those are
    surfaced — only the routing arg appears in the slash popover.
    """
    return f"Render the {source} custom visualization on the dashboard."


# ---------------------------------------------------------------------------
# Slash-command prompt templates (Phase 3b) — visualization create + modify
# + plugin registration. Same pattern as Phase 3a:
#   - Python function names carry the `_prompt_` prefix so they don't
#     shadow the underlying tool functions of the same name. The `name=`
#     arg on `@mcp.prompt` is the slash-command name the popover surfaces.
#   - Every surfaced arg is `Annotated[str, Field(...)]` (R9). Even tools
#     with Dict / List args (e.g., patch_visualization.ops) surface as str;
#     the LLM (or the tool's own Union[List, str] coercion) handles the
#     translation at invocation time.
#   - Only TRULY required args are surfaced (R6). Defaultable layout dims
#     (w, h) and optional decorations (title on create_text, description
#     on patch_visualization) are NOT surfaced (R7).
#   - Hint copy drawn from each tool's Field(description=...) with
#     cleanups: drop concrete-example values per CLAUDE.md "MCP tool
#     descriptions" rule. register_runtime_plugin's 4 args and
#     patch_visualization.ops have fresh-authored hint copy (the tool's
#     own Field descriptions contain example violations or insufficient
#     cross-arg references for R5 lockstep).
# ---------------------------------------------------------------------------


@mcp.prompt(name="create_plotly_chart")
def _prompt_create_plotly_chart(
    data: Annotated[
        str,
        Field(
            description=(
                "Array of Plotly trace objects (each with non-empty x and y "
                "arrays). At least one trace required."
            ),
        ),
    ],
) -> str:
    """Create a NEW Plotly chart tile on the dashboard.

    Drives the create_plotly_chart tool. Layout / config / title use the
    tool's documented defaults and are not surfaced (R7).
    """
    return f"Create a Plotly chart tile on the dashboard with traces: {data}"


@mcp.prompt(name="create_data_table")
def _prompt_create_data_table(
    data: Annotated[
        str,
        Field(
            description=(
                "Array of row objects sharing the same keys. At least one row "
                "required."
            ),
        ),
    ],
) -> str:
    """Create a NEW data-table tile on the dashboard.

    Drives the create_data_table tool. Title / subtitle / layout use the
    tool's documented defaults and are not surfaced (R7).
    """
    return f"Create a data table tile on the dashboard with rows: {data}"


@mcp.prompt(name="create_card")
def _prompt_create_card(
    title: Annotated[
        str,
        Field(description="Title shown at the top of the card."),
    ],
) -> str:
    """Create a NEW stat-card tile on the dashboard.

    Drives the create_card tool. Description / data / layout are optional
    on the tool and not surfaced on the prompt (R7); the user adds them
    in prose if needed.
    """
    return f"Create a card tile on the dashboard with title: {title}"


@mcp.prompt(name="create_text")
def _prompt_create_text(
    text: Annotated[
        str,
        Field(description="Text content to display in the tile (non-empty)."),
    ],
) -> str:
    """Create a NEW text tile on the dashboard.

    Drives the create_text tool. Layout uses the tool's documented
    defaults and is not surfaced (R7).
    """
    return f"Create a text tile on the dashboard with content: {text}"


@mcp.prompt(name="create_custom_image")
def _prompt_create_custom_image(
    image_url: Annotated[
        str,
        Field(
            description=(
                "URL of the image to display (http/https URL, data URI, or "
                "S3 path)."
            ),
        ),
    ],
) -> str:
    """Create a NEW custom-image tile on the dashboard.

    Drives the create_custom_image tool. Alt text / layout use the tool's
    documented defaults and are not surfaced (R7).
    """
    return f"Create a custom image tile on the dashboard with url: {image_url}"


@mcp.prompt(name="create_map_visualization")
def _prompt_create_map_visualization() -> str:
    """Create a NEW map visualization tile on the dashboard.

    Drives the create_map_visualization tool. Zero-arg by design — the
    tool has no required arguments (base map, center, zoom, markers, and
    layers all default sensibly). The user adds layers and config via
    follow-up prose or by combining with add_*_layer tools.
    """
    return "Create a map visualization tile on the dashboard."


@mcp.prompt(name="create_variable_input")
def _prompt_create_variable_input(
    variable_name: Annotated[
        str,
        Field(
            description=(
                "Snake_case identifier other visualizations will reference "
                "via ${variable_name}. Preserve the user's exact name."
            ),
        ),
    ],
) -> str:
    """Create a NEW variable-input tile on the dashboard.

    Drives the create_variable_input tool. Variable type defaults to
    'text'; the user requests a different type in prose if needed
    (R7 — type is not surfaced).
    """
    return f"Create a variable input tile on the dashboard with name: {variable_name}"


@mcp.prompt(name="register_runtime_plugin")
def _prompt_register_runtime_plugin(
    url: Annotated[
        str,
        Field(description="Full URL to the plugin's remoteEntry.js manifest."),
    ],
    scope: Annotated[
        str,
        Field(description="Module Federation scope name registered by the build."),
    ],
    module: Annotated[
        str,
        Field(
            description=(
                "Exposed module path within the federation, "
                "starting with a relative-path prefix."
            ),
        ),
    ],
    label: Annotated[
        str,
        Field(
            description=(
                "Human-readable display name for the plugin in the "
                "visualization picker."
            ),
        ),
    ],
) -> str:
    """Register a runtime Module Federation plugin.

    Drives the register_runtime_plugin tool. All 4 surfaced args
    (url, scope, module, label) are routing decisions with no defaults
    and are required per R6. Remote type / description / group /
    data_key are optional and not surfaced (R7).
    """
    return (
        f"Register the runtime plugin {label} (scope {scope}, module "
        f"{module}) from {url}."
    )


@mcp.prompt(name="configure_popup_modal_layer")
def _prompt_configure_popup_modal_layer(
    map_uuid: Annotated[
        str,
        Field(
            description=(
                "UUID of the target map visualization (from dashboard_state "
                "or the return of create_map_visualization)."
            ),
        ),
    ],
    layer_index: Annotated[
        str,
        Field(
            description=(
                "0-based index into the map's layers array, as a string "
                "(FastMCP prompt args are string-typed). Read the index from "
                "dashboard_state."
            ),
        ),
    ],
    popup_config: Annotated[
        str,
        Field(
            description=(
                "Popup modal payload as a JSON object string. Shape: "
                "{mode: 'modal', position?: {leftPct, topPct, widthPct, "
                "heightPct}, titleTemplate?: str, gridItems: [{source, "
                "args, x, y, w, h, metadata?}, ...]}. The tool accepts "
                "both Dict and JSON-string Dict; pass either."
            ),
        ),
    ],
) -> str:
    """Scaffold a configure_popup_modal_layer tool call.

    Drives the ``configure_popup_modal_layer`` tool. Use after the layer
    was added by an ``add_*_layer`` tool — a same-turn add does not yet
    appear in ``dashboard_state``, so ``layer_index`` would be stale.

    Template strings inside ``popup_config`` (``titleTemplate`` and any
    string value inside a gridItem's ``args``) may embed
    ``${feature.<key>}`` tokens that substitute against the clicked
    feature's attributes at popup-render time; missing keys resolve to
    empty string.
    """
    return (
        f"Configure a custom popup modal for the layer at index "
        f"{layer_index} on map {map_uuid}. Apply the popup_config payload: "
        f"{popup_config}."
    )


@mcp.prompt(name="patch_visualization")
def _prompt_patch_visualization(
    uuid: Annotated[
        str,
        Field(
            description=(
                "UUID of the target visualization tile (from dashboard_state)."
            ),
        ),
    ],
    source: Annotated[
        str,
        Field(
            description=(
                "Registry source name of the target visualization "
                "(e.g., the source returned alongside the uuid)."
            ),
        ),
    ],
    ops: Annotated[
        str,
        Field(
            description=(
                "Operations to apply to the visualization identified by "
                "uuid. RFC 6902-style array as JSON: each op is "
                "{op, path, value} with op in "
                "{add, replace, remove, move, test}. Tool accepts both "
                "array and JSON-string shapes."
            ),
        ),
    ],
) -> str:
    """Patch an existing visualization tile with RFC 6902-style operations.

    Drives the patch_visualization tool. The ops arg is typed str on the
    prompt (R9); the tool's Union[List, str] signature with server-side
    json.loads coercion accepts either the LLM-translated list or the
    raw JSON string.
    """
    return (
        f"Patch visualization {uuid} (source {source}) "
        f"with operations: {ops}"
    )


# ---------------------------------------------------------------------------
# Slash-command prompt templates (Phase 3c) — layer-add family.
#
# 12 prompts, one per add_*_layer tool. All surface map_uuid + name plus
# layer-type-specific args (url, wms_layers, layer_id, projection, etc.).
# All args typed Annotated[str, Field(...)] per R9.
#
# add_dynamic_map_layer is architecturally distinct from the other 11:
# its `source` arg is an intake-driver source name discovered via
# list_intake_plugins (structurally identical to render_plugin.source
# in Phase 3a), NOT a URL. Its `source` arg description MUST reference
# list_intake_plugins by name — the R5 lockstep test
# `test_add_dynamic_map_layer_source_arg_references_list_intake_plugins`
# enforces this.
#
# Phase 3c probe (commit a739750) removed BM25SearchTransform, so all
# 26 tools are visible to chatbox-core's embedding ranker. No
# always_visible pinning is needed for any of these layer prompts.
# ---------------------------------------------------------------------------


@mcp.prompt(name="add_wms_layer")
def _prompt_add_wms_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the WMS layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(description="WMS service URL (GetCapabilities endpoint)."),
    ],
    wms_layers: Annotated[
        str,
        Field(
            description=(
                "WMS LAYERS parameter value in workspace:layer format. "
                "Comma-separated for multiple layers."
            ),
        ),
    ],
) -> str:
    """Add a WMS map layer to an existing map tile.

    Drives the add_wms_layer tool. Required surfaced args (R6):
    map_uuid, name, url, wms_layers.

    Common optional args the LLM should consider when the user
    requests them (set via the tool's `params` arg or per-layer
    styling — not surfaced as prompt brackets per R7):
      - params: WMS GetMap parameters merged into the request
        (STYLES, TIME, FORMAT, TRANSPARENT). Supports ${variable_name}
        substitution for dashboard variable inputs.
      - queryable: enable click-to-query on this layer.
      - attribute_variables: map feature attributes to dashboard
        variable names.
      - opacity (0-1), min_zoom, max_zoom, visible, legend, style.
    """
    return (
        f"Add a WMS layer named '{name}' to map {map_uuid} from "
        f"{url} (layers: {wms_layers}). "
        f"If the user requests temporal filtering, custom styles, or "
        f"a non-default image format, set the tool's `params` arg "
        f"(e.g., TIME, STYLES, FORMAT, TRANSPARENT)."
    )


@mcp.prompt(name="add_esri_image_layer")
def _prompt_add_esri_image_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the ESRI image layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(description="ArcGIS REST service URL (Image or Map Service)."),
    ],
) -> str:
    """Add an ESRI Image / Map Service layer to an existing map tile.

    Drives the add_esri_image_layer tool. Required surfaced args (R6):
    map_uuid, name, url.

    Common optional args the LLM should consider:
      - layer_id: integer or comma-separated layer indices to show
        from the service (canonicalized internally to `LAYERS=show:...`).
      - params: additional ESRI source parameters merged into the
        request (LAYERS, TIME, LAYERDEFS, mosaicRule). Supports
        ${variable_name} substitution.
      - queryable, attribute_variables, opacity, min_zoom, max_zoom,
        visible, legend, style.
    """
    return (
        f"Add an ESRI image layer named '{name}' to map {map_uuid} "
        f"from {url}. "
        f"If the user names specific service layers or requests temporal "
        f"filtering / definition queries, set the tool's `layer_id` or "
        f"`params` arg (LAYERS, TIME, LAYERDEFS, mosaicRule)."
    )


@mcp.prompt(name="add_esri_feature_layer")
def _prompt_add_esri_feature_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the ESRI feature layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(description="ArcGIS Feature Service URL."),
    ],
    layer_id: Annotated[
        str,
        Field(
            description=(
                "Integer layer index within the service (as a string; cast "
                "to int by the tool internally)."
            ),
        ),
    ],
) -> str:
    """Add an ESRI Feature Service layer to an existing map tile.

    Drives the add_esri_feature_layer tool. Required surfaced args
    (R6): map_uuid, name, url, layer_id.

    Common optional args the LLM should consider:
      - params: feature-query parameters merged into the request
        (TIME for temporal, WHERE for SQL filtering). Supports
        ${variable_name} substitution.
      - queryable: enable feature attribute popups on click.
      - attribute_variables: map feature attributes to dashboard
        variable names — published on click.
      - opacity, min_zoom, max_zoom, visible, legend, style.
    """
    return (
        f"Add an ESRI feature layer named '{name}' to map {map_uuid} "
        f"from {url} (layer index {layer_id}). "
        f"If the user requests temporal filtering, SQL-style WHERE "
        f"clauses, or click-to-query behavior, set the tool's `params` "
        f"(TIME, WHERE), `queryable`, or `attribute_variables` args."
    )


@mcp.prompt(name="add_geojson_layer")
def _prompt_add_geojson_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the GeoJSON layer in the layer control."),
    ],
) -> str:
    """Add a GeoJSON layer to an existing map tile.

    Drives the add_geojson_layer tool. Required surfaced args (R6):
    map_uuid, name. The user MUST supply one of `geojson` (inline
    FeatureCollection / Feature) or `geojson_url` (URL fetched at
    render time) — these are optional individually on the tool but
    mutually-required (one or the other).

    Common optional args the LLM should consider:
      - geojson OR geojson_url: pick one based on whether the user
        gave inline data or a URL.
      - queryable, attribute_variables for click-to-query.
      - opacity, min_zoom, max_zoom, visible, legend, style.
    """
    return (
        f"Add a GeoJSON layer named '{name}' to map {map_uuid}. "
        f"Supply either the tool's `geojson` arg (inline "
        f"FeatureCollection or Feature) or `geojson_url` (URL of a "
        f"GeoJSON file fetched at render time) — exactly one is "
        f"required."
    )


@mcp.prompt(name="add_kml_layer")
def _prompt_add_kml_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the KML layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(description="KML resource URL."),
    ],
) -> str:
    """Add a KML layer to an existing map tile.

    Drives the add_kml_layer tool. Required surfaced args (R6):
    map_uuid, name, url.

    Common optional args: queryable, attribute_variables for
    click-to-query; opacity, min_zoom, max_zoom, visible, legend,
    style for layer presentation.
    """
    return (
        f"Add a KML layer named '{name}' to map {map_uuid} from {url}. "
        f"If the user requests custom styling, click-to-query, or "
        f"zoom-range visibility, set the corresponding tool args "
        f"(style, queryable, min_zoom, max_zoom)."
    )


@mcp.prompt(name="add_image_tile_layer")
def _prompt_add_image_tile_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the image-tile layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(
            description=(
                "Image tile URL template with the standard {x}, {y}, {z} "
                "tile-coordinate placeholders."
            ),
        ),
    ],
) -> str:
    """Add an image-tile (raster XYZ) layer to an existing map tile.

    Drives the add_image_tile_layer tool. Required surfaced args (R6):
    map_uuid, name, url.

    Common optional args: opacity, min_zoom, max_zoom, visible,
    legend, style; queryable + attribute_variables for click-to-query.
    """
    return (
        f"Add an image-tile layer named '{name}' to map {map_uuid} "
        f"from {url}. "
        f"If the user requests opacity, zoom-range visibility, "
        f"attribution, or a custom legend, set the corresponding tool "
        f"args (opacity, min_zoom, max_zoom, legend)."
    )


@mcp.prompt(name="add_vector_tile_layer")
def _prompt_add_vector_tile_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the vector-tile layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(
            description=(
                "Vector tile URL template with {x}, {y} (or {-y}), {z} "
                "tile-coordinate placeholders. Comma-separated for "
                "multiple template URLs."
            ),
        ),
    ],
) -> str:
    """Add a vector-tile layer to an existing map tile.

    Drives the add_vector_tile_layer tool. Required surfaced args
    (R6): map_uuid, name, url.

    Common optional args: style (critical for vector tiles — controls
    fill/stroke/text rendering), queryable, attribute_variables,
    opacity, min_zoom, max_zoom, visible, legend.
    """
    return (
        f"Add a vector-tile layer named '{name}' to map {map_uuid} "
        f"from {url}. "
        f"Vector tiles typically need a custom `style` arg to control "
        f"fill/stroke/text rendering — set it if the user supplies one "
        f"or describes the desired styling."
    )


@mcp.prompt(name="add_pmtiles_vector_layer")
def _prompt_add_pmtiles_vector_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the PMTiles vector layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(description="PMTiles archive URL (vector tiles)."),
    ],
) -> str:
    """Add a PMTiles vector layer to an existing map tile.

    Drives the add_pmtiles_vector_layer tool. Required surfaced args
    (R6): map_uuid, name, url.

    Common optional args: style (controls fill/stroke/text rendering
    for vector features), queryable, attribute_variables, opacity,
    min_zoom, max_zoom, visible, legend.
    """
    return (
        f"Add a PMTiles vector layer named '{name}' to map {map_uuid} "
        f"from {url}. "
        f"Vector layers typically need a custom `style` arg for "
        f"rendering — set it if the user supplies one or describes "
        f"the desired styling."
    )


@mcp.prompt(name="add_pmtiles_raster_layer")
def _prompt_add_pmtiles_raster_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the PMTiles raster layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(description="PMTiles archive URL (raster tiles)."),
    ],
) -> str:
    """Add a PMTiles raster layer to an existing map tile.

    Drives the add_pmtiles_raster_layer tool. Required surfaced args
    (R6): map_uuid, name, url.

    Common optional args: opacity, min_zoom, max_zoom, visible,
    legend; queryable + attribute_variables for click-to-query.
    """
    return (
        f"Add a PMTiles raster layer named '{name}' to map {map_uuid} "
        f"from {url}. "
        f"If the user requests opacity, zoom-range visibility, or a "
        f"custom legend, set the corresponding tool args."
    )


@mcp.prompt(name="add_geotiff_layer")
def _prompt_add_geotiff_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the GeoTIFF layer in the layer control."),
    ],
) -> str:
    """Add a GeoTIFF layer (Cloud Optimized GeoTIFF) to an existing map tile.

    Drives the add_geotiff_layer tool. Required surfaced args (R6):
    map_uuid, name.

    GeoTIFF rendering has the richest optional-arg surface of any
    layer type — the LLM should set these based on what the user
    requests:
      - url: COG URL (single-source). Required unless `source_props`
        supplies multi-source `sources` array.
      - bands: comma-separated band indices to render (single channel,
        RGB triplet, or false-color combinations).
      - nodata: sentinel value for transparent pixels.
      - min, max: numeric range for color scaling.
      - ramp_name: registered color ramp (e.g., 'viridis', 'magma',
        'terrain'). Used when legend='default'.
      - ramp_min, ramp_max: explicit ramp range for the auto-legend.
      - source_props: advanced — pass {'sources': [{'url': ...}]}
        for multi-source GeoTIFFs or to override flat fields.

    Plus the standard styling args (queryable, attribute_variables,
    opacity, min_zoom, max_zoom, visible, legend, style).
    """
    return (
        f"Add a GeoTIFF layer named '{name}' to map {map_uuid}. "
        f"Configure rendering via the tool's args: `url` (COG URL), "
        f"`bands` (e.g., comma-separated indices for single-channel or "
        f"RGB), `nodata` (sentinel), `min`/`max` (color scaling), and "
        f"`ramp_name` for the color ramp."
    )


@mcp.prompt(name="add_static_image_layer")
def _prompt_add_static_image_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the static-image layer in the layer control."),
    ],
    url: Annotated[
        str,
        Field(description="Image resource URL."),
    ],
    projection: Annotated[
        str,
        Field(description="EPSG code identifying the projection of the bounding box."),
    ],
    image_extent: Annotated[
        str,
        Field(
            description=(
                "Bounding box as a comma-separated string "
                "'minX,minY,maxX,maxY' in the projection's coordinate "
                "system."
            ),
        ),
    ],
) -> str:
    """Add a static-image (non-georeferenced raster pinned to an extent) layer to a map tile.

    Drives the add_static_image_layer tool. Required surfaced args
    (R6): map_uuid, name, url, projection, image_extent.

    Common optional args: opacity, min_zoom, max_zoom, visible,
    legend, style; source_props for advanced source-level overrides
    (e.g., attribution).
    """
    return (
        f"Add a static-image layer named '{name}' to map {map_uuid} "
        f"from {url}, projected as {projection} with extent "
        f"{image_extent}. "
        f"If the user requests opacity, zoom-range visibility, or an "
        f"attribution string, set the corresponding tool args."
    )


@mcp.prompt(name="add_dynamic_map_layer")
def _prompt_add_dynamic_map_layer(
    map_uuid: Annotated[
        str,
        Field(description="UUID of the existing map tile (from create_map_visualization)."),
    ],
    source: Annotated[
        str,
        Field(
            description=(
                "Intake plugin source name (from list_intake_plugins "
                "results). Must resolve to a plugin with type=='map_layer' "
                "and dynamic_map_layer=True."
            ),
        ),
    ],
    name: Annotated[
        str,
        Field(description="Display name for the dynamic map layer in the layer control."),
    ],
) -> str:
    """Add a dynamically-generated map layer from a backend intake plugin to a map tile.

    Drives the add_dynamic_map_layer tool. Required surfaced args
    (R6): map_uuid, source, name. Architecturally distinct from the
    other 11 layer prompts: `source` is an intake-driver source name
    (discoverable via `list_intake_plugins`), NOT a URL.

    Common optional args the LLM should set when the user supplies
    plugin-specific values:
      - args: dict of plugin args passed to fetch_features at render
        time. Supports ${variable_name} syntax for dashboard variable
        substitution — these are preserved verbatim and re-evaluated
        on every variable change.
      - queryable, attribute_variables, opacity, min_zoom, max_zoom,
        visible, legend, style.
    """
    return (
        f"Add a dynamic map layer named '{name}' from intake plugin "
        f"'{source}' to map {map_uuid}. "
        f"If the plugin needs non-default arguments, set the tool's "
        f"`args` dict; use ${{variable_name}} syntax to reference "
        f"dashboard variable inputs that auto-refresh the layer."
    )


# ---------------------------------------------------------------------------
# Logging + Entry Point
# ---------------------------------------------------------------------------

class _SuppressFastMCPValidationTraceback(logging.Filter):
    """Drop the FastMCP `logger.exception("Error validating tool ...")`
    record at `fastmcp/server/server.py:1167`.

    The verbose Pydantic Rich-formatted traceback was the dominant noise
    source when validation errors fired (~50 lines per rejected call).
    `InputValidationEnvelopeMiddleware` already converts the same error
    into a structured tool-result envelope; the traceback adds nothing
    actionable beyond what the envelope and the observability log line
    already capture. Drop it.

    Surgical filter over the message text rather than bumping the
    logger's level — keeps other genuine errors from this logger
    (`Error calling tool ...` for non-validation failures) visible.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return not record.getMessage().startswith("Error validating tool")


class _SuppressUvicornAccessNoise(logging.Filter):
    """Drop CORS-preflight and Streamable-HTTP-keepalive lines from
    `uvicorn.access`.

    The MCP transport produces a steady stream of OPTIONS preflights
    (CORS) and GET /mcp keepalive polls. They almost never indicate a
    problem and they drown out the POST /mcp lines that carry actual
    tool invocations. Filter both. Leave 4xx/5xx lines visible — those
    DO indicate problems.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        msg = record.getMessage()
        # uvicorn.access format: '<host>:<port> - "<METHOD> <path> HTTP/x" <code>'
        if " 4" in msg or " 5" in msg:
            return True  # let 4xx/5xx through regardless of method
        if '"OPTIONS ' in msg:
            return False
        if '"GET /mcp ' in msg or '"GET /sse ' in msg:
            return False
        return True


def _configure_logging():
    level = os.getenv("TETHYSDASH_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    # Suppress the FastMCP validation-error Rich traceback — the
    # observability middleware + envelope middleware already capture
    # the actionable signal.
    logging.getLogger("fastmcp.server.server").addFilter(
        _SuppressFastMCPValidationTraceback()
    )
    # Dampen uvicorn HTTP-access noise. Set TETHYSDASH_VERBOSE_ACCESS=1
    # to keep all access lines (useful when debugging transport issues).
    if os.getenv("TETHYSDASH_VERBOSE_ACCESS", "").lower() not in ("1", "true", "yes"):
        logging.getLogger("uvicorn.access").addFilter(_SuppressUvicornAccessNoise())


if __name__ == "__main__":
    _configure_logging()
    port = int(os.getenv("MCP_PORT", "9001"))
    # R13: default to loopback binding (CLAUDE.md says the MCP server "must
    # run localhost-bound or behind an authenticated reverse proxy"). The
    # MCP_HOST override exists for production deploys that wrap this server
    # behind such a proxy.
    host = os.getenv("MCP_HOST", "127.0.0.1")
    # R5/R6: default to Streamable HTTP at /mcp (FastMCP's default
    # streamable_http_path). MCP_TRANSPORT=sse opts back into the legacy
    # SSE transport during the migration window for users with /sse
    # localStorage URLs (see plan Open Questions → SSE compat path).
    transport = os.getenv("MCP_TRANSPORT", "streamable-http")
    if transport == "sse":
        # K3: SSE compat path — invoke the (R12-corrected) monkey-patch only
        # when SSE is actually selected. On the streamable-http default path,
        # the patch is never applied; CORS_MIDDLEWARE handles preflight.
        _patch_sse_transport_for_cors()
        endpoint = f"http://{host}:{port}/sse"
    else:
        endpoint = f"http://{host}:{port}/mcp"
    LOGGER.info(
        "Starting TethysDash MCP Server on %s:%d with %s transport at %s",
        host, port, transport, endpoint,
    )
    mcp.run(
        transport=transport,
        host=host,
        port=port,
        middleware=CORS_MIDDLEWARE,
    )
