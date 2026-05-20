# Changelog

All notable changes to the standalone tethysdash MCP server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Image tags follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] — 2026-05-19

### Changed

- **`data` field descriptions on `create_plotly_chart`, `create_data_table`, `create_card` now lead with `data_uri` as the preferred path.** Companion to chatbox-core 0.12.0's pre-dispatch inline-list cap. Small models pattern-match on the first option listed in a field description; with the inline shape leading, nemotron-class models reliably picked inline `data` even when a cached `_cache_uri` was available — triggering JSON parse errors at the ~1KB threshold (observed: nemotron-3-nano-30b on a 24-record NRDS plot, `Extra data: line 1 column 1275`, ~200s repair-loop oscillation). Each description now opens with a `PREFER data_uri ...` clause naming `_cache_uri` as the source and the small-model failure mode as the reason. Inline shape moves to a clearly-labeled "Inline shape:" section after. No contract change — inline `data` still accepted for small synthesized payloads.

- **New regression test `test_data_uri_description_ordering.py`** pins the new framing: each of the three tools' `data` descriptions must (a) name `data_uri`, (b) name `_cache_uri`, (c) mention `data_uri` before the inline-shape section, (d) name the small-model failure mode. Catches future description edits that move inline back to the lead.

## [0.3.0] — 2026-05-19

### Added

- **`data_uri` opt-in on `create_plotly_chart`, `create_data_table`, `create_card`.** Receiving side of chatbox-core's MCP result-by-reference protocol (chatbox-core 0.7.0). The three create_* tools that take inline `data` arrays gain an optional `data_uri: str | list[str]` arg. After chatbox-core's substitution layer resolves the URI from its IndexedDB cache, the server sees the call as if the LLM had passed `data` directly — no MCP wire-contract change for the mediated path. New shared helpers in `tethysdash_mcp/_uri_field.py`: `uri_field(inline_arg_name=...)` for the Pydantic Field-factory (regex-validated `mcp+cache://<conv-id>/<token>`, `max_length=128`) and `ensure_exactly_one_set(...)` for the mutual-exclusion contract.

- **Envelope unwrap via `BeforeValidator(_unwrap_data_envelope)`** on `create_plotly_chart.data` and `create_data_table.data`. Pre-validator extracts the first list-valued `data` / `rows` / `records` key from dict inputs before Pydantic's Union check. Defends against the case where chatbox-core's URI substitution writes a full upstream envelope (`{ok, rows, columns, data:[...]}`) into a slot that expected the inner list. Published JSON schema is unchanged — LLM-visible types stay `Union[List, str]`.

- **Records-mode pivot on `create_plotly_chart`.** New optional `x_field` / `y_field` / `series_field` args let the LLM name source columns instead of constructing Plotly trace arrays. Server detects records (list of dicts without Plotly trace keys `x`/`y`/`type`) and pivots into traces — single trace if no `series_field`, one trace per group otherwise. Removes the LLM-as-ETL transformation step that the cache+URI protocol was silently bypassing.

- **None-string + JSON-string coercion on `Optional[Dict]` args.** `layout` and `config` on `create_plotly_chart` widened to `Optional[Union[Dict[str, Any], str]]`. Server coerces `"None"` / `"null"` / `""` literal strings to actual `None` (some Ollama Cloud models emit those for genuinely-empty optional dicts), then `json.loads` if still string (some models emit nested dicts as JSON strings to flatten output complexity).

### Changed

- **Softened `_uri_field.py` description framing.** Dropped prohibitive `"DO NOT"` / `"WRONG"` / `"wasting tokens"` language that was suspected of biasing weak models toward defensive over-quoting. Replaced with imperative direction on when to use the URI vs. inline form.

### Tests

- 24 new tests in `test_mcp/test_data_uri_opt_in.py` covering: backward-compat inline-data calls on all three tools (3); unmediated-client `data_uri` rejection (3); both-set / neither-set conflict envelopes (5); Pydantic pattern enforcement on bad URIs (2); None-string + JSON-string coercion + production-failure-mode reproduction (5); envelope unwrap (data/rows/records keys + rejection of dicts without list-valued keys) and records-mode pivot (single trace, series grouping, missing-field rejection, backward-compat trace passthrough, end-to-end envelope→records→traces) (6). Full suite: 821 passed.

### Documentation

- Solution documented at `docs/solutions/integration-issues/mcp-data-envelope-unwrap-and-records-pivot-2026-05-19.md` (firoh workspace). Cross-link refreshes applied to two related `best-practices/` docs (Pydantic field constraints + dict parameter coercion).

## [0.2.0] — 2026-05-11

### Added

- **Image publishing workflow `.github/workflows/release.yml` (2026-05-11).** Tag-driven (`v*`), multi-arch (`linux/amd64` + `linux/arm64`), publishes to `ghcr.io/aquaveo/tethysdash-mcps`. Image tags derive from the git tag (`v0.2.0` → `0.2.0` + `latest`). Mirrors the release-job half of `mcp/nrds_mcps/.github/workflows/release.yml`; the Cloud Run deploy job is deliberately out of scope (see plan `2026-05-11-007-feat-tethysdash-mcps-image-publishing-plan.md`). Includes `provenance: true` + `sbom: true` for supply-chain attestation and GHA cache for fast subsequent builds.

  **One-time operator action after the first tag push:** the GitHub Container Registry package defaults to **private**. To let workshop participants and downstream operators `docker pull` anonymously, flip the package to public at `https://github.com/orgs/Aquaveo/packages/container/tethysdash-mcps/settings` → Danger Zone → "Change visibility" → Public. Subsequent tag pushes inherit the public visibility automatically.

  **Workshop consumption path:**

  ```bash
  docker run --rm -p 9001:9001 \
    -e TETHYSDASH_BASE_URL=https://workshop-tethys.example.com/apps/tethysdash \
    ghcr.io/aquaveo/tethysdash-mcps:latest
  ```

### Changed

- **Runtime plugin registry now read via HTTP (2026-05-11).** The standalone reads the runtime plugin registry from `${TETHYSDASH_BASE_URL}/runtime-plugins/list/` instead of a local JSON file. Removes the filesystem-coupled `TETHYSDASH_RUNTIME_REGISTRY_PATH` env var entirely. tethysdash side added a sibling read-only endpoint (`Aquaveo/tethysapp-tethys_dash` PR — companion to this change) that exposes the registry anonymously while the existing `runtime-plugins/sync/` POST endpoint stays gated for the browser-side write flow. Plan reference: `docs/plans/2026-05-11-006-feat-runtime-plugin-registry-http-endpoint-plan.md`.

  Operator-visible delta: drop `TETHYSDASH_RUNTIME_REGISTRY_PATH` from your environment. Set only `TETHYSDASH_BASE_URL`. The standalone now decouples cleanly from tethysdash's filesystem and can be deployed remotely (Cloud Run, sidecar, separate host) without volume-mount choreography.

- **`register_runtime_plugin` MCP tool is feature-flagged off in standalone mode.** Returns a structured `{"error": "registration_not_supported", ...}` envelope rather than writing to disk. The tool stays in the MCP surface (so chatbox-core's tool list stays consistent and the prompt parity contract holds); it just refuses the call. Plugin registration goes through the browser-side chatbox UI which posts to tethysdash's `runtime-plugins/sync/` with the user's session credential. Re-enables once auth + an authenticated write path land (plan 2026-05-11-004, deferred).

- Deleted `tethysdash_mcp/plugin_registry_loader.py` — the file existed solely to wrap filesystem reads; the HTTP helper replaces it. The tethysdash-side `plugin_registry_loader.py` (a different file, in `tethysapp-tethys_dash/`) stays — it backs the live `runtime_plugins_sync` Django endpoint.

### Added

- **`scripts/setup-mcp.sh` (2026-05-11).** Bundled helper modeled on `nrds_mcps/scripts/setup-mcp.sh`. Creates `.venv-mcp/`, installs `tethysdash_mcp/requirements.lock`, and runs `python -m tethysdash_mcp.mcp_server` from the repo dir so the `tethysdash_mcp` package resolves without a `pyproject.toml`. Subcommands: `--setup` (install only), `--run` (boot only), no-arg (setup + run). Honors `MCP_PORT`, `MCP_HOST`, `MCP_TRANSPORT`, `TETHYSDASH_BASE_URL`, `ALLOWED_ORIGINS` from the environment. Replaces the previous Quick Start (Python) recipe and Step 2 / Step 4 of the dev runbook with single-line invocations.

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
- Entry-point env vars: `MCP_PORT` (default `9001`), `MCP_HOST` (package default `127.0.0.1`, Dockerfile overrides to `0.0.0.0`), `MCP_TRANSPORT` (default `streamable-http`), `ALLOWED_ORIGINS` (unset → wildcard fallback).
- `TETHYSDASH_BASE_URL` defaults to empty string; HTTP-call tools return a `backend_not_configured` envelope when unset, rather than silently mis-targeting localhost.
- Dockerfile (two-stage builder → runtime, non-root, `HOME=/tmp`, port 9001, `/health` HEALTHCHECK).
- GitHub Actions `ci.yml` (smoke import + standalone-independence check + local-load docker build; no push, no release pipeline).
