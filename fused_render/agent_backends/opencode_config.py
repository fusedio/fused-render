"""The config OpenCode children run with, built per spawn and injected through
`OPENCODE_CONFIG_CONTENT` (docs/OPENCODE_BACKEND.md D5, D7, D8, D19, D20).

Inline content MERGES over the user's own global config (verified 1.19.5:
their skills and commands still arrive with content injected), so this file
never touches `OPENCODE_CONFIG_DIR` or the user's `opencode.json`. What it
builds:

* `permission` — the approval policy, from the fused mode (D5), or every
  known tool denied for a one-shot that must not act (the recap, D12/D16);
* `model` — `<provider>/<id>` (D7), when the spawn names one;
* `instructions` — the appended system prompt's path (D8);
* `provider.fused` — the local gateway as an OpenAI-compatible provider
  (D20), listing the installed local models, ONLY when there is at least
  one AND the server published its origin AND a session key was minted (the
  config names the key as `{env:FUSED_RENDER_V1_KEY}`; the spawn env
  carries the value).

STDLIB ONLY and importable by `claude_agent/agent.py` (loaded by path from
processes where the package is not importable — the session host) as well
as by the health probe and the recap in the server: the two app facts it
needs (the local catalog, the gateway's key mint) are imported lazily and
absent ones read as "no local models" / "no key". Never raises for them.

This is one of the five files allowed to name OpenCode's own config dir
(`~/.config/opencode` / `OPENCODE_CONFIG_DIR`) — pinned by a grep test.
"""
from __future__ import annotations

import os
from typing import Optional

#: Every tool name OpenCode 1.19.5 gates through `permission` (its own
#: built-ins; `patch` is the older edit tool's name). `deny_all_tools` lists
#: them all so a run cannot act — read-only tools included: the text it is
#: handed is untrusted, and a `read` would carry the server's cwd into the
#: model's context (GEMINI D16's rule).
KNOWN_TOOLS = ("bash", "edit", "write", "read", "glob", "grep", "list",
               "webfetch", "websearch", "todowrite", "task", "patch")

#: The fused modes → the `permission` block (D5). Keyed by BOTH the page's
#: spelling (`prompt`) and the CLI-wire one `_start` records (`default`).
PERMISSIONS = {
    "prompt": {"edit": "ask", "bash": "ask", "webfetch": "ask"},
    "default": {"edit": "ask", "bash": "ask", "webfetch": "ask"},
    "acceptEdits": {"edit": "allow", "bash": "ask", "webfetch": "ask"},
    "auto": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
}

#: The provider id local models ride under; a model id is `fused/<catalog id>`.
PROVIDER_ID = "fused"
PROVIDER_NPM = "@ai-sdk/openai-compatible"
PROVIDER_NAME = "Fused Render (local)"
#: The env var the config names for the provider's key (`{env:...}` is
#: OpenCode's own substitution syntax) and the spawn env must carry.
KEY_ENV = "FUSED_RENDER_V1_KEY"
#: The env var that carries the injected config (D19).
CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"


def config_dir() -> str:
    """OpenCode's OWN config dir — where it reads `opencode.json`, `skills/`
    and `commands/` from: `OPENCODE_CONFIG_DIR` when set, else
    `<XDG_CONFIG_HOME|~/.config>/opencode`. Read-only for fused (D19): the
    skills reader lists it; nothing here writes into it."""
    override = os.environ.get("OPENCODE_CONFIG_DIR")
    if override:
        return os.path.expanduser(override)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = os.path.expanduser(xdg) if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "opencode")


def local_models() -> list:
    """The installed local models the gateway can serve to an agent, as
    `[{id, label, context, max_output, tools: bool}]` from
    `fused_render.ai.catalog.local_agent_models()` — or `[]` when the
    package, the function or the catalog is not there (the host process,
    a build without local inference). Never raises."""
    try:
        from fused_render.ai import catalog  # noqa: PLC0415 - lazy by design
    except Exception:  # noqa: BLE001 - not importable here
        return []
    getter = getattr(catalog, "local_agent_models", None)
    if not callable(getter):
        return []
    try:
        rows = getter()
    except Exception:  # noqa: BLE001 - a catalog fault is "no local models"
        return []
    return [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]


def session_key() -> Optional[str]:
    """A per-spawn key the gateway accepts on `/v1` (`gateway_keys.
    mint_session_key`, docs/LOCAL_MODEL_GATEWAY.md), or None when the mint
    is not available. Never raises."""
    try:
        from fused_render.ai import gateway_keys  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    mint = getattr(gateway_keys, "mint_session_key", None)
    if not callable(mint):
        return None
    try:
        key = mint()
    except Exception:  # noqa: BLE001
        return None
    return str(key) if key else None


def release_key(key: Optional[str]) -> None:
    """Forget a minted key once its spawn is over. Never raises."""
    if not key:
        return
    try:
        from fused_render.ai import gateway_keys  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    release = getattr(gateway_keys, "release_key", None)
    if not callable(release):
        return
    try:
        release(key)
    except Exception:  # noqa: BLE001
        pass


def _model_row(row: dict) -> dict:
    out = {"name": f"{row.get('label') or row['id']} (local)"}
    limit = {}
    try:
        if row.get("context"):
            limit["context"] = int(row["context"])
        if row.get("max_output"):
            limit["output"] = int(row["max_output"])
    except (TypeError, ValueError):
        limit = {}
    if limit:
        out["limit"] = limit
    # A chat template with no tools branch: OpenCode disables tools for the
    # row instead of sending calls the model cannot make (D20, assumed flag).
    if row.get("tools") is False:
        out["tool_call"] = False
    return out


def build(*, model: Optional[str], mode: str, instructions_path: Optional[str],
          local: list, origin: Optional[str], key: Optional[str],
          deny_all_tools: bool = False,
          mcp_timeout_ms: Optional[int] = None) -> dict:
    """The injected config, as a dict (`json.dumps` it onto `CONTENT_ENV`).

    `mode` is a fused mode (`prompt` | `default` | `acceptEdits` | `auto`);
    an unknown one reads as Ask, the safest. `deny_all_tools` overrides it
    with every `KNOWN_TOOLS` entry denied. `local` is `local_models()`'s
    list; the `fused` provider is written only when it is non-empty AND
    `origin` AND `key` are given (no origin: nothing to call; no key: the
    `{env:...}` reference would resolve to nothing and every call would 401)."""
    if deny_all_tools:
        permission = {tool: "deny" for tool in KNOWN_TOOLS}
    else:
        permission = dict(PERMISSIONS.get(mode or "prompt") or PERMISSIONS["prompt"])
    config: dict = {"permission": permission}
    if mcp_timeout_ms:
        # OpenCode's one MCP call ceiling (`experimental.mcp_timeout`, ms): the
        # ACP `mcpServers` list the host hands over carries no per-server
        # timeout, so this global is the only knob. Set for a canvas session,
        # whose `canvas_screenshot` outlasts the default (D1471).
        config["experimental"] = {"mcp_timeout": int(mcp_timeout_ms)}
    if model:
        config["model"] = str(model)
    if instructions_path:
        config["instructions"] = [str(instructions_path)]
    if local and origin and key:
        models = {str(r["id"]): _model_row(r) for r in local
                  if isinstance(r, dict) and r.get("id")}
        if models:
            config["provider"] = {PROVIDER_ID: {
                "npm": PROVIDER_NPM,
                "name": PROVIDER_NAME,
                "options": {"baseURL": str(origin).rstrip("/") + "/v1",
                            "apiKey": "{env:%s}" % KEY_ENV},
                "models": models,
            }}
    return config
