"""MCP-server management endpoints (mcp.md).

Every action delegates to the `claude mcp` CLI (mcp.md §1) — this module never reads
or writes ~/.claude.json directly. The CLI owns that file (config-store.md §6).

main(action=...):
  list   -> {ok, servers:[{name,endpoint,transport,status,kind,connected,
                           needsAuth,canAuth,removable}]} | {ok:false, error}   (mcp.md §2)
  login  -> {ok, launched, name} | {ok:false, error}         (mcp.md §3, detached OAuth)
           params: name
  logout -> {ok, name, stdout, stderr}                        (mcp.md §4)
           params: name
  remove -> {ok, name, stdout, stderr}                        (mcp.md §4)
           params: name
  add    -> {ok, name, stdout, stderr} | {ok:false, error}    (mcp.md §4)
           params: name, json (JSON-stringified server definition)
"""
import json

from . import lib


def _bad_name(name: str) -> bool:
    # argv-array safety (mcp.md §3): no shell, so injection is impossible; still
    # reject empty / control-char names that could only be a mistake.
    return not name or any(ord(c) < 0x20 for c in name)


def main(action: str = "list", name: str = "", **kwargs) -> dict:
    if action == "list":
        res = lib.claude_cli("mcp", "list", timeout=28)
        if not res["ok"]:
            return {"ok": False, "error": res.get("stderr") or "failed to list MCP servers"}
        return {"ok": True, "servers": lib.parse_mcp_list(res["stdout"])}

    if action == "login":
        # mcp.md §3: fire-and-forget — the CLI opens a browser and blocks past 30s,
        # so spawn it detached and let the user refresh once auth completes.
        if _bad_name(name):
            return {"ok": False, "error": "invalid server name"}
        res = lib.claude_cli_detached("mcp", "login", name)
        if not res["ok"]:
            return {"ok": False, "error": res.get("error") or "could not launch login"}
        return {"ok": True, "launched": True, "name": name}

    if action == "logout":
        if _bad_name(name):
            return {"ok": False, "error": "invalid server name"}
        res = lib.claude_cli("mcp", "logout", name)
        return {"ok": res["ok"], "name": name,
                "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}

    if action == "remove":
        if _bad_name(name):
            return {"ok": False, "error": "invalid server name"}
        res = lib.claude_cli("mcp", "remove", name, "--scope", "user")
        return {"ok": res["ok"], "name": name,
                "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}

    if action == "add":
        if _bad_name(name):
            return {"ok": False, "error": "invalid server name"}
        spec = kwargs.get("json") or ""
        try:
            json.loads(spec)  # validate before delegating
        except ValueError:
            return {"ok": False, "error": "server definition is not valid JSON"}
        res = lib.claude_cli("mcp", "add-json", name, spec, "--scope", "user")
        return {"ok": res["ok"], "name": name,
                "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}

    return {"ok": False, "error": f"unknown action: {action}"}
