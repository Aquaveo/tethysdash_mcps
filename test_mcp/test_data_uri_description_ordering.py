"""Regression guard for the ``data_uri``-preferred framing in the
``data`` field descriptions on the inline-data create tools.

Debug session 2026-05-19: nemotron-3-nano-30b reliably produced JSON
parse errors at the ~1KB threshold when inlining a 24-record NRDS plot
(symptom: ``data is not valid JSON: Extra data: line 1 column 1275``,
~200s of repair-loop oscillation before the user cancelled). The fix
shipped a chatbox-core-side cap that short-circuits inline ``data``
over 20 records with an actionable error pointing at ``data_uri``.

This test pins the companion server-side change: the ``data`` field
description on ``create_plotly_chart`` / ``create_data_table`` /
``create_card`` leads with ``data_uri`` as the PREFERRED path so the
LLM picks the cache-URI route on the first try (avoiding the +1
round-trip cost of triggering the chatbox-core cap on the sad path).

We assert ordering — the word ``data_uri`` must appear BEFORE the
inline-shape vocabulary (``Array of`` / ``list of stat entries`` /
``array of row objects``). Small models pattern-match on the first
option in a field description; if a future edit moves the inline shape
ahead of the ``data_uri`` framing, the small-model failure mode silently
returns. This test catches that regression.
"""

import asyncio

import pytest
from fastmcp import Client

from tethysdash_mcp.mcp_server import mcp


# Tools whose ``data`` field is paired with a ``data_uri`` companion.
# These are the tools chatbox-core's INLINE_LIST_MAX_RECORDS cap also
# covers; the description must lead the LLM toward the URI path so the
# cap rarely fires.
TOOLS_REQUIRING_DATA_URI_PREFERENCE = (
    "create_plotly_chart",
    "create_data_table",
    "create_card",
)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


@pytest.fixture(scope="module")
def tools_by_name():
    async def _collect():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            return {t.name: t for t in tools}

    return _run(_collect())


def _data_field_description(tool, tool_name: str) -> str:
    """Return the description of the ``data`` field from the tool's input
    schema. Surfaces a useful error if the field is absent.
    """
    schema = tool.inputSchema or {}
    props = schema.get("properties", {}) or {}
    data_prop = props.get("data")
    assert data_prop is not None, (
        f"{tool_name}: expected a `data` field in inputSchema.properties"
    )
    desc = data_prop.get("description", "")
    assert isinstance(desc, str) and desc, (
        f"{tool_name}: `data` field has no description"
    )
    return desc


@pytest.mark.parametrize("tool_name", TOOLS_REQUIRING_DATA_URI_PREFERENCE)
def test_data_description_names_data_uri(tool_name: str, tools_by_name):
    """The `data` description must name `data_uri` as the preferred path."""
    tool = tools_by_name.get(tool_name)
    assert tool is not None, f"tool `{tool_name}` not registered"
    desc = _data_field_description(tool, tool_name)
    assert "data_uri" in desc, (
        f"{tool_name}: `data` description must name `data_uri` so the LLM "
        f"knows the URI-path alternative exists. Current description:\n{desc}"
    )


@pytest.mark.parametrize("tool_name", TOOLS_REQUIRING_DATA_URI_PREFERENCE)
def test_data_description_names_cache_uri_source(tool_name: str, tools_by_name):
    """The description must point at `_cache_uri` so the LLM knows where
    the URI comes from (the source tool's result envelope)."""
    tool = tools_by_name.get(tool_name)
    assert tool is not None
    desc = _data_field_description(tool, tool_name)
    assert "_cache_uri" in desc, (
        f"{tool_name}: `data` description must mention `_cache_uri` (the "
        f"field auto-injected on prior tool results) so the LLM knows "
        f"where to source the URI value. Current description:\n{desc}"
    )


@pytest.mark.parametrize("tool_name", TOOLS_REQUIRING_DATA_URI_PREFERENCE)
def test_data_uri_mentioned_before_inline_shape(tool_name: str, tools_by_name):
    """Position matters: the LLM's first-option-wins bias on field
    descriptions means `data_uri` must appear before the inline-shape
    vocabulary, or small models will pick inline by default.
    """
    tool = tools_by_name.get(tool_name)
    assert tool is not None
    desc = _data_field_description(tool, tool_name)

    # Find the first mention of `data_uri`.
    data_uri_idx = desc.find("data_uri")
    assert data_uri_idx >= 0  # already asserted in the test above

    # Find the first mention of any inline-shape vocabulary token. The
    # exact wording varies per tool, so we check for any of these markers.
    inline_markers = ("Inline shape", "Inline data")
    inline_indices = [
        desc.find(marker) for marker in inline_markers if desc.find(marker) >= 0
    ]
    assert inline_indices, (
        f"{tool_name}: description should carry an explicit `Inline shape` "
        f"or `Inline data` section header so the URI vs inline framing is "
        f"separable. Current description:\n{desc}"
    )
    first_inline_idx = min(inline_indices)

    assert data_uri_idx < first_inline_idx, (
        f"{tool_name}: `data_uri` mention (idx={data_uri_idx}) must appear "
        f"BEFORE the inline-shape section (idx={first_inline_idx}). Small "
        f"models follow the first option listed; if inline leads, they "
        f"will inline. Current description:\n{desc}"
    )


@pytest.mark.parametrize("tool_name", TOOLS_REQUIRING_DATA_URI_PREFERENCE)
def test_data_description_names_small_model_failure_mode(
    tool_name: str, tools_by_name
):
    """The description must explicitly name the small-model failure mode
    so the LLM has a concrete reason to prefer `data_uri` — abstract
    "preferred" framing alone isn't enough for nemotron-class models.
    """
    tool = tools_by_name.get(tool_name)
    assert tool is not None
    desc = _data_field_description(tool, tool_name)
    assert "small model" in desc.lower() or "parse error" in desc.lower(), (
        f"{tool_name}: description must name the small-model failure mode "
        f"(e.g., 'parse errors on small models') so the LLM has a concrete "
        f"reason to prefer the URI path. Current description:\n{desc}"
    )
