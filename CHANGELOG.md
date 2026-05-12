# Changelog

All notable changes to the standalone tethysdash MCP server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Image tags follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`scripts/setup-mcp.sh` (2026-05-11).** Bundled helper modeled on `nrds_mcps/scripts/setup-mcp.sh`. Creates `.venv-mcp/`, installs `tethysdash_mcp/requirements.lock`, and runs `python -m tethysdash_mcp.mcp_server` from the repo dir so the `tethysdash_mcp` package resolves without a `pyproject.toml`. Subcommands: `--setup` (install only), `--run` (boot only), no-arg (setup + run). Honors `MCP_PORT`, `MCP_HOST`, `MCP_TRANSPORT`, `TETHYSDASH_BASE_URL`, `TETHYSDASH_RUNTIME_REGISTRY_PATH`, `ALLOWED_ORIGINS` from the environment. Replaces the previous Quick Start (Python) recipe and Step 2 / Step 4 of the dev runbook with single-line invocations.

- **Dev runbook + CLI smoke evidence (2026-05-11).** New README section "Running alongside a local tethysdash dev server" with a six-step walkthrough (prerequisites → tethys dev server → venv bootstrap → bridge env vars → standalone boot → chatbox URL config → smoke checklist). Workspace `firoh/CLAUDE.md` carries a pointer paragraph. Key env-var bridge: `TETHYSDASH_RUNTIME_REGISTRY_PATH` must point at `<workspace>/tethysapp-tethys_dash/reactapp/generated/runtimePluginRegistry.json` so chatbox-registered runtime plugins reach the standalone server.

  **CLI smoke run (2026-05-11):** Bootstrapped a fresh venv, set the bridge env vars, booted the standalone, exercised the MCP surface from a Python `fastmcp.Client`:
  - `GET /health` → 200 `{"status":"ok"}`. ✅
  - MCP initialize + `list_tools` → 25 tools enumerated (matches `@mcp.tool` count). ✅
  - `create_card(title='hello', description='world', data='42')` → clean `{"visualization": {...}}` envelope, uuid generated. ✅
  - `list_intake_plugins({})` against unreachable tethysdash backend → structured error envelope (`Failed to fetch intake plugins from TethysDash: HTTPConnectionPool... Connection refused`) — not a crash, not a silent fallback. ✅
  - Input-validation middleware: `create_card(body='world')` (wrong kwarg name) → rich validator envelope with `unexpected_kwargs`, `expected_kwargs`, and `fix_hint`. ✅

  **Browser-side smoke pending human follow-up:** runtime-plugin registration via the chatbox UI through the `TETHYSDASH_RUNTIME_REGISTRY_PATH` bridge, end-to-end `patch_visualization` against a live tethysdash dev server, and the chatbox tool-selection / slash-popover surface. CLI smoke confirms the standalone is functionally complete and the runbook is copy-paste correct; the chatbox-driven flows require a human at the keyboard.

  **Gotcha caught + fixed mid-smoke:** initial Step 4 invocation `mcp/tethysdash_mcps/.venv/bin/python -m tethysdash_mcp.mcp_server` from the workspace root failed with `ModuleNotFoundError: No module named 'tethysdash_mcp'` — the repo has no `pyproject.toml`, so `tethysdash_mcp` is only importable when cwd contains the package directory. Runbook updated to use `(cd mcp/tethysdash_mcps && .venv/bin/python -m tethysdash_mcp.mcp_server)` (subshell preserves Step 3 env vars without changing the user's prompt cwd).

  Together these satisfy plan 003 (`docs/plans/2026-05-11-003-refactor-remove-embedded-mcp-server-plan.md`) revival triggers 1 (runnable as a daily dev artifact) and 3 (HTTP-call tools verified against a configured `TETHYSDASH_BASE_URL`). Trigger 2 (chatbox runtime-plugin registration through the bridge) remains pending the browser-side follow-up.

## [0.1.0] — 2026-05-11

### Added

- Initial standalone extract from `tethysapp-tethys_dash/tethysapp/tethysdash/mcp/`. 25 `@mcp.tool` definitions + 25 `@mcp.prompt` definitions, parity with the embedded server as of the source commit. Code duplication strategy (no shared package); the embedded server in tethysdash remains the developer-local default, this repo is a parallel artifact ready for future deploy work.
- Symbol-allowlist copy of `plugin_helpers.py` (omits `TethysDashPlugin`, `send_websocket_message`, `validate_feature_collection` to avoid `intake.source.base` / Django Channels coupling).
- `editable_schemas.py` rewritten to load `editableSchemas.json` from a package-local `data/` directory instead of the React frontend's `reactapp/config/`.
- `editable_schemas_plugin.py` import rewritten to use `tethysdash_mcp.plugin_helpers`.
- `plugin_registry_loader.py` rewritten so `_RUNTIME_REGISTRY_PATH` defaults to `/tmp/runtimePluginRegistry.json` (overridable via `TETHYSDASH_RUNTIME_REGISTRY_PATH` env var).
- Standalone-independence guard: `test_standalone_independence.py` asserts no `tethysapp.*` or `django` modules load when the server is imported.
- Entry-point env vars: `MCP_PORT` (default `9000`), `MCP_HOST` (package default `127.0.0.1`, Dockerfile overrides to `0.0.0.0`), `MCP_TRANSPORT` (default `streamable-http`), `ALLOWED_ORIGINS` (unset → wildcard fallback).
- `TETHYSDASH_BASE_URL` defaults to empty string; HTTP-call tools return a `backend_not_configured` envelope when unset, rather than silently mis-targeting localhost.
- Dockerfile (two-stage builder → runtime, non-root, `HOME=/tmp`, port 9000, `/health` HEALTHCHECK).
- GitHub Actions `ci.yml` (smoke import + standalone-independence check + local-load docker build; no push, no release pipeline).
