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

Use the bundled setup script — it creates a dedicated venv at `.venv-mcp/`, installs the locked dependencies, and starts the server in one step:

```bash
TETHYSDASH_BASE_URL=https://your-tethys-host/apps/tethysdash \
  ./scripts/setup-mcp.sh
```

Subcommands:

- `./scripts/setup-mcp.sh --setup` — create the venv and install deps only (no run)
- `./scripts/setup-mcp.sh --run` — start the server (venv must already exist)
- `./scripts/setup-mcp.sh` (no args) — setup + run

The script honors `MCP_PORT`, `MCP_HOST`, `MCP_TRANSPORT`, `TETHYSDASH_BASE_URL`, `TETHYSDASH_RUNTIME_REGISTRY_PATH`, and `ALLOWED_ORIGINS` from the environment. See the env-var table below for defaults.

The server binds to `127.0.0.1:9000` by default; set `MCP_HOST=0.0.0.0` for non-loopback binding (the Dockerfile already does this for the container path).

## Running alongside a local tethysdash dev server

This is the recommended setup for **exercising the standalone in your day-to-day chatbox workflow** instead of (or alongside) the embedded server in `tethysapp-tethys_dash/tethysapp/tethysdash/mcp/`. Six steps from a clean clone to a working chatbox.

### Prerequisites

- Both repos cloned into the same workspace root (e.g., `~/tethysdev/firoh/`):
  - `tethysapp-tethys_dash/` — the Django app
  - `mcp/tethysdash_mcps/` — this repo
- A working Python environment for the tethysdash side, with Django + Tethys SDK + tethysdash installed. Conda, pyenv, or system Python all work — whatever you normally use to run `tethys manage start`.
- Python 3.11+ available for the standalone's dedicated venv. The bundled `scripts/setup-mcp.sh` creates `.venv-mcp/` for you using `python3` from your PATH. The standalone is deliberately Django/Tethys-free; **do not install it into the tethysdash environment** — the standalone-independence assertion enforces no Django leakage.
- Loopback only. The runbook below does **not** add authentication. Both processes bind to `127.0.0.1`. Do not expose either port to a non-loopback network with this setup.

### Step 1 — Start the tethysdash Django dev server

From the workspace root, in a terminal with your tethysdash Python environment active:

```bash
tethys manage start -p 8000
```

Confirm `http://localhost:8000/apps/tethysdash/` loads in a browser. Sign in.

### Step 2 — Bootstrap the standalone venv

In a second terminal, from the workspace root, run the setup script in `--setup` mode:

```bash
./mcp/tethysdash_mcps/scripts/setup-mcp.sh --setup
```

This creates `mcp/tethysdash_mcps/.venv-mcp/` and installs the locked dependencies from `tethysdash_mcp/requirements.lock`. Idempotent — re-run only when the lockfile changes.

### Step 3 — Set the bridge env vars

The standalone needs two pointers: where the tethysdash backend lives, and where to read the runtime-plugin registry that tethysdash's `runtime_plugins_sync` controller writes to. Export from the second terminal:

```bash
export TETHYSDASH_BASE_URL=http://localhost:8000/apps/tethysdash
export TETHYSDASH_RUNTIME_REGISTRY_PATH="$(pwd)/tethysapp-tethys_dash/reactapp/generated/runtimePluginRegistry.json"
```

`TETHYSDASH_RUNTIME_REGISTRY_PATH` **must be an absolute path** and must point at the same file that tethysdash's `controllers.runtime_plugins_sync` writes (`reactapp/generated/runtimePluginRegistry.json` under the tethysdash repo). Without this, plugins you register via the chatbox UI will silently fail to reach the LLM running against the standalone.

### Step 4 — Start the standalone

From the same terminal where you exported the env vars in Step 3, run the setup script in `--run` mode:

```bash
./mcp/tethysdash_mcps/scripts/setup-mcp.sh --run
```

The script `cd`s into the repo before invoking `python -m tethysdash_mcp.mcp_server`, so the exported env vars carry through and your prompt's cwd doesn't change. The server listens on `127.0.0.1:9000` (override via `MCP_PORT` / `MCP_HOST`). Confirm `/health` returns 200 (in another terminal):

```bash
curl -fsS http://localhost:9000/health
# {"status":"ok"}
```

The embedded server (port `9001`) is unaffected — both can run simultaneously. The chatbox picks one via its URL config (next step).

### Step 5 — Point the chatbox at the standalone

In the browser tab where tethysdash is open, open the chatbox sidebar (admin/editor only — sign in with an appropriate account if needed). Open the chatbox **settings** (gear icon) and set the MCP server URL to:

```
http://localhost:9000/mcp
```

The URL persists in `localStorage` per the `tethysdash:chat:v1:<uuid>` convention, so this only needs to be configured once per browser per dashboard.

### Step 6 — Smoke checklist

With both servers running and the chatbox configured, work through these end-to-end checks. Each should "just work" — if any fails, file the failure (or check the Known caveats below).

| Check | Prompt / Action | Expected |
|---|---|---|
| Tool list | Open chatbox; observe tool count or slash-command popover | 25 tools surfaced (matches the standalone's `@mcp.tool` count) |
| No-backend tool | `"create a card with title hello and description world showing the value 42"` | Card visualization renders on the dashboard |
| Backend-touching tool | `"list available intake plugins"` | Returns the local tethysdash's intake plugins (proves `TETHYSDASH_BASE_URL` is reachable) |
| Runtime plugin registration | Register a plugin via the chatbox (`/register_runtime_plugin` slash command or natural language) | `reactapp/generated/runtimePluginRegistry.json` is updated; the standalone reads the new plugin on its next tool call |
| Patch operation | `"change the hello card title to greetings"` | `patch_visualization` fires; tile updates in place |
| Map layer | `"create a map and add a WMS layer for ..."` | Map visualization renders; layer appears |

### Known caveats

- **Loopback only.** Do not bind the standalone to `0.0.0.0` in this setup — it has no authentication. Authentication is deferred (see `docs/plans/2026-05-11-004-feat-tethysdash-mcps-token-auth-plan.md`).
- **Registry path must be absolute.** Relative paths like `./reactapp/generated/...` will resolve against whatever directory the standalone was started from. The `$(pwd)/...` form in Step 3 avoids this.
- **Single-process cache.** The standalone reads `runtimePluginRegistry.json` on each tool call that needs it — no in-process caching of registry entries. Restarting the standalone is not required after registering a plugin via the chatbox.
- **Tool list cached by chatbox-core.** If you restart the standalone, the chatbox may show a stale tool list until the next chatbox-core probe interval. Refreshing the browser tab forces a re-probe.
- **No CORS issues expected.** The standalone defaults `ALLOWED_ORIGINS=*` with `allow_credentials=False` auto-derived — compatible with the chatbox fetch model.

### Why not the embedded server?

The embedded server at `tethysapp/tethysdash/mcp/tethysdash_mcp_server.py` (port 9001) is the default and is the chatbox's pre-configured URL out of the box. It works fine and stays in place. This runbook is the path to exercising the standalone — the future home of the MCP server once `docs/plans/2026-05-11-003-refactor-remove-embedded-mcp-server-plan.md` revives.

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

Run the contract test suite against the script-managed venv:

```bash
./scripts/setup-mcp.sh --setup
.venv-mcp/bin/pip install --quiet pytest pytest-asyncio pytest-mock
.venv-mcp/bin/python -m pytest test_mcp/ -q
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
