"""Standalone-independence guard.

Asserts the standalone tethysdash_mcp server does not pull in any
``tethysapp.*`` or ``django`` modules at import time. If this test
fails, an import rewrite was missed during extraction -- a
``from tethysapp.tethysdash.*`` line slipped in, or a transitive
dependency dragged Django into the standalone (intake's HTTPSource,
pluggy entry points, etc.).

The check runs in a subprocess so we measure a *fresh* interpreter
state. Popping modules from ``sys.modules`` and re-importing in-process
would mutate other test files' bound symbols (e.g. functions imported
``from tethysdash_mcp.mcp_server``), so subprocess isolation both
hardens the assertion and avoids cross-test pollution.

The cost of catching it here is one second. The cost of catching it
in CI on the container build is minutes. The cost of catching it in
production is debugging a Cloud Run image that pulls in the entire
Tethys stack.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap


def _subprocess_module_inventory(target_module: str) -> set[str]:
    """Return the set of tethysapp/django sys.modules entries after a fresh
    import of ``target_module`` in a clean subprocess.
    """
    script = textwrap.dedent(
        f"""
        import importlib
        import sys
        importlib.import_module({target_module!r})
        leaked = sorted(
            k for k in sys.modules
            if k == "tethysapp" or k.startswith("tethysapp.")
            or k == "django" or k.startswith("django.")
        )
        for name in leaked:
            print(name)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    return {line for line in result.stdout.splitlines() if line}


def test_no_tethysapp_or_django_imports_at_module_load():
    leaked = _subprocess_module_inventory("tethysdash_mcp.mcp_server")
    assert not leaked, (
        f"Standalone server pulled in tethysapp/django modules: {sorted(leaked)}. "
        "An import rewrite was missed during extraction, or a transitive "
        "dependency is dragging Django into the standalone."
    )


def test_individual_modules_are_standalone_independent():
    """Each submodule also imports cleanly without tethysapp/django."""
    for mod in (
        "tethysdash_mcp.plugin_helpers",
        "tethysdash_mcp.editable_schemas",
        "tethysdash_mcp.editable_schemas_plugin",
        "tethysdash_mcp.plugin_registry_loader",
        "tethysdash_mcp._input_validation_middleware",
        "tethysdash_mcp._observability_middleware",
    ):
        leaked = _subprocess_module_inventory(mod)
        assert not leaked, (
            f"Submodule {mod!r} pulled in tethysapp/django modules: {sorted(leaked)}."
        )
