# Changelog

All notable changes to the standalone tethysdash MCP server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Image tags follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
