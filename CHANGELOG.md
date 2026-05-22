# Changelog

All notable changes to the standalone tethysdash MCP server will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Image tags follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] — 2026-05-21

The popup-modal arc: a new MCP tool + slash-prompt for configuring custom-popup modals on map layers, plus a server-side tolerance layer for LLM-emitted identifier-name mismatches (case-fold on plugin args, display-name vs service-name keying on ESRI / WMS layer aliases). Eight PRs, 31 new tests (suite grew 930 → 961).

### Added

- **`configure_popup_modal_layer` MCP tool + matching `@mcp.prompt`** (PR #10). New tool for first-time popup-modal setup on a map layer; complements `patch_visualization` (which is the right tool for partial edits to an existing `popupConfig`). The tool description leads with positive use ("USE THIS TOOL for FIRST-TIME popup-modal setup ..."), names the canonical `popupConfig` path + shape (`{mode, position, titleTemplate, gridItems}`), and explicitly carves out the boundary with `patch_visualization` to prevent LLM mis-routing. The slash-prompt counterpart lets users invoke a guided popup-modal setup flow without naming the tool.

- **Server-side case-fold normalization for `configure_popup_modal_layer` gridItems[].args keys** (PR #14). New helpers `_fetch_plugin_arg_names(source)` and `_normalize_args_case(args, arg_names)` in `tethysdash_mcp/mcp_server.py`. The fetch hits `${TETHYSDASH_BASE_URL}/visualizations/list/` to read the source's authoritative `arg_names`; the normalizer case-fold-matches LLM-emitted keys against canonical names and rewrites mismatches in place. Soft-fails to pass-through when `TETHYSDASH_BASE_URL` is unset, the fetch fails, or the source isn't registered. Hard-fails (structured error envelope) only when two LLM-emitted keys collide case-insensitively. Catches gemini-flash's near-universal snake-case-normalization habit (e.g., emitting `river_id` for a plugin that declared `river_ID`).

### Changed

- **Case-fold args normalization extended to `render_plugin` + `add_dynamic_map_layer`** (PR #15). Same `_fetch_plugin_arg_names` + `_normalize_args_case` helpers reused at both new call sites — no new code in the helpers themselves. Description tightening in both tools' `args` fields too: replaced the prior concrete snake-case example (`{"gauge_id": "${my_gauge}"}`, which reinforced the LLM's snake-case default) with case-sensitivity prose naming the `river_ID` counterexample as anchoring shape only.

- **ESRI Image `attr_key` resolution widened to fire for `popup_options` too** (PR #17). Previously gated on `if attribute_variables:`, missing the case where `popup_options.aliases` alone was provided — the popup-table render path keys by the ESRI service's sublayer name (from `?f=json`), so PR #11's outer-key normalization needed the resolved name as `attr_key` to land on the right key. Guard widened to `if attribute_variables or popup_options:`. Single-line semantic fix.

- **`popup_options` description tightened across all 11 `add_*_layer` tools** (PR #11). The 10 stub descriptions that just said "Click-popup options" now explicitly document the `{layer_name: {field: alias}}` outer-key shape and anchor the outer-key contract to the tool's `name` arg. Companion server-side helper `_normalize_popup_outer_keys` auto-corrects single-entry sub-dicts when the LLM emits a mismatched outer key (e.g., the sublayer ID `"0"` from `params.LAYERDEFS` instead of the layer name).

- **Tool description case-sensitive arg_names + 100-col grid guidance** (PR #13) for `configure_popup_modal_layer`'s `popup_config` field. Names the case-sensitivity rule for `gridItems[].args` keys (with the `river_ID` counterexample as anchoring shape) and the popup's 100-column react-grid-layout convention (with explicit "NOT Bootstrap's 12-column" anti-pattern call-out). Description-only — the case-sensitivity guidance is belt-and-suspenders alongside the server-side normalization in PR #14, since the description-only approach proved insufficient for the LLM's snake-case default on its own.

### Fixed

- **ESRI Image sublayer-name resolution falls back to `params.LAYERDEFS` when `params.LAYERS` is absent** (PR #12). The pre-existing `_resolve_esri_layer_name` could only read the sublayer ID from `params.LAYERS`; when the LLM specified the sublayer via `params.LAYERDEFS = "0:rivercountry = 'Bolivia'"` (no LAYERS, no `layer_id` arg), the resolver returned `None` and fell back to the user-supplied display name — but the React popup-render path queries by the ESRI service's sublayer name fetched from `?f=json`, so the click-time lookup silently missed. New extraction: if LAYERS is absent, parse the layer index from LAYERDEFS's `"<layer_id>:<where_clause>"` prefix.

- **WMS `attr_key` resolves to the `wms_layers` value, not the display name** (PR #16). The React popup-render path keys WMS alias maps by the WMS LAYERS param value (e.g., `topp:states` from `DescribeFeatureType?typename=topp:states`), not by the user-supplied display label (e.g., "US States"). New helper `_resolve_wms_attr_key(wms_layers, fallback_name)` picks the first comma-separated entry from `wms_layers`; falls back to the display name when `wms_layers` is missing. Simpler than the ESRI Image variant — no HTTP fetch, the LAYERS value is already in the user's arg.

### Tests

- **31 new regression tests across the 8 PRs** (suite 930 → 961). Per-PR coverage pins each new contract: per-source-type layer-name normalization (5 in PR #12, 4 in PR #16, 5 in PR #17), case-fold args normalization for the popup-modal path (7 in PR #14) and the render_plugin / add_dynamic_map_layer paths (8 in PR #15), description drift on the 11 `add_*_layer` tools' popup_options (PR #11). Several tests assert structured error envelopes for the collision case (two LLM keys map to the same canonical arg_name).

### Documentation

- Solution documented at `docs/solutions/best-practices/mcp-tool-input-identifier-normalization-server-side-2026-05-21.md` (firoh workspace). The meta-pattern: server-side tolerant normalization for LLM-emitted identifier-name mismatches (case-fold, display-name vs service-name keying), with the escalation rule (description tightening → server-side normalization when the same class fails twice) and the boundary (normalize KEY NAMES against an authoritative registry, NOT VALUES — value corruption stays in edit-modal recovery per the prior carve-out). Cross-link added on the precursor doc `docs/solutions/logic-errors/esri-attribute-variables-key-mismatch-display-vs-service-name-2026-04-17.md` (refreshed via `/ce:compound-refresh`).

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
