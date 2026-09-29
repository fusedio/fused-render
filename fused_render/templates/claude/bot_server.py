"""Minimal stdio MCP server `fused_bot`: the tools a Bots-sub-app chat gets.

A bot is a conversational Claude session whose built-in tools are whitelisted
down to read/search/ask (`agent.py` passes `--tools ...` in bot mode), so it
cannot write code or files itself. This server is everything it CAN do beyond
talking:

  * remember / forget / recall — its own durable memory, `<bot>/memory/MEMORY.md`
    (plain markdown; the Bots page edits the same file, so every call re-reads it
    from disk and writes it back atomically rather than caching anything).
  * create_app / edit_app — hand a detailed spec to a BUILDER agent: the server's
    `POST /api/bots/<slug>/tasks` scaffolds a new app (or targets an existing one)
    and starts a task agent on it. The build runs in the background; the bot only
    reports the task.
  * list_apps / task_status — read-backs for the two above.

Spawned by `claude`, never by the app: stdlib only, no `fused_render` import,
no assumption about cwd. argv[1] is the bot directory (its basename IS the slug —
the directory name is minted once at creation and never renamed). HTTP goes to
`FUSED_RENDER_ORIGIN`, which agent.py names explicitly in this server's mcp.json
`env` (the CLI's MCP client passes an allowlist of env vars plus that dict, so an
ambient value would not survive the spawn). Every mutating request carries
`X-Fused: 1`, the server's CSRF gate.

Every tool answers with exactly one text block; a failure is a text block that
starts "Error: " (marked isError) rather than a JSON-RPC error, so the model
reads what went wrong and can tell the user instead of seeing a transport fault.

Framing is newline-delimited JSON-RPC on stdin/stdout (MCP stdio), cloned from
permission_server.py. stdout carries protocol only — diagnostics go to stderr.
"""
import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "fused_bot"

BOT_DIR = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else ""
SLUG = os.path.basename(os.path.normpath(BOT_DIR)) if BOT_DIR else ""
MEMORY_PATH = os.path.join(BOT_DIR, "memory", "MEMORY.md") if BOT_DIR else ""
MEMORY_SEED = "# Memory\n"

# create_app scaffolds a folder (git init, boilerplate commit) and schedules a
# task before it answers, so it is the slow call. Kept under the mcp.json
# per-server `timeout` agent.py writes (BOT_MCP_TIMEOUT there), so a slow
# server surfaces as OUR "Error: ..." text, not the CLI's MCP-timeout fault.
HTTP_TIMEOUT = float(os.environ.get("FUSED_BOT_HTTP_TIMEOUT", "150"))

_stdout_lock = threading.Lock()
# Serialises this process's own read-modify-write of MEMORY.md. Tool calls may
# run on separate threads (see main), and two `remember`s racing would each
# write back a file missing the other's bullet.
_memory_lock = threading.Lock()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _utf8_stdio() -> None:
    """UTF-8 on the wire whatever the locale — same reasoning as
    permission_server._utf8_stdio (Node writes raw UTF-8; Windows would decode
    the pipe as cp1252 and a curly quote in a memory note would kill us)."""
    for stream, extra in ((sys.stdin, {"errors": "replace"}),
                          (sys.stdout, {"newline": "\n"})):
        try:
            stream.reconfigure(encoding="utf-8", **extra)
        except (AttributeError, ValueError, OSError) as exc:
            _log("bot_server.py: could not force UTF-8 stdio (%s)" % exc)


def _send(payload: dict) -> None:
    with _stdout_lock:
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}],
            "isError": text.startswith("Error: ")}


def _str(args: dict, key: str) -> str:
    value = args.get(key)
    return value.strip() if isinstance(value, str) else ""


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------

def _read_memory() -> str:
    try:
        with open(MEMORY_PATH, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return MEMORY_SEED


def _write_memory(text: str) -> None:
    """tmp + os.replace, so the Bots page reading (or PUTting) the file never
    sees half of it."""
    folder = os.path.dirname(MEMORY_PATH)
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".MEMORY.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
        os.replace(tmp, MEMORY_PATH)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _remember(args: dict) -> str:
    text = " ".join(_str(args, "text").split())  # one bullet = one line
    if not text:
        return "Error: `text` is required."
    topic = " ".join(_str(args, "topic").split()) or "Notes"
    bullet = "- " + text
    with _memory_lock:
        lines = _read_memory().splitlines()
        # Dedupe exact lines anywhere in the file: the same fact filed under a
        # second topic is still the same fact, and a bot re-saving what it
        # already knows each session would otherwise grow the file forever.
        if any(line.strip() == bullet for line in lines):
            return "Saved."
        want = "## " + topic
        start = next((i for i, line in enumerate(lines)
                      if line.strip().casefold() == want.casefold()), None)
        if start is None:
            while lines and not lines[-1].strip():
                lines.pop()
            lines += ["", want, bullet]
        else:
            end = next((j for j in range(start + 1, len(lines))
                        if _is_heading(lines[j])), len(lines))
            # Insert after the section's last non-blank line, so the blank line
            # that separates it from the next heading stays where it was.
            at = end
            while at > start + 1 and not lines[at - 1].strip():
                at -= 1
            lines.insert(at, bullet)
        _write_memory("\n".join(lines) + "\n")
    return "Saved."


def _forget(args: dict) -> str:
    match = _str(args, "match")
    if not match:
        return "Error: `match` is required."
    needle = match.casefold()
    with _memory_lock:
        lines = _read_memory().splitlines()
        # Headings are structure, not memories: a `match` that happens to hit
        # "## Notes" must not orphan every bullet under it.
        kept = [line for line in lines
                if _is_heading(line) or needle not in line.casefold()]
        removed = len(lines) - len(kept)
        if removed:
            _write_memory("\n".join(kept) + "\n")
    return "Removed %d line%s." % (removed, "" if removed == 1 else "s")


def _recall(args: dict) -> str:
    query = _str(args, "query")
    text = _read_memory()
    if not query:
        return text if text.strip() and text.strip() != MEMORY_SEED.strip() \
            else "(memory is empty)"
    needle = query.casefold()
    words = [w for w in needle.split() if w]
    out, heading = [], ""
    for line in text.splitlines():
        if _is_heading(line):
            heading = line.strip()
            continue
        low = line.casefold()
        # Whole-phrase first; failing that, any word — a query like "dog name"
        # should still find "- Has a dog called Rex".
        if line.strip() and (needle in low or any(w in low for w in words)):
            out.append("%s%s" % ("[%s] " % heading.lstrip("# ") if heading else "",
                                 line.strip()))
    return "\n".join(out) if out else "No saved memory matches %r." % query


# --------------------------------------------------------------------------
# HTTP to the fused-render server
# --------------------------------------------------------------------------

def _origin() -> str:
    return (os.environ.get("FUSED_RENDER_ORIGIN") or "").rstrip("/")


class _HttpError(Exception):
    pass


def _http(method: str, path: str, body: dict | None = None) -> dict:
    origin = _origin()
    if not origin:
        raise _HttpError("fused-render server origin unknown "
                         "(FUSED_RENDER_ORIGIN is not set for this session)")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"X-Fused": "1", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(origin + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        msg = ""
        try:
            payload = json.loads(exc.read().decode("utf-8", "replace"))
            if isinstance(payload, dict):
                msg = str(payload.get("error") or payload.get("detail") or "")
        except Exception:  # noqa: BLE001 — a non-JSON error body
            pass
        raise _HttpError("server said %d%s" % (exc.code, ": " + msg if msg else ""))
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise _HttpError("fused-render server unreachable at %s (%s)"
                         % (origin, reason))
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise _HttpError("server answered with something that is not JSON")
    return parsed if isinstance(parsed, dict) else {}


def _bot_path(suffix: str) -> str:
    return "/api/bots/%s%s" % (urllib.parse.quote(SLUG, safe=""), suffix)


def _opt_model_effort(args: dict, body: dict) -> dict:
    for key in ("model", "effort"):
        value = _str(args, key)
        if value:
            body[key] = value
    return body


def _app_url(entry_html: str) -> str:
    return "/apps?" + urllib.parse.urlencode({"path": entry_html}) if entry_html else ""


def _task_summary(task: dict, note: str) -> str:
    entry = str(task.get("entry_html") or "")
    return json.dumps({
        "task_id": task.get("id"),
        "kind": task.get("kind"),
        "app_name": task.get("app_name"),
        "app_path": task.get("app_path"),
        "entry_html": entry,
        "url": _app_url(entry),
        "state": task.get("state"),
        "note": note,
    }, indent=2)


_STARTED = ("Task started; the build runs in the background — tell the user "
            "they can watch it from the app's Tasks tab (open the url).")


def _create_app(args: dict) -> str:
    name, spec = _str(args, "name"), _str(args, "spec")
    if not name:
        return "Error: `name` is required."
    if not spec:
        return "Error: `spec` is required — write the detailed spec first."
    body = _opt_model_effort(args, {"kind": "new", "name": name, "spec": spec})
    try:
        resp = _http("POST", _bot_path("/tasks"), body)
    except _HttpError as exc:
        return "Error: could not start the build: %s" % exc
    task = resp.get("task")
    if not isinstance(task, dict):
        return "Error: the server did not return a task."
    return _task_summary(task, _STARTED)


def _apps() -> list:
    apps = _http("GET", _bot_path("/apps")).get("apps")
    return [a for a in apps if isinstance(a, dict)] if isinstance(apps, list) else []


def _resolve_app(ref: str) -> str:
    """App name or path → a path the server's edit branch resolves. A path the
    model already has (it exists, or names an .html) goes straight through —
    the server resolves the entry html either way; a name is matched against
    the listing exactly, then case-insensitively, then by folder basename."""
    expanded = os.path.expanduser(ref)
    if os.path.isabs(expanded) and (os.path.exists(expanded)
                                    or expanded.endswith(".html")):
        return expanded
    apps = _apps()
    for pick in (lambda a: str(a.get("name") or "") == ref,
                 lambda a: str(a.get("name") or "").casefold() == ref.casefold(),
                 lambda a: os.path.basename(str(a.get("path") or "").rstrip("/\\"))
                 .casefold() == ref.casefold()):
        hits = [a for a in apps if pick(a)]
        if len(hits) == 1:
            return str(hits[0].get("path") or hits[0].get("entry_html") or "")
        if len(hits) > 1:
            raise _HttpError("%r matches %d apps (%s) — pass the folder path"
                             % (ref, len(hits), ", ".join(
                                 str(a.get("path")) for a in hits)))
    names = ", ".join(sorted(str(a.get("name")) for a in apps)) or "(none)"
    raise _HttpError("no app named %r. Known apps: %s" % (ref, names))


def _edit_app(args: dict) -> str:
    ref, spec = _str(args, "app"), _str(args, "spec")
    if not ref:
        return "Error: `app` is required (an app name or folder path)."
    if not spec:
        return "Error: `spec` is required — describe the change in detail first."
    try:
        path = _resolve_app(ref)
        body = _opt_model_effort(args, {"kind": "edit", "path": path, "spec": spec})
        resp = _http("POST", _bot_path("/tasks"), body)
    except _HttpError as exc:
        return "Error: %s" % exc
    task = resp.get("task")
    if not isinstance(task, dict):
        return "Error: the server did not return a task."
    return _task_summary(task, _STARTED.replace("the build", "the edit"))


def _list_apps(args: dict) -> str:
    try:
        apps = _apps()
    except _HttpError as exc:
        return "Error: %s" % exc
    if not apps:
        return "No apps yet."
    return json.dumps([{"name": a.get("name"), "path": a.get("path"),
                        "entry_html": a.get("entry_html")} for a in apps], indent=2)


def _task_status(args: dict) -> str:
    try:
        tasks = _http("GET", _bot_path("/tasks")).get("tasks")
    except _HttpError as exc:
        return "Error: %s" % exc
    tasks = [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []
    task_id = _str(args, "task_id")
    # state/turn/error are the live fields the server joins from the schedule;
    # state "" means the schedule entry is gone (finished and pruned, or deleted).
    keep = ("id", "kind", "app_name", "app_path", "entry_html", "state", "turn",
            "error", "created")
    if task_id:
        hit = next((t for t in tasks if str(t.get("id")) == task_id), None)
        if hit is None:
            return "Error: no task %r for this bot." % task_id
        return json.dumps({k: hit.get(k) for k in keep}, indent=2)
    if not tasks:
        return "This bot has not started any tasks yet."
    # The server returns tasks OLDEST first (tasks.json append order), so the
    # most recent are at the end: reverse rather than re-sort, the server's order
    # is the authority.
    tasks = tasks[::-1]
    return json.dumps([{k: t.get(k) for k in keep} for t in tasks[:20]], indent=2)


# --------------------------------------------------------------------------
# MCP wiring
# --------------------------------------------------------------------------

def _schema(name, description, properties, required=()):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": list(required)}}


_S = {"type": "string"}
_MODEL = {"type": "string", "description":
          "Optional model for the builder agent: fable, opus, sonnet or haiku. "
          "Omit to use the user's default."}
_EFFORT = {"type": "string", "description":
           "Optional effort for the builder agent: low, medium, high, xhigh or "
           "max. Omit to use the default."}

TOOLS = {
    "remember": (_schema(
        "remember",
        "Save a durable fact to your long-term memory (MEMORY.md) — something "
        "about the user, their preferences, projects or decisions that should "
        "still be known in a future conversation. One short fact per call.",
        {"text": dict(_S, description="The fact, one line."),
         "topic": dict(_S, description="Optional section heading, e.g. "
                                        "'Preferences' or 'Projects'. "
                                        "Defaults to 'Notes'.")},
        ("text",)), _remember),
    "forget": (_schema(
        "forget",
        "Delete memory lines containing `match` (case-insensitive). Returns "
        "how many lines were removed.",
        {"match": dict(_S, description="Text the lines to delete contain.")},
        ("match",)), _forget),
    "recall": (_schema(
        "recall",
        "Read your saved memory: all of MEMORY.md, or only lines matching "
        "`query`.",
        {"query": dict(_S, description="Optional filter text.")}), _recall),
    "create_app": (_schema(
        "create_app",
        "Create a NEW Fused app and start a builder agent on it in the "
        "background. `spec` must be a detailed build spec: purpose, pages or "
        "views, data and where it comes from, interactions, and look and feel. "
        "Returns the task id and the app's path and url.",
        {"name": dict(_S, description="The app's display name."),
         "spec": dict(_S, description="The detailed build spec."),
         "model": _MODEL, "effort": _EFFORT},
        ("name", "spec")), _create_app),
    "edit_app": (_schema(
        "edit_app",
        "Start a builder agent in the background that changes an EXISTING "
        "Fused app to `spec`. `app` is the app's name (see list_apps) or its "
        "folder path.",
        {"app": dict(_S, description="App name or folder path."),
         "spec": dict(_S, description="A detailed description of the change."),
         "model": _MODEL, "effort": _EFFORT},
        ("app", "spec")), _edit_app),
    "list_apps": (_schema(
        "list_apps", "List the user's local Fused apps (name, folder path, "
        "entry page).", {}), _list_apps),
    "task_status": (_schema(
        "task_status",
        "Status of the app-building tasks you started: one task by id, or the "
        "20 most recent.",
        {"task_id": dict(_S, description="Optional task id.")}), _task_status),
}


def _dispatch(method: str, params: dict) -> dict:
    if method == "initialize":
        client_version = params.get("protocolVersion")
        return {
            "protocolVersion": (client_version if isinstance(client_version, str)
                                else PROTOCOL_VERSION),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
        }
    if method == "tools/list":
        return {"tools": [schema for schema, _ in TOOLS.values()]}
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            raise LookupError("unknown tool: %s" % name)
        args = params.get("arguments")
        args = args if isinstance(args, dict) else {}
        try:
            return _text(TOOLS[name][1](args))
        except Exception as exc:  # noqa: BLE001 — a tool fault is a text answer
            return _text("Error: %s: %s" % (type(exc).__name__, exc))
    if method == "ping":
        return {}
    raise LookupError("unknown method: %s" % method)


def _serve_request(req_id, method: str, params: dict) -> None:
    try:
        _send({"jsonrpc": "2.0", "id": req_id, "result": _dispatch(method, params)})
    except LookupError as exc:
        _send({"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32601, "message": str(exc)}})
    except (OSError, TypeError, ValueError) as exc:
        _send({"jsonrpc": "2.0", "id": req_id,
               "error": {"code": -32603, "message": "%s: %s"
                         % (type(exc).__name__, exc)}})


def main() -> int:
    _utf8_stdio()
    if not BOT_DIR or not os.path.isdir(BOT_DIR):
        _log("bot_server.py: missing or non-directory bot-dir argument")
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        req_id = msg.get("id")
        if req_id is None:
            continue  # a notification — nothing to answer
        method = msg.get("method") or ""
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "tools/call":
            # Off the reader thread: create_app can take many seconds and a
            # parallel recall should not queue behind it. Memory writes are
            # serialised by _memory_lock.
            threading.Thread(target=_serve_request,
                             args=(req_id, method, params), daemon=True).start()
        else:
            _serve_request(req_id, method, params)
    return 0


if __name__ == "__main__":
    sys.exit(main())
