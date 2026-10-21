"""MCP-server management endpoints (mcp.md).

Every action delegates to the `claude mcp` CLI (mcp.md §1) — this module never reads
or writes ~/.claude.json directly. The CLI owns that file (config-store.md §6).

main(action=...):
  list   -> {ok, servers:[{name,endpoint,transport,status,kind,connected,
                           needsAuth,canAuth,removable}], stale, checkedAt}
            | {ok:false, error}                                (mcp.md §2)
           `claude mcp list` health-checks every server and takes ~11 s, so a cached
           list is returned at once with stale=true (and a background refresh is
           started); `checkedAt` is the epoch second the cache was taken. With no
           cache it blocks on the CLI as before (stale=false).
           params: fresh (bool or "1"/"true") -> join/run the refresh and return it
           (stale=false). Concurrent refreshes share ONE CLI run.
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
import os
import threading
import time

from . import lib

_LIST_TIMEOUT = 28

_lock = threading.Lock()
_inflight = None
# Bumped by every add/remove/logout so a refresh that started before the change
# cannot write its (now outdated) answer back over the cleared cache.
_generation = 0


class _Flight:
    def __init__(self):
        self.done = threading.Event()
        self.result = {"ok": False, "error": "failed to list MCP servers"}


def _cache_path() -> str:
    return os.path.join(lib.home_dir(), "claude_config", "mcp_list_cache.json")


def _read_cache():
    try:
        data = lib.read_json(_cache_path(), None)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("servers"), list):
        return data
    return None


def _invalidate() -> None:
    global _generation
    with _lock:
        _generation += 1
    try:
        os.remove(_cache_path())
    except OSError:
        pass


def _refresh(generation=None) -> dict:
    res = lib.claude_cli("mcp", "list", timeout=_LIST_TIMEOUT)
    if not res["ok"]:
        return {"ok": False, "error": res.get("stderr") or "failed to list MCP servers"}
    servers = lib.parse_mcp_list(res["stdout"])
    checked = time.time()
    with _lock:
        current = generation is None or generation == _generation
        if current:
            try:
                lib.write_json(_cache_path(), {"servers": servers, "checkedAt": checked})
            except OSError:
                pass
    return {"ok": True, "servers": servers, "checkedAt": checked, "stale": False}


def _run(flight: _Flight, generation: int) -> None:
    global _inflight
    try:
        flight.result = _refresh(generation)
    except Exception as exc:  # noqa: BLE001 — the waiter gets the message, not a hang
        flight.result = {"ok": False, "error": str(exc) or "failed to list MCP servers"}
    finally:
        with _lock:
            if _inflight is flight:
                _inflight = None
        flight.done.set()


def _flight() -> _Flight:
    """The refresh in flight, started if there is none (single-flight)."""
    global _inflight
    with _lock:
        if _inflight is not None:
            return _inflight
        flight = _inflight = _Flight()
        generation = _generation
    threading.Thread(target=_run, args=(flight, generation), daemon=True,
                     name="mcp-list-refresh").start()
    return flight


def _wait_idle(timeout: float = 30) -> None:
    """Block until no refresh is in flight (tests)."""
    with _lock:
        flight = _inflight
    if flight is not None:
        flight.done.wait(timeout)


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return bool(value)


def _bad_name(name: str) -> bool:
    # argv-array safety (mcp.md §3): there is no shell, so COMMAND injection is
    # impossible — but that is not the same as "anything is safe to pass". The
    # name arrives from the client and is echoed back from `claude mcp list`
    # output we parse, and argv[n] is still parsed as options by the program
    # receiving it, so a leading dash makes it a flag rather than a server
    # (lib.option_shaped). Reject that along with the empty and control-char
    # names that could only be a mistake.
    return not name or lib.option_shaped(name) or any(ord(c) < 0x20 for c in name)


def main(action: str = "list", name: str = "", **kwargs) -> dict:
    if action == "list":
        if _truthy(kwargs.get("fresh")):
            flight = _flight()
            flight.done.wait(_LIST_TIMEOUT + 5)
            return flight.result
        cached = _read_cache()
        if cached is None:
            flight = _flight()
            flight.done.wait(_LIST_TIMEOUT + 5)
            return flight.result
        _flight()
        return {"ok": True, "servers": cached["servers"],
                "checkedAt": cached.get("checkedAt"), "stale": True}

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
        if res["ok"]:
            _invalidate()
        return {"ok": res["ok"], "name": name,
                "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}

    if action == "remove":
        if _bad_name(name):
            return {"ok": False, "error": "invalid server name"}
        res = lib.claude_cli("mcp", "remove", name, "--scope", "user")
        if res["ok"]:
            _invalidate()
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
        if res["ok"]:
            _invalidate()
        return {"ok": res["ok"], "name": name,
                "stdout": res.get("stdout", ""), "stderr": res.get("stderr", "")}

    return {"ok": False, "error": f"unknown action: {action}"}
