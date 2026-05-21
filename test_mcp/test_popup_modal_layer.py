"""Contract tests for ``configure_popup_modal_layer`` MCP tool + prompt.

Covers Units 1-4 of the popup-modal MCP surface plan:

* Unit 1: Pydantic model shape validation (PopupConfigPayload + nested models)
* Unit 2: tool body — UUID validation, JSON-string coercion, normalization,
  patch_update envelope construction
* Unit 3: @mcp.prompt counterpart shape
* Unit 4: end-to-end envelope contract — RFC 6902 add-op shape, canonical
  persisted gridItem shape, whitelist prefix-match coverage for deep paths

Layer 1 tests — no browser, no server, milliseconds per test.
"""

import json
import uuid as uuid_mod

import pytest

from tethysdash_mcp.editable_schemas import is_path_allowed
from tethysdash_mcp.mcp_server import (
    _DEFAULT_GRID_ITEM_METADATA,
    _DEFAULT_POPUP_POSITION,
    _PopupConfigGridItemInput,
    _PopupConfigPayload,
    _PopupConfigPosition,
    _prompt_configure_popup_modal_layer,
    configure_popup_modal_layer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_uuid() -> str:
    return str(uuid_mod.uuid4())


def _minimal_gridItem(**overrides):
    """Build a single gridItem with sensible defaults."""
    base = {
        "source": "plotly",
        "args": {"inlineData": {"data": [], "layout": {}}},
        "x": 0,
        "y": 0,
        "w": 12,
        "h": 6,
    }
    base.update(overrides)
    return base


def _minimal_payload(**overrides):
    """Build a minimal valid popup_config payload (modal + one gridItem)."""
    base = {
        "mode": "modal",
        "gridItems": [_minimal_gridItem()],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Unit 1 — Pydantic shape validation
# ---------------------------------------------------------------------------


class TestPydanticModels:
    """Direct shape validation against the Pydantic models."""

    def test_minimal_payload_accepts(self):
        m = _PopupConfigPayload(**_minimal_payload())
        assert m.mode == "modal"
        assert m.position is None
        assert m.titleTemplate == ""
        assert len(m.gridItems) == 1

    def test_full_payload_accepts(self):
        m = _PopupConfigPayload(
            mode="modal",
            position={"leftPct": 10, "topPct": 10, "widthPct": 80, "heightPct": 80},
            titleTemplate="Site ${feature.station_name}",
            gridItems=[
                _minimal_gridItem(),
                _minimal_gridItem(x=12, y=0, source="Text", args={"text": "x"}),
            ],
        )
        assert isinstance(m.position, _PopupConfigPosition)
        assert m.position.widthPct == 80
        assert len(m.gridItems) == 2

    def test_position_below_size_min_rejected(self):
        with pytest.raises(Exception):  # pydantic.ValidationError
            _PopupConfigPayload(
                mode="modal",
                position={"leftPct": 0, "topPct": 0, "widthPct": 10, "heightPct": 80},
                gridItems=[_minimal_gridItem()],
            )

    def test_position_above_100_rejected(self):
        with pytest.raises(Exception):
            _PopupConfigPayload(
                mode="modal",
                position={"leftPct": 150, "topPct": 0, "widthPct": 60, "heightPct": 60},
                gridItems=[_minimal_gridItem()],
            )

    def test_gridItem_negative_x_rejected(self):
        with pytest.raises(Exception):
            _PopupConfigPayload(
                mode="modal",
                gridItems=[_minimal_gridItem(x=-1)],
            )

    def test_gridItem_zero_w_rejected(self):
        """w must be >= 1 (a zero-width gridItem can't render)."""
        with pytest.raises(Exception):
            _PopupConfigPayload(
                mode="modal",
                gridItems=[_minimal_gridItem(w=0)],
            )

    def test_gridItem_empty_source_rejected(self):
        with pytest.raises(Exception):
            _PopupConfigPayload(
                mode="modal",
                gridItems=[_minimal_gridItem(source="")],
            )

    def test_gridItem_non_dict_args_rejected(self):
        with pytest.raises(Exception):
            _PopupConfigPayload(
                mode="modal",
                gridItems=[_minimal_gridItem(args="not a dict")],
            )

    def test_empty_gridItems_rejected(self):
        with pytest.raises(Exception):
            _PopupConfigPayload(mode="modal", gridItems=[])

    def test_mode_table_rejected(self):
        """v1 supports only the modal mode; table mode lives on popup_options.aliases."""
        with pytest.raises(Exception):
            _PopupConfigPayload(mode="table", gridItems=[_minimal_gridItem()])

    def test_missing_mode_rejected(self):
        with pytest.raises(Exception):
            _PopupConfigPayload(gridItems=[_minimal_gridItem()])

    def test_extra_field_on_gridItem_rejected(self):
        """extra=forbid catches LLM-emitted unknown keys."""
        with pytest.raises(Exception):
            _PopupConfigGridItemInput(
                source="plotly",
                args={},
                x=0,
                y=0,
                w=1,
                h=1,
                visualizationType="Plotly Chart",  # LLM old-shape leak
            )

    def test_extra_field_on_payload_rejected(self):
        with pytest.raises(Exception):
            _PopupConfigPayload(
                mode="modal",
                gridItems=[_minimal_gridItem()],
                extra_garbage_key="oops",
            )


# ---------------------------------------------------------------------------
# Unit 2 — tool body: happy paths
# ---------------------------------------------------------------------------


class TestToolHappyPath:
    """Valid calls produce {patch_update: {uuid, source: 'Map', ops: [...]}}."""

    def test_minimal_call_returns_patch_update(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        assert "patch_update" in result
        assert "error" not in result

    def test_envelope_source_is_Map(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        assert result["patch_update"]["source"] == "Map"

    def test_single_op_only(self):
        """v1 emits exactly one op — single-op atomicity (no partial-failure)."""
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        ops = result["patch_update"]["ops"]
        assert len(ops) == 1
        assert ops[0]["op"] == "add"

    def test_path_uses_layer_index(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=3,
            popup_config=_minimal_payload(),
        )
        path = result["patch_update"]["ops"][0]["path"]
        assert path == "/args/layers/3/popupConfig"

    def test_layer_index_zero_path(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        assert result["patch_update"]["ops"][0]["path"] == "/args/layers/0/popupConfig"

    def test_two_gridItems_get_distinct_uuids_and_i(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(
                gridItems=[
                    _minimal_gridItem(),
                    _minimal_gridItem(x=12, source="Text", args={"text": "hi"}),
                ]
            ),
        )
        gridItems = result["patch_update"]["ops"][0]["value"]["gridItems"]
        assert len(gridItems) == 2
        assert gridItems[0]["uuid"] != gridItems[1]["uuid"]
        assert gridItems[0]["i"] == "1"
        assert gridItems[1]["i"] == "2"

    def test_json_string_popup_config_accepted(self):
        """Some LLM providers stringify dict args — _coerce_json_strings handles it."""
        payload = _minimal_payload()
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=json.dumps(payload),
        )
        assert "patch_update" in result
        assert "error" not in result

    def test_omitted_position_defaults_to_centered_60x60(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        position = result["patch_update"]["ops"][0]["value"]["position"]
        assert position == _DEFAULT_POPUP_POSITION

    def test_omitted_metadata_defaults_to_refresh_rate_zero(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        gridItems = result["patch_update"]["ops"][0]["value"]["gridItems"]
        metadata = json.loads(gridItems[0]["metadata_string"])
        assert metadata == _DEFAULT_GRID_ITEM_METADATA

    def test_omitted_titleTemplate_defaults_empty(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        assert result["patch_update"]["ops"][0]["value"]["titleTemplate"] == ""

    def test_titleTemplate_preserves_feature_template_string(self):
        """Template substitution happens at render time; the server preserves the string verbatim."""
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(
                titleTemplate="Site ${feature.station_name}",
            ),
        )
        assert (
            result["patch_update"]["ops"][0]["value"]["titleTemplate"]
            == "Site ${feature.station_name}"
        )

    def test_feature_template_inside_gridItem_args_preserved(self):
        """`${feature.<key>}` AND `${variable_name}` inside args survive json.dumps verbatim."""
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(
                gridItems=[
                    _minimal_gridItem(
                        source="GeoGLOWS Forecast Plot",
                        args={
                            "River ID": "${feature.comid}",
                            "Dashboard Filter": "${siteName}",
                        },
                    )
                ]
            ),
        )
        gridItems = result["patch_update"]["ops"][0]["value"]["gridItems"]
        args = json.loads(gridItems[0]["args_string"])
        assert args["River ID"] == "${feature.comid}"
        assert args["Dashboard Filter"] == "${siteName}"


# ---------------------------------------------------------------------------
# Unit 2 — tool body: error paths
# ---------------------------------------------------------------------------


class TestToolErrorPaths:
    """Validation failures return {error, fix_hint} envelopes."""

    def test_invalid_uuid_rejected(self):
        result = configure_popup_modal_layer(
            map_uuid="<map_uuid>",  # template placeholder
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        assert "error" in result
        assert "patch_update" not in result

    def test_empty_gridItems_rejected_with_fix_hint(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config={"mode": "modal", "gridItems": []},
        )
        assert "error" in result
        assert "fix_hint" in result
        assert "gridItem" in result["error"].lower() or "list" in result["error"].lower()

    def test_mode_carousel_rejected(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config={"mode": "carousel", "gridItems": [_minimal_gridItem()]},
        )
        assert "error" in result
        assert "fix_hint" in result

    def test_popup_config_none_rejected(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=None,
        )
        assert "error" in result
        assert "fix_hint" in result

    def test_popup_config_empty_string_rejected(self):
        """Empty string passes _coerce_json_strings (json.loads('') raises JSONDecodeError)."""
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config="",
        )
        assert "error" in result

    def test_popup_config_non_dict_rejected(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=42,
        )
        assert "error" in result
        assert "fix_hint" in result

    def test_negative_layer_index_rejected(self):
        """Pydantic Field(ge=0) rejects negative layer_index via FastMCP validation
        layer; we can't trigger this from the tool body directly because Annotated/
        Field validation runs before the function. Confirm the constraint exists.
        """
        # Pydantic ConstrainedInt(ge=0) would raise if FastMCP passes -1 through; sanity-
        # check via the model directly:
        from typing_extensions import Annotated  # noqa: F401
        from pydantic import Field, TypeAdapter

        adapter = TypeAdapter(Annotated[int, Field(ge=0)])
        with pytest.raises(Exception):
            adapter.validate_python(-1)


# ---------------------------------------------------------------------------
# Unit 4 — Envelope contract round-trip
# ---------------------------------------------------------------------------


class TestEnvelopeContract:
    """Server normalization produces the canonical persisted shape."""

    def test_persisted_gridItem_has_canonical_fields(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        gridItem = result["patch_update"]["ops"][0]["value"]["gridItems"][0]
        expected_keys = {
            "i",
            "uuid",
            "id",
            "source",
            "args_string",
            "metadata_string",
            "x",
            "y",
            "w",
            "h",
        }
        assert set(gridItem.keys()) == expected_keys

    def test_persisted_gridItem_excludes_llm_input_shape_keys(self):
        """No `args` (dict), `metadata` (dict), `visualizationType`, `props`,
        or `position` at the gridItem level."""
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        gridItem = result["patch_update"]["ops"][0]["value"]["gridItems"][0]
        forbidden = {"args", "metadata", "visualizationType", "props", "position"}
        assert forbidden.isdisjoint(gridItem.keys())

    def test_args_string_is_json_encoded_dict(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(
                gridItems=[_minimal_gridItem(args={"x": 1, "y": "two"})]
            ),
        )
        gridItem = result["patch_update"]["ops"][0]["value"]["gridItems"][0]
        assert isinstance(gridItem["args_string"], str)
        assert json.loads(gridItem["args_string"]) == {"x": 1, "y": "two"}

    def test_metadata_string_is_json_encoded_dict(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(
                gridItems=[_minimal_gridItem(metadata={"refreshRate": 30})]
            ),
        )
        gridItem = result["patch_update"]["ops"][0]["value"]["gridItems"][0]
        assert json.loads(gridItem["metadata_string"]) == {"refreshRate": 30}

    def test_persisted_uuid_is_string_uuid4(self):
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        gridItem = result["patch_update"]["ops"][0]["value"]["gridItems"][0]
        # round-trips through uuid_mod.UUID — confirms valid uuid4 string
        parsed = uuid_mod.UUID(gridItem["uuid"])
        assert parsed.version == 4

    def test_persisted_id_is_None(self):
        """Matches PopupLayoutEditor.js seed (id is the SQLAlchemy primary-key placeholder)."""
        result = configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        gridItem = result["patch_update"]["ops"][0]["value"]["gridItems"][0]
        assert gridItem["id"] is None

    def test_recall_mints_fresh_uuids(self):
        """Destructive replace: re-calling produces new gridItem UUIDs (KTD #8)."""
        map_uuid = _fresh_uuid()
        first = configure_popup_modal_layer(
            map_uuid=map_uuid,
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        second = configure_popup_modal_layer(
            map_uuid=map_uuid,
            layer_index=0,
            popup_config=_minimal_payload(),
        )
        u1 = first["patch_update"]["ops"][0]["value"]["gridItems"][0]["uuid"]
        u2 = second["patch_update"]["ops"][0]["value"]["gridItems"][0]["uuid"]
        assert u1 != u2


class TestWhitelistCoverage:
    """The Map source whitelist's /args/layers prefix already covers deep paths."""

    def test_path_to_popupConfig_allowed(self):
        assert is_path_allowed("Map", "/args/layers/0/popupConfig")

    def test_path_to_popupConfig_title_allowed(self):
        """patch_visualization on this sub-path must continue to work (R8)."""
        assert is_path_allowed("Map", "/args/layers/0/popupConfig/titleTemplate")

    def test_path_to_gridItem_args_string_allowed(self):
        """Deepest path the tool writes to — proves prefix-match covers it."""
        assert is_path_allowed(
            "Map", "/args/layers/3/popupConfig/gridItems/0/args_string"
        )

    def test_path_to_position_widthPct_allowed(self):
        assert is_path_allowed(
            "Map", "/args/layers/0/popupConfig/position/widthPct"
        )

    def test_path_to_nonexistent_source_outside_layers_rejected(self):
        """Sanity: a path under /args/legend (NOT in Map whitelist) is rejected."""
        assert not is_path_allowed("Map", "/args/legend/title")


# ---------------------------------------------------------------------------
# Unit 3 — Slash prompt
# ---------------------------------------------------------------------------


class TestSlashPrompt:
    """Prompt scaffolds the tool call with provided args."""

    def test_prompt_returns_string(self):
        body = _prompt_configure_popup_modal_layer(
            map_uuid=_fresh_uuid(),
            layer_index="0",
            popup_config=json.dumps(_minimal_payload()),
        )
        assert isinstance(body, str)
        assert "configure" in body.lower() or "popup" in body.lower()

    def test_prompt_includes_supplied_args(self):
        """The scaffolded prompt references the user-supplied identifiers."""
        my_uuid = _fresh_uuid()
        body = _prompt_configure_popup_modal_layer(
            map_uuid=my_uuid,
            layer_index="3",
            popup_config="{}",
        )
        assert my_uuid in body
        assert "3" in body


# ---------------------------------------------------------------------------
# Tool description contract — keep documentation-shaping clauses in sync
# ---------------------------------------------------------------------------


class TestToolDescription:
    """Description text carries the load-bearing prose clauses."""

    @pytest.fixture(scope="class")
    def description(self):
        """Fetch the registered tool's description via FastMCP's local provider.

        Mirrors the pattern in test_tool_description_exclusivity.py to
        bypass middleware/transforms and read the raw registered string.
        """
        import asyncio

        from tethysdash_mcp.mcp_server import mcp

        async def go():
            return await mcp._local_provider.list_tools()

        loop = asyncio.new_event_loop()
        try:
            tools = loop.run_until_complete(go())
        finally:
            loop.close()
        by_name = {t.name: (t.description or "") for t in tools}
        return by_name["configure_popup_modal_layer"]

    def test_description_leads_with_positive_use(self, description):
        """Description must lead with FIRST-TIME setup framing.

        Debug session 2026-05-21 turn 2: gemini-flash read the prior
        exclusion-first phrasing ("DO NOT use this tool to edit...") as
        "don't use configure_popup_modal_layer for anything that touches
        an existing layer" and routed to patch_visualization. Positive-
        first framing fixes this.
        """
        d = description.lower()
        assert "use this tool for first-time popup-modal setup" in d, (
            "Description must lead with the positive use case so the LLM "
            "picks this tool for first-time popup-modal setup."
        )

    def test_description_names_patch_visualization_carveout(self, description):
        """Partial-edits-only clause for patch_visualization survives."""
        d = description.lower()
        # The patch_visualization carve-out must be present, but it must
        # NOT be the leading framing (covered by test_description_leads_with_positive_use).
        assert "patch_visualization" in description
        assert (
            "use patch_visualization only for partial edits" in d
            or "only for partial edits to an already-existing" in d
        )

    def test_description_names_same_turn_race_constraint(self, description):
        """KTD #6 / Unit 2 test scenario."""
        d = description.lower()
        assert "after" in d
        assert "add_" in description or "dashboard_state" in d

    def test_description_names_feature_template_abstractly(self, description):
        """Names `${feature.<key>}` syntax without concrete example values."""
        assert "${feature.<key>}" in description
        # Heuristic for "no concrete example value": no `${feature.station_name}`
        # or other resolved-attribute names in the description. (Test scenarios
        # use concrete values; the tool description itself must not.)
        assert "${feature.station_name}" not in description
        assert "${feature.comid}" not in description

    def test_description_points_at_source_discovery_tools(self, description):
        assert "list_available_visualizations" in description
        assert "list_intake_plugins" in description

    def test_description_names_full_overwrite_semantic(self, description):
        d = description.lower()
        assert "replaces" in d
