"""Standalone MCP test configuration.

The embedded conftest neutralized parent DB autouse fixtures from the
tethysapp test tree. In the standalone there is no parent conftest with
DB state, so no overrides are required.

The one shared concern is ``TETHYSDASH_BASE_URL``: the package default
is an empty string so an unconfigured deployment surfaces a
``backend_not_configured`` envelope instead of silently mis-targeting
``localhost:8080``. Tests that drive ``http_requests.get`` mocks
through ``list_intake_plugins`` / ``_resolve_dynamic_map_layer_plugin``
need a non-empty URL so the envelope guard does not short-circuit
before their mock is invoked. We set a sentinel URL once at session
start; individual tests can monkeypatch it back to ``""`` to exercise
the unset-URL branch.
"""

import pytest

from tethysdash_mcp import mcp_server


@pytest.fixture(autouse=True, scope="session")
def _populate_tethysdash_base_url():
    """Set a non-empty sentinel URL so the backend_not_configured guard
    does not short-circuit tests whose mocks expect an HTTP call.
    """
    original = mcp_server.TETHYSDASH_BASE_URL
    mcp_server.TETHYSDASH_BASE_URL = "http://testserver.invalid/apps/tethysdash"
    yield
    mcp_server.TETHYSDASH_BASE_URL = original
