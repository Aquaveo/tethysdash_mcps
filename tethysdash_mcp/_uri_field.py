"""Pydantic Field-factory helpers for the MCP result-by-reference protocol.

Plan: docs/plans/2026-05-18-002-feat-mcp-result-by-reference-protocol-plan.md
(in the firoh workspace) — Unit 5.

A tool opts in to receiving cache URIs by declaring an optional
``*_uri`` arg alongside its primary inline-data arg. The factory below
gives every consumer a consistent Pydantic Field shape:

  - Scalar OR list of cache URIs
  - Regex-validated `mcp+cache://<conv-id>/<token>` scheme
  - Max-length capped to prevent log-injection / abuse

After chatbox-core's substitution layer (Unit 3) runs, the server tool
always sees the corresponding inline arg populated (e.g. ``data_uri``
is resolved to ``data: [...]`` before dispatch). The server only ever
sees a literal ``data_uri`` value when an unmediated MCP client
bypasses chatbox-core — that path is rejected with a clear error.

The companion validator ``ensure_exactly_one_set`` enforces the
exactly-one-of contract in the tool body, where Pydantic's schema
already permits either field to be Optional but doesn't enforce the
mutual-exclusion constraint.
"""

from __future__ import annotations

from typing import Any, List, Optional, Union

from pydantic import Field
from typing_extensions import Annotated


# Pydantic regex pattern for cache URIs minted by chatbox-core. Matches:
#   mcp+cache://<conv-id, 1-64 url-safe chars>/<token, 8-16 url-safe chars>
# Length cap (~128 chars total) prevents log-injection or arbitrary
# string payloads from sneaking through this field.
CACHE_URI_PATTERN = r"^mcp\+cache://[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{8,16}$"


def uri_field(
    *,
    inline_arg_name: str,
    description_suffix: str = "",
) -> Any:
    """Build the standard Pydantic Field annotation for a ``*_uri`` opt-in arg.

    The returned annotation is suitable for declaring a tool argument
    like::

        data_uri: Annotated[
            Optional[Union[str, List[str]]],
            uri_field(inline_arg_name="data"),
        ] = None

    Parameters
    ----------
    inline_arg_name:
        The corresponding inline arg this URI resolves into (e.g., ``"data"``
        for ``data_uri``). Used in the Field description so the LLM
        knows which slot the substitution fills.
    description_suffix:
        Optional per-tool addendum to the standard description (e.g., a
        note about array form when the tool accepts ``list[str]``).
    """
    desc = (
        f"Optional MCP result-by-reference URI (mcp+cache://...) that "
        f"chatbox-core substitutes into `{inline_arg_name}` before dispatch. "
        f"Pass this in PLACE OF `{inline_arg_name}` when the data came from "
        f"a prior tool call in the same conversation — chatbox-core surfaces "
        f"a `_cache_uri` field on every oversized tool result that you can "
        f"emit here. Eliminates token-by-token regeneration of large arrays. "
        f"Use `{inline_arg_name}` directly when you have inline data or no "
        f"cache hit. Setting BOTH this and `{inline_arg_name}` is a bug — "
        f"chatbox-core's substitution prefers the URI and drops the inline "
        f"value, but tool-side validation will reject both-set when the "
        f"call reaches the server unmediated."
    )
    if description_suffix:
        desc = desc + " " + description_suffix
    return Field(
        default=None,
        description=desc,
        # Pattern applies element-wise to a list per Pydantic, so this
        # gates both the scalar and list-of-strings forms.
        pattern=CACHE_URI_PATTERN,
        max_length=128,
    )


def ensure_exactly_one_set(
    inline_value: Any,
    inline_name: str,
    uri_value: Any,
    uri_name: str,
) -> Optional[str]:
    """Validate the exactly-one-set contract between an inline arg and its URI sibling.

    Returns ``None`` when exactly one of the two is non-empty (the call
    is valid); returns an LLM-facing error message string when either both
    are set or neither is set.

    Should run AFTER chatbox-core's substitution layer would have resolved
    a URI into the inline slot — so by the time this runs server-side,
    only one of the two should be present.
    """
    inline_present = _is_non_empty(inline_value)
    uri_present = _is_non_empty(uri_value)

    if inline_present and uri_present:
        return (
            f"invalid_args: both `{inline_name}` and `{uri_name}` were set "
            f"on the same call. Pass EITHER `{inline_name}` (inline data) "
            f"OR `{uri_name}` (mcp+cache:// URI from a prior tool result) "
            f"but not both. If you intended the URI form, drop "
            f"`{inline_name}` from your call; chatbox-core resolves "
            f"`{uri_name}` into `{inline_name}` automatically before "
            f"dispatch."
        )
    if not inline_present and not uri_present:
        return (
            f"invalid_args: neither `{inline_name}` nor `{uri_name}` was "
            f"provided. Pass `{inline_name}` with inline data (an array of "
            f"the expected shape) OR `{uri_name}` with an mcp+cache:// URI "
            f"from a prior tool result in this conversation."
        )
    return None


def _is_non_empty(value: Any) -> bool:
    """True when `value` is something other than None and a non-empty
    collection / string. Mirrors the existing ``min_length=1`` Pydantic
    invariant on the inline ``data`` field so the URI path preserves the
    empty-array protection.
    """
    if value is None:
        return False
    if isinstance(value, (list, str)) and len(value) == 0:
        return False
    return True
