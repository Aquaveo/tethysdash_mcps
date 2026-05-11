"""R7 LLM-editable-path whitelist — Python loader.

Canonical source: ``reactapp/config/editableSchemas.json`` — the single
source of truth consumed by both JS (``editableSchemas.js``) and Python.
Because both sides load the same file, JS/Python parity is enforced by
construction, not by a drift-detection test.

Format::

    {"<viz source name>": ["<JSON Pointer prefix>", ...]}

Matching semantics (see :func:`is_path_allowed`): a path ``P`` is allowed
for a given source if, for any prefix ``P_i`` in the list, ``P == P_i`` OR
``P`` starts with ``P_i + "/"``. Structural segment match — RFC 6901
literal dots in segment names (e.g.,
``variable_options_source.metadata``) are preserved as single segments; do
not split on ``.``.

Not in scope this iteration: Text, Custom Image, ``render_plugin`` viz
types, ``render_custom_visualization``. These fall through to
``whitelist_rejected`` — the desired fail-closed behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

# Resolve the JSON path relative to this module. The schema travels with
# the package — copied into ``tethysdash_mcp/data/editableSchemas.json`` at
# extract time from the canonical source at
# ``tethysapp-tethys_dash/reactapp/config/editableSchemas.json``. Rewritten
# from the embedded server's ``parents[2] / reactapp / config / ...`` walk
# because the standalone repo has no React frontend tree to walk into.
_JSON_PATH = (
    Path(__file__).resolve().parent
    / "data"
    / "editableSchemas.json"
)

try:
    with open(_JSON_PATH, encoding="utf-8") as _f:
        LLM_EDITABLE_PATHS: Dict[str, List[str]] = json.load(_f)
except FileNotFoundError as _exc:
    # The JSON is shipped inside the package under ``tethysdash_mcp/data/``.
    # If it is missing, the package was assembled incorrectly during the
    # extract; restore the file from the canonical source in the tethysdash
    # repo before running the server.
    raise RuntimeError(
        f"Missing LLM-editable-path whitelist at {_JSON_PATH!s}. "
        f"This file is the single source of truth for the JS and Python "
        f"sides of the patch_visualization whitelist. The canonical source "
        f"lives at tethysapp-tethys_dash/reactapp/config/editableSchemas.json "
        f"and is copied into this package at extract time. Restore it from "
        f"that source if missing here."
    ) from _exc


def is_path_allowed(source: str, json_pointer: str) -> bool:
    """Return True if ``json_pointer`` is whitelisted for the given viz ``source``.

    Uses structural prefix matching: ``P`` is allowed if it equals a prefix
    or starts with ``prefix + "/"``. Does NOT split on ``.`` (RFC 6901).
    """
    prefixes = LLM_EDITABLE_PATHS.get(source)
    if not prefixes:
        return False
    for prefix in prefixes:
        if json_pointer == prefix:
            return True
        if json_pointer.startswith(prefix + "/"):
            return True
    return False


def validate_path_against_whitelist(source: str, json_pointer: str) -> None:
    """Raise ``ValueError`` if ``json_pointer`` is NOT whitelisted for ``source``.

    Callers can convert the raised exception into a structured
    ``whitelist_rejected`` error per the MCP error contract.
    """
    if not is_path_allowed(source, json_pointer):
        raise ValueError(
            f"whitelist_rejected: path {json_pointer!r} is not editable "
            f"for viz source {source!r}"
        )
