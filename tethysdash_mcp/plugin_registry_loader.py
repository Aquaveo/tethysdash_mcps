"""Shared loader for the runtime plugin registry.

In the embedded tethysdash server, the registry lives under
``reactapp/generated/runtimePluginRegistry.json`` (synced from browser
state). In this standalone package there is no React frontend tree to
walk into, so the path defaults to ``/tmp/runtimePluginRegistry.json``
and is overridable via the ``TETHYSDASH_RUNTIME_REGISTRY_PATH`` env var.

Local-dev consequence: with the ``/tmp`` default, registrations survive
within a single session but not across reboots. Operators that need
persistent registration should set ``TETHYSDASH_RUNTIME_REGISTRY_PATH``
to a durable path (e.g., a mounted volume in the container) before
starting the server.

Returned shape: ``List[Dict[str, Any]]`` — a list of plugin entries, NOT
a dict keyed by source. Consumers that need lookup by source should
iterate or build their own lookup.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

LOGGER = logging.getLogger(__name__)

_RUNTIME_REGISTRY_PATH = os.environ.get(
    "TETHYSDASH_RUNTIME_REGISTRY_PATH",
    "/tmp/runtimePluginRegistry.json",
)


def load_runtime_plugin_registry() -> List[Dict[str, Any]]:
    """Load the runtime plugin registry synced from browser localStorage."""
    try:
        with open(_RUNTIME_REGISTRY_PATH, "r") as f:
            registry = json.load(f)
        LOGGER.info(
            "Loaded %d runtime plugin(s) from %s",
            len(registry),
            _RUNTIME_REGISTRY_PATH,
        )
        return registry
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as e:
        LOGGER.warning("Invalid JSON in runtime plugin registry: %s", e)
        return []


def save_runtime_plugin_registry(plugins: List[Dict[str, Any]]) -> None:
    """Persist the runtime plugin registry list to ``_RUNTIME_REGISTRY_PATH``.

    Creates the parent directory if needed. Overwrites any existing file.
    The path is the ``/tmp`` default or the operator-supplied
    ``TETHYSDASH_RUNTIME_REGISTRY_PATH`` override (see module docstring).
    """
    os.makedirs(os.path.dirname(_RUNTIME_REGISTRY_PATH), exist_ok=True)
    with open(_RUNTIME_REGISTRY_PATH, "w") as f:
        json.dump(plugins, f, indent=2)
    LOGGER.info(
        "Wrote %d runtime plugin(s) to %s",
        len(plugins),
        _RUNTIME_REGISTRY_PATH,
    )
