"""Codex and Copilot configuration: plain-file editing and MCP servers.

Claude has dedicated modules; these two harnesses keep their configuration in
ordinary files under their own homes (`agent_backends.homes`), so the tabs the
Agents page shows for them share this one module.

files_main(harness, action=...):
  get -> {ok, path, exists, text}   params: kind  ("instructions" | "settings")
  put -> {ok, path}                 params: kind, text

mcp_main(harness, action=...):
  list   -> {ok, servers: [{name, transport, endpoint, spec}], path}
  add    -> {ok}   params: name, json  (a server definition object, as JSON)
  edit   -> {ok}   params: name, json  (replaces the named server)
  remove -> {ok}   params: name

Copilot's servers live under `mcpServers` in `mcp-config.json`; Codex's under
`[mcp_servers.<name>]` tables of `config.toml`. Codex writes are line-level
edits that leave the rest of the file untouched, and are checked by parsing
the result before it replaces the file.
"""
from __future__ import annotations

import json
import os
import re
import tomllib

from fused_render.agent_backends import homes
from . import lib

_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _path(harness: str, kind: str) -> str:
    name = homes.FILES.get(harness, {}).get(kind)
    if not name:
        raise ValueError(f"{harness} has no {kind} file")
    return os.path.join(homes.home_for(harness), name)


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _write(path: str, text: str) -> None:
    lib.write_text(path, text)


def files_main(harness: str, action: str = "get", kind: str = "instructions", text: str = "") -> dict:
    try:
        path = _path(harness, kind)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    if action == "get":
        body = _read(path)
        return {"ok": True, "path": path, "exists": body is not None, "text": body or ""}
    if action == "put":
        if path.endswith(".toml"):
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                return {"ok": False, "error": f"not valid TOML: {e}"}
        if path.endswith(".json") and text.strip():
            try:
                json.loads(text)
            except ValueError as e:
                return {"ok": False, "error": f"not valid JSON: {e}"}
        _write(path, text)
        return {"ok": True, "path": path}
    return {"ok": False, "error": f"unknown action: {action}"}


# -- MCP ---------------------------------------------------------------------


def _summarize(name: str, spec: dict) -> dict:
    url = spec.get("url")
    if url:
        transport = spec.get("type") or "http"
        endpoint = url
    else:
        transport = "stdio"
        endpoint = " ".join([str(spec.get("command", "")), *map(str, spec.get("args") or [])]).strip()
    return {"name": name, "transport": transport, "endpoint": endpoint, "spec": spec}


def _toml_scalar(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings
    if isinstance(v, list):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    raise ValueError(f"unsupported value in server definition: {v!r}")


def _toml_table(name: str, spec: dict) -> str:
    head = f"[mcp_servers.{json.dumps(name)}]" if not re.fullmatch(r"[A-Za-z0-9_-]+", name) else f"[mcp_servers.{name}]"
    lines = [head]
    subs = []
    for k, v in spec.items():
        if isinstance(v, dict):
            subs.append((k, v))
        else:
            lines.append(f"{k} = {_toml_scalar(v)}")
    for k, v in subs:
        lines.append("")
        lines.append(f"{head[:-1]}.{k}]")
        for sk, sv in v.items():
            lines.append(f"{json.dumps(sk)} = {_toml_scalar(sv)}")
    return "\n".join(lines) + "\n"


_TABLE_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _strip_server(text: str, name: str) -> str:
    """Drop the `[mcp_servers.<name>]` table and its subtables, line by line."""
    quoted = json.dumps(name)
    prefixes = (f"mcp_servers.{name}", f"mcp_servers.{quoted}")
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        m = _TABLE_RE.match(line)
        if m:
            header = m.group(1)
            skipping = any(header == p or header.startswith(p + ".") for p in prefixes)
        if not skipping:
            out.append(line)
    return "".join(out).rstrip("\n") + "\n" if out else ""


def _codex_servers(text: str) -> dict:
    return tomllib.loads(text).get("mcp_servers") or {}


def _copilot_doc(path: str) -> dict:
    raw = _read(path)
    doc = json.loads(raw) if raw and raw.strip() else {}
    doc.setdefault("mcpServers", {})
    return doc


def mcp_main(harness: str, action: str = "list", name: str = "", **kwargs) -> dict:
    fname = homes.MCP_FILES.get(harness)
    if not fname:
        return {"ok": False, "error": f"{harness} has no MCP config"}
    path = os.path.join(homes.home_for(harness), fname)
    try:
        if action == "list":
            if harness == "codex":
                servers = _codex_servers(_read(path) or "")
            else:
                servers = _copilot_doc(path)["mcpServers"]
            return {"ok": True, "path": path, "servers": [_summarize(n, s) for n, s in servers.items()]}

        if not name or not _NAME_RE.match(name):
            return {"ok": False, "error": "invalid server name"}
        spec = None
        if action in ("add", "edit"):
            try:
                spec = json.loads(kwargs.get("json") or "")
            except ValueError:
                return {"ok": False, "error": "server definition is not valid JSON"}
            if not isinstance(spec, dict):
                return {"ok": False, "error": "server definition must be a JSON object"}
        elif action != "remove":
            return {"ok": False, "error": f"unknown action: {action}"}

        if harness == "copilot":
            doc = _copilot_doc(path)
            exists = name in doc["mcpServers"]
            if action == "add" and exists:
                return {"ok": False, "error": f"{name} already exists"}
            if action == "remove":
                doc["mcpServers"].pop(name, None)
            else:
                doc["mcpServers"][name] = spec
            lib.write_json(path, doc)
            return {"ok": True, "name": name}

        text = _read(path) or ""
        exists = name in _codex_servers(text)
        if action == "add" and exists:
            return {"ok": False, "error": f"{name} already exists"}
        if action in ("remove", "edit"):
            text = _strip_server(text, name)
        if spec is not None:
            text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + _toml_table(name, spec)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return {"ok": False, "error": f"could not write a valid config.toml: {e}"}
        _write(path, text)
        return {"ok": True, "name": name}
    except (ValueError, tomllib.TOMLDecodeError) as e:
        return {"ok": False, "error": str(e)}
