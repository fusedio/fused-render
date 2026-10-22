# Derived from openfused (MIT, Copyright (c) 2026 Fused Labs, Inc.); see NOTICE.
"""Lazy loader for the opt-in `mcp` package (`pip install 'fused-render[fused]'`).

`fused-render app serve` builds a `mcp.server.fastmcp.FastMCP` stdio server
(`app_mcp.py`). `mcp` is not a core dependency, so it is imported here rather
than at column 0, and its absence raises `McpExtraMissingError` (a
`ModuleNotFoundError`) whose message names the extra.

Only a `ModuleNotFoundError` for the top-level `mcp` package means "extra
missing". Anything else, such as mcp 2.x dropping `mcp.server.fastmcp`
(`exc.name == "mcp.server.fastmcp"`) or a broken transitive dependency,
propagates as itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

MCP_EXTRA_REQUIRED_MSG = (
    "This requires the `mcp` extra (used by `fused-render app serve`). "
    "Install it with:  pip install 'fused-render[fused]'"
)


class McpExtraMissingError(ModuleNotFoundError):
    """The `mcp` package is not installed; the message says how to install it."""

    def __init__(self) -> None:
        super().__init__(MCP_EXTRA_REQUIRED_MSG, name="mcp")


def load_fastmcp() -> type[FastMCP]:
    """Return the `FastMCP` server class."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ModuleNotFoundError as exc:
        if exc.name != "mcp":
            raise
        raise McpExtraMissingError() from exc
    return FastMCP
