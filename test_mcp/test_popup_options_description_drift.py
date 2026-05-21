"""Drift guard for popup_options field descriptions across add_*_layer tools.

Debug session 2026-05-21: gemini-flash misrouted a "alias the comid
attribute to River ID" prompt to `attribute_variables` instead of
`popup_options.aliases` on `add_esri_image_layer`. Root cause was
asymmetric description weight — `attribute_variables` had a 35-word
description with "attribute" repeated 3 times; `popup_options` had a
3-word stub ("Click-popup options") with no mention of aliases or
omit. The LLM picked the field whose description carried the relevant
domain idiom.

`add_wms_layer` had the full description ("aliases: {layer_name: {field:
alias}}", "omit: {layer_name: [field, ...]}"); the 10 sibling layer-add
tools all got the stub. This test pins the wording so future edits
can't silently drop it again.

Tools that need the clause: every add_*_layer tool that accepts a
`popup_options` parameter. (add_dynamic_map_layer does not — it uses
the plugin's own popup mechanism — so it is intentionally excluded.)
"""

import asyncio

import pytest

from tethysdash_mcp.mcp_server import mcp


# Every add_*_layer tool that accepts a popup_options parameter. The
# description on each must name both the `aliases` and `omit` keys so
# the LLM can route "alias the X attribute to Y" or "hide field X" prompts
# to popup_options instead of an adjacent field like attribute_variables.
TOOLS_WITH_POPUP_OPTIONS = (
    "add_wms_layer",
    "add_esri_image_layer",
    "add_esri_feature_layer",
    "add_geojson_layer",
    "add_kml_layer",
    "add_image_tile_layer",
    "add_vector_tile_layer",
    "add_pmtiles_vector_layer",
    "add_pmtiles_raster_layer",
    "add_geotiff_layer",
    "add_static_image_layer",
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(scope="module")
def tool_input_schemas():
    """Map of tool name -> JSON schema for tool inputs (from MCP catalog)."""
    async def go():
        return await mcp._local_provider.list_tools()

    tools = _run(go())
    return {t.name: t.parameters for t in tools}


@pytest.mark.parametrize("tool_name", TOOLS_WITH_POPUP_OPTIONS)
def test_popup_options_description_names_aliases_keyword(
    tool_input_schemas: dict, tool_name: str
):
    """popup_options description must name `aliases` so LLMs route table-column
    renames here, not to attribute_variables or another adjacent field.
    """
    schema = tool_input_schemas.get(tool_name)
    assert schema is not None, f"tool {tool_name!r} not in catalog"
    props = schema.get("properties", {})
    assert "popup_options" in props, (
        f"tool {tool_name!r} missing popup_options field — update "
        f"TOOLS_WITH_POPUP_OPTIONS if popup_options was intentionally dropped"
    )
    description = props["popup_options"].get("description", "")
    assert "aliases" in description, (
        f"tool {tool_name!r} popup_options description doesn't name 'aliases'. "
        f"Current: {description!r}. Add the aliases keyword so LLMs (especially "
        f"small models like gemini-flash) can route 'alias attribute X to Y' "
        f"prompts here instead of misrouting to attribute_variables."
    )


@pytest.mark.parametrize("tool_name", TOOLS_WITH_POPUP_OPTIONS)
def test_popup_options_description_names_omit_keyword(
    tool_input_schemas: dict, tool_name: str
):
    """popup_options description must also name `omit` for symmetric routing."""
    schema = tool_input_schemas[tool_name]
    description = schema["properties"]["popup_options"].get("description", "")
    assert "omit" in description, (
        f"tool {tool_name!r} popup_options description doesn't name 'omit'. "
        f"Current: {description!r}."
    )
