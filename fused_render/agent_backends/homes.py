"""Where each agent harness keeps its own configuration, and which parts of it
the Agents page can edit.

Claude's home is `claude_config.lib.CLAUDE_DIR` (env `CLAUDE_DIR` or
`~/.claude`, fixed at import). Codex reads `CODEX_HOME` or `~/.codex`; Copilot
reads `COPILOT_HOME` or `~/.copilot` — the same resolution the health probes
use (`codex_health.py`, `copilot_health.py`).

`CAPABILITIES` is the tab list the Agents page draws per harness. Claude's
tabs are backed by the dedicated `claude_config` modules; Codex and Copilot
get plain-file tabs: `instructions` edits the harness's global instructions
file, `settings` its raw config file, `mcp` its MCP server table.
"""
from __future__ import annotations

import os

from fused_render.claude_config import lib

HARNESSES = ("claude", "copilot", "codex")

CAPABILITIES: dict[str, tuple[str, ...]] = {
    "claude": ("settings", "skills", "memory", "plugins", "mcp", "history"),
    "codex": ("instructions", "mcp", "settings"),
    "copilot": ("instructions", "mcp", "settings"),
}

# capability -> file name under the harness home, for the plain-file tabs.
FILES: dict[str, dict[str, str]] = {
    "codex": {"instructions": "AGENTS.md", "settings": "config.toml"},
    "copilot": {"instructions": "copilot-instructions.md", "settings": "config.json"},
}

MCP_FILES = {"codex": "config.toml", "copilot": "mcp-config.json"}


def home_for(harness: str) -> str:
    if harness == "claude":
        return lib.CLAUDE_DIR
    if harness == "codex":
        return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    if harness == "copilot":
        return os.environ.get("COPILOT_HOME") or os.path.expanduser("~/.copilot")
    raise ValueError(f"unknown harness: {harness!r}")


def describe() -> list[dict]:
    return [
        {
            "id": h,
            "home": home_for(h),
            "exists": os.path.isdir(home_for(h)),
            "capabilities": list(CAPABILITIES[h]),
        }
        for h in HARNESSES
    ]
