"""Tests for the HTTP-based runtime plugin registry reader.

Replaces the deleted ``test_plugin_registry_loader.py`` (filesystem-coupled
loader) per plan 2026-05-11-006. The standalone server now reads the
registry from tethysdash over HTTP via ``${TETHYSDASH_BASE_URL}/runtime-
plugins/list/``. These tests pin the silent-recovery contract: every failure
mode returns an empty list, never raises, never crashes an unrelated tool
call. Failures are logged at warning level so operators have a breadcrumb.

The feature-flagged ``register_runtime_plugin`` returning the
``registration_not_supported`` envelope is also covered here -- it's the
same architectural decision (the standalone has no authenticated write
path until plan 004 revives).
"""

from unittest.mock import patch, MagicMock

import pytest
import requests

from tethysdash_mcp import mcp_server


@pytest.fixture
def base_url_set(monkeypatch):
    """Pin TETHYSDASH_BASE_URL to a sentinel so the helper attempts HTTP.

    The session-scoped conftest fixture already sets a sentinel; this
    fixture is here for explicit per-test scoping when needed.
    """
    monkeypatch.setattr(
        mcp_server, "TETHYSDASH_BASE_URL", "http://testserver.invalid/apps/tethysdash"
    )


@pytest.fixture
def base_url_empty(monkeypatch):
    """Force TETHYSDASH_BASE_URL to empty so the helper short-circuits."""
    monkeypatch.setattr(mcp_server, "TETHYSDASH_BASE_URL", "")


# ---------------------------------------------------------------------------
# _load_runtime_plugin_registry_http behavior
# ---------------------------------------------------------------------------


def test_http_loader_empty_base_url_returns_empty_list(base_url_empty):
    """When TETHYSDASH_BASE_URL is unset, return [] without making any
    HTTP call. Matches the embedded loader's behavior when no registry
    file existed."""
    with patch.object(mcp_server, "http_requests") as mock_http:
        result = mcp_server._load_runtime_plugin_registry_http()
        assert result == []
        mock_http.get.assert_not_called()


def test_http_loader_happy_path_returns_list(base_url_set):
    """200 + JSON array payload → returns the array unchanged."""
    fake_registry = [
        {"source": "Echo", "scope": "echo", "module": "./Echo", "url": "http://x"},
        {"source": "Beep", "scope": "beep", "module": "./Beep", "url": "http://y"},
    ]
    mock_response = MagicMock()
    mock_response.json.return_value = fake_registry
    mock_response.raise_for_status.return_value = None

    with patch.object(mcp_server.http_requests, "get", return_value=mock_response) as mock_get:
        result = mcp_server._load_runtime_plugin_registry_http()

    assert result == fake_registry
    mock_get.assert_called_once()
    call_args = mock_get.call_args
    assert "/runtime-plugins/list/" in call_args[0][0]
    # allow_redirects=False protects against tethysdash auth redirects
    assert call_args.kwargs.get("allow_redirects") is False


def test_http_loader_empty_registry_returns_empty_list(base_url_set):
    """Empty registry on the server side (200 + []) → []."""
    mock_response = MagicMock()
    mock_response.json.return_value = []
    mock_response.raise_for_status.return_value = None

    with patch.object(mcp_server.http_requests, "get", return_value=mock_response):
        result = mcp_server._load_runtime_plugin_registry_http()

    assert result == []


def test_http_loader_connection_error_returns_empty_list(base_url_set, caplog):
    """Network unreachable → [] + warning log. Must NOT raise."""
    with patch.object(
        mcp_server.http_requests,
        "get",
        side_effect=requests.ConnectionError("refused"),
    ):
        with caplog.at_level("WARNING"):
            result = mcp_server._load_runtime_plugin_registry_http()

    assert result == []
    assert any(
        "Failed to fetch runtime plugin registry" in r.message for r in caplog.records
    )


def test_http_loader_5xx_returns_empty_list(base_url_set, caplog):
    """Server 500 → raise_for_status raises HTTPError → caught → []."""
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = requests.HTTPError("500 Server Error")

    with patch.object(mcp_server.http_requests, "get", return_value=mock_response):
        with caplog.at_level("WARNING"):
            result = mcp_server._load_runtime_plugin_registry_http()

    assert result == []
    assert any("Failed to fetch" in r.message for r in caplog.records)


def test_http_loader_non_json_body_returns_empty_list(base_url_set, caplog):
    """200 OK + non-JSON body (e.g., a tethysdash login-redirect HTML
    page) → []. Defends against the silent-success failure mode where a
    misconfigured tethysdash would otherwise look like an empty registry."""
    mock_response = MagicMock()
    mock_response.json.side_effect = ValueError("Expecting value")
    mock_response.raise_for_status.return_value = None

    with patch.object(mcp_server.http_requests, "get", return_value=mock_response):
        with caplog.at_level("WARNING"):
            result = mcp_server._load_runtime_plugin_registry_http()

    assert result == []
    assert any("Failed to fetch" in r.message for r in caplog.records)


def test_http_loader_non_list_payload_returns_empty_list(base_url_set, caplog):
    """200 OK + JSON dict (instead of a list) → [] + warning. Pins the
    response-shape contract: a misimplemented endpoint that returned a
    dict would otherwise be silently consumed and produce confusing
    AttributeErrors downstream."""
    mock_response = MagicMock()
    mock_response.json.return_value = {"plugins": []}  # wrong shape
    mock_response.raise_for_status.return_value = None

    with patch.object(mcp_server.http_requests, "get", return_value=mock_response):
        with caplog.at_level("WARNING"):
            result = mcp_server._load_runtime_plugin_registry_http()

    assert result == []
    assert any("non-list payload" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _get_all_plugins delegates to the HTTP loader
# ---------------------------------------------------------------------------


def test_get_all_plugins_delegates_to_http_loader(base_url_set):
    """The public _get_all_plugins seam still works -- it just routes
    through the HTTP loader now. Existing tests that patch _get_all_plugins
    keep working unchanged."""
    fake_registry = [{"source": "X", "scope": "x", "module": "./X", "url": "http://x"}]
    with patch.object(
        mcp_server, "_load_runtime_plugin_registry_http", return_value=fake_registry
    ) as mock_loader:
        result = mcp_server._get_all_plugins()

    assert result == fake_registry
    mock_loader.assert_called_once()


# ---------------------------------------------------------------------------
# register_runtime_plugin returns the not-supported envelope
# ---------------------------------------------------------------------------


def test_register_runtime_plugin_returns_not_supported_envelope():
    """The standalone has no authenticated write path until plan 004
    revives. register_runtime_plugin returns a structured envelope rather
    than attempting any write or HTTP call."""
    with patch.object(mcp_server, "http_requests") as mock_http:
        result = mcp_server.register_runtime_plugin(
            url="http://example.com/remoteEntry.js",
            scope="MyScope",
            module="./MyPanel",
            label="My Panel",
        )

    assert result == {
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
    # No HTTP call attempted
    mock_http.get.assert_not_called()
    if hasattr(mock_http, "post"):
        mock_http.post.assert_not_called()


# Note: the "tool stays registered in the MCP surface" invariant is already
# enforced by test_prompts.py::test_prompt_target_tool_is_visible_in_default_list_tools
# (PROMPT_TO_TOOL includes "register_runtime_plugin"). No duplicate test here.
