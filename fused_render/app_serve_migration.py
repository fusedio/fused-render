"""Rewrites the MCP entries the panel registered as `fused app serve <dir>`.

The panel used to register `{command: <home>/fused-bin/fused, args: ["app",
"serve", <dir>]}` in the user's Claude config. App tools are now served by
`fused-render app serve <dir>`, so an old entry would run a command that no
longer serves them. At startup every user-scope entry that names this home's
`fused` wrapper with that argv shape is rewritten to the `fused-render`
wrapper; its other fields (env, type) are kept.

Entries are read with `claude_config.mcp.user_servers` (which honours
`CLAUDE_CONFIG_DIR`) and written through `claude_config.mcp` (`edit`), which
owns every write to that file via the `claude mcp` CLI and restores the old
entry if the new one is refused. Idempotent: a rewritten entry no longer names the old
wrapper.
"""
from __future__ import annotations

import json
import logging
import os
import threading

from fused_render import fusedcli
from fused_render.claude_config import lib, mcp

logger = logging.getLogger(__name__)


def _old_command() -> str:
    from fused_render.shell.storage import home_dir

    return os.path.join(home_dir(), fusedcli.CLI_BIN_SUBDIR, "fused")


def _is_old_entry(spec: object, old_command: str) -> bool:
    if not isinstance(spec, dict) or spec.get("command") != old_command:
        return False
    args = spec.get("args")
    return isinstance(args, list) and len(args) == 3 and args[:2] == ["app", "serve"]


def migrate(new_command: str) -> list[str]:
    """Rewrite this home's `fused app serve` entries to `new_command`; returns
    the names rewritten. Never raises."""
    old_command = _old_command()
    rewritten: list[str] = []
    for name, spec in mcp.user_servers().items():
        if not _is_old_entry(spec, old_command):
            continue
        try:
            out = mcp.main(action="edit", name=name,
                           json=json.dumps({**spec, "command": new_command}))
        except Exception:  # noqa: BLE001 — a failed migration must not stop startup
            logger.warning("could not migrate MCP entry %s", name, exc_info=True)
            continue
        if out.get("ok"):
            rewritten.append(name)
        else:
            logger.warning("could not migrate MCP entry %s: %s", name,
                           out.get("error") or out.get("stderr"))
    return rewritten


def start(new_command: str | None) -> None:
    """Run `migrate` off the startup path (the `claude` CLI takes seconds), and
    only when there is an old entry to rewrite."""
    if not new_command:
        return
    old_command = _old_command()
    if not any(_is_old_entry(s, old_command) for s in mcp.user_servers().values()):
        return
    if not lib.claude_available():
        # Without the CLI nothing can be rewritten; the entry stays old and the
        # next startup looks again, at the cost of one PATH lookup.
        logger.info("claude CLI not found; old `fused app serve` entries stay as they are")
        return
    threading.Thread(target=migrate, args=(new_command,), daemon=True,
                     name="app-serve-migration").start()
