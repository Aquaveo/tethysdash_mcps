# tethysdash MCP server

Standalone [Model Context Protocol](https://modelcontextprotocol.io) server providing the tethysdash dashboard-creation tool surface: 25 `@mcp.tool` definitions covering native visualizations (Plotly, table, card, text, image, map), 12 map layer types (WMS, GeoJSON, ESRI, PMTiles, GeoTIFF, …), runtime Module-Federation plugin registration, and RFC 6902 patch operations -- plus 25 matching `@mcp.prompt` slash-command templates.

Built on [FastMCP](https://github.com/jlowin/fastmcp) with Streamable HTTP transport.

> **Relationship to the embedded server.** This package is a parallel artifact extracted from `tethysapp-tethys_dash/tethysapp/tethysdash/mcp/`. The embedded MCP server inside tethysdash remains the developer-local default and the canonical home; this repo is a deploy-ready duplicate that operators can run as a container without bringing in the full Django + Tethys stack. If you are doing day-to-day tethysdash development, work in the embedded server. If you are deploying the MCP surface as a service, use this one.

## Quick start (Docker)

```bash
docker build -t tethysdash-mcps:local .
docker run --rm -d \
  --name tethysdash-mcps \
  -p 9000:9000 \
  -e TETHYSDASH_BASE_URL=https://your-tethys-host/apps/tethysdash \
  tethysdash-mcps:local
```

Verify it's running:

```bash
curl -fsS http://localhost:9000/health
# {"status":"ok"}
```

Connect an MCP client to `http://<host>:9000/mcp` (Streamable HTTP transport). Set `MCP_TRANSPORT=sse` and connect to `/sse` for legacy clients.

## Quick start (Python)

```bash
pip install -r tethysdash_mcp/requirements.lock
TETHYSDASH_BASE_URL=https://your-tethys-host/apps/tethysdash \
  python -m tethysdash_mcp.mcp_server
```

The server binds to `127.0.0.1:9000` by default; set `MCP_HOST=0.0.0.0` for non-loopback binding (the Dockerfile already does this for the container path).

## Configuration (env vars)

| Variable | Default | Description |
|---|---|---|
| `MCP_PORT` | `9000` | Port the server listens on. |
| `MCP_HOST` | `127.0.0.1` (package) / `0.0.0.0` (Docker) | Bind address. Loopback by default for safety; the container's ENV overrides to `0.0.0.0` so the published port is reachable. |
| `MCP_TRANSPORT` | `streamable-http` | `streamable-http` (path `/mcp`) or `sse` (path `/sse`). |
| `ALLOWED_ORIGINS` | `*` | CORS allow-list, comma-separated. Set explicitly for production behind a known origin -- wildcard auto-disables `allow_credentials`. |
| `TETHYSDASH_BASE_URL` | *(empty)* | Base URL of the TethysDash Django app (e.g., `https://tethys.example.com/apps/tethysdash`). When unset, tools that proxy to the backend (`list_intake_plugins`, dynamic-map-layer plugin discovery) return a structured `backend_not_configured` envelope rather than silently mis-targeting `localhost`. |
| `TETHYSDASH_RUNTIME_REGISTRY_PATH` | `/tmp/runtimePluginRegistry.json` | JSON file backing the runtime Module-Federation plugin registry. `register_runtime_plugin` writes here; `list_available_visualizations` / `render_custom_visualization` read here. The `/tmp` default means registrations survive within a single container session but not across restarts -- mount a volume and point this env var at it for persistence. |
| `TETHYSDASH_LOG_LEVEL` | `INFO` | One of `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `TETHYSDASH_VERBOSE_ACCESS` | *(unset)* | Set `1`/`true`/`yes` to keep all uvicorn HTTP access logs. Default dampens noise. |

## Endpoints

| Path | Method | Purpose |
|---|---|---|
| `/mcp` | GET, POST | MCP Streamable HTTP transport. |
| `/sse` | GET | MCP SSE transport (only when `MCP_TRANSPORT=sse`). |
| `/health` | GET | Liveness probe. Returns `200 {"status":"ok"}`. |

## Tool surface

25 `@mcp.tool` definitions across four families:

- **Discovery** (zero-arg): `list_intake_plugins`, `list_available_visualizations`
- **Visualization create**: `create_plotly_chart`, `create_data_table`, `create_card`, `create_text`, `create_custom_image`, `create_map_visualization`, `create_variable_input`
- **Render / register**: `render_plugin`, `render_custom_visualization`, `register_runtime_plugin`
- **Modify**: `patch_visualization` (RFC 6902-style ops)
- **Map layers** (each takes the `map_uuid` returned by `create_map_visualization`): `add_wms_layer`, `add_esri_image_layer`, `add_esri_feature_layer`, `add_geojson_layer`, `add_kml_layer`, `add_image_tile_layer`, `add_vector_tile_layer`, `add_pmtiles_vector_layer`, `add_pmtiles_raster_layer`, `add_geotiff_layer`, `add_static_image_layer`, `add_dynamic_map_layer`

25 matching `@mcp.prompt` slash-command templates expose each tool through the chatbox slash-popover (Phase 3a/3b/3c parity). See `tethysdash_mcp/mcp_server.py` for the full definitions.

## Backend dependency

Most tools are pure: they accept arguments, return a `{"visualization": ...}` or `{"layer_update": ...}` envelope, and the host UI dispatches it. **Two tools call back to the TethysDash Django backend** for plugin metadata:

- `list_intake_plugins` -- proxies `GET {TETHYSDASH_BASE_URL}/visualizations/list/`
- `add_dynamic_map_layer` -- resolves the runtime map-layer plugin metadata via the same endpoint

When `TETHYSDASH_BASE_URL` is empty (the package default), both return:

```json
{"error": "backend_not_configured", "message": "TETHYSDASH_BASE_URL is unset. ..."}
```

The unset default is intentional: silent fallback to `localhost:8080` (the embedded-server default) would be a footgun for a containerized deployment.

## Development

Run the contract test suite:

```bash
pip install -r tethysdash_mcp/requirements.lock pytest pytest-asyncio pytest-mock
pytest test_mcp/ -q
```

777 tests covering tool input validation, output envelope shapes, prompt/tool parity, runtime plugin dispatch, CORS, and a standalone-independence guard (`test_mcp/test_standalone_independence.py` asserts no `tethysapp.*` or `django` modules load during import -- runs in a subprocess so it doesn't pollute other tests). The full suite runs in under 6 seconds; no database, no Django.

## Updating from the embedded server

This package is a snapshot of `tethysapp-tethys_dash/tethysapp/tethysdash/mcp/` at extract time. When the embedded server lands a tool change (new `@mcp.tool`, modified input schema, changed envelope shape, prompt rename), re-sync by copying the changed module here and re-applying the standalone-specific rewrites:

- Top-of-file imports: `from tethysapp.tethysdash.X` -> `from tethysdash_mcp.X`
- `tethysdash_mcp/plugin_registry_loader.py::_RUNTIME_REGISTRY_PATH` -> env-var read with `/tmp` default (already in place)
- `tethysdash_mcp/editable_schemas.py::_JSON_PATH` -> `Path(__file__).parent / "data" / "editableSchemas.json"` (already in place)
- `TETHYSDASH_BASE_URL` default -> `""` (already in place)
- Entry-point env vars: `TETHYSDASH_MCP_PORT` -> `MCP_PORT`, default port `9001` -> `9000` (already in place)

If `editableSchemas.json` changes on the embedded side, copy the updated JSON into `tethysdash_mcp/data/`. The standalone-independence test will catch any missed import rewrites; the full contract suite will catch envelope drift.

See `CHANGELOG.md` for the per-release sync notes.
