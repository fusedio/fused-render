"""A small MCP client: connect to a server a caller configured, list its
tools, call one. Used by `server/ai.py`'s local-model tool loop — the Claude
tier does not need this at all, since the `claude` CLI speaks MCP itself
(`--mcp-config`) once handed the same server list.

Two transports, both already affordable in THIS process's own dependency set
(no new package): **stdio** (a child process, newline-delimited JSON-RPC on
its stdin/stdout — MCP's own framing, no LSP-style Content-Length header) via
`subprocess`, and **http** (a single POST per call, MCP's "Streamable HTTP"
transport in its simplest shape: one JSON-RPC request in, either a plain JSON
response or a one-shot SSE stream carrying it back out) via `httpx`, already a
core dependency of this app.

Deliberately NOT a persistent, pooled connection. Every `/api/ai` call that
names MCP servers opens what it needs, lists or calls a tool, and tears the
connection down again before the response is sent — simplest-correct for a
local, single-user app where a chat turn is already seconds of latency and
one more process spawn or HTTP round trip is noise beside it. A server that
is slow or wedged costs this ONE call a timeout, not every future one.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid


class McpError(Exception):
    """A configured server could not be reached, or answered with an error —
    the caller's problem to report, never this module's to swallow: a tool
    the model was told about that silently vanishes is a worse failure than
    a request that names what went wrong."""


#: How long one stdio round trip (a request written, its response read) may
#: take before this gives up on the server. Generous for a tool that shells
#: out or hits a network of its own, short enough that one wedged server
#: cannot hang a chat turn indefinitely.
_STDIO_TIMEOUT_S = 20.0
#: Same budget, the HTTP transport's own request timeout.
_HTTP_TIMEOUT_S = 20.0
#: How long a spawned stdio server gets to answer `initialize` — separate
#: from the per-call timeout above because a server that never starts (a
#: missing binary, a bad interpreter shebang) should fail fast rather than
#: eat a full tool-call budget for nothing.
_INIT_TIMEOUT_S = 10.0

#: The MCP protocol version this client speaks. Sent in `initialize` and not
#: renegotiated — the handful of methods this client calls (`tools/list`,
#: `tools/call`) have been stable across every revision the spec has shipped,
#: so there is nothing here that needs the newest wire shape to work.
_PROTOCOL_VERSION = "2025-06-18"

_CLIENT_INFO = {"name": "fused-render", "version": "1"}


def validate_server(server: dict) -> None:
    """Raise `McpError` naming what is wrong with `server`'s shape, or return.

    The same closed-envelope discipline `server/ai.py`'s own request
    validation follows: a caller that mistyped a key learns which key, not a
    500 three modules away. `type` follows the shape the app already asks a
    user to type by hand (`claude_config/mcp.py`'s `add-json`, mirrored by
    the playground's own "add a server" form) — `stdio` needs `command`,
    `http`/`sse` need `url`.
    """
    name = server.get("name")
    if not isinstance(name, str) or not name.strip():
        raise McpError("an MCP server definition needs a non-empty 'name'")
    kind = server.get("type")
    if kind == "stdio":
        command = server.get("command")
        if not isinstance(command, str) or not command.strip():
            raise McpError(f"MCP server {name!r}: 'command' is required for a stdio server")
        args = server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise McpError(f"MCP server {name!r}: 'args' must be a list of strings")
        env = server.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
            raise McpError(f"MCP server {name!r}: 'env' must be a string-to-string object")
    elif kind in ("http", "sse"):
        url = server.get("url")
        if not isinstance(url, str) or not url.strip():
            raise McpError(f"MCP server {name!r}: 'url' is required for an http/sse server")
    else:
        raise McpError(
            f"MCP server {name!r}: 'type' must be 'stdio', 'http' or 'sse', got {kind!r}")


class _StdioSession:
    """One spawned server, talked to over its stdin/stdout for the life of
    this object — used as a context manager so the child is always reaped,
    success or failure alike."""

    def __init__(self, server: dict):
        self._server = server
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._next_id = 0

    def __enter__(self) -> "_StdioSession":
        env = dict(os.environ)
        env.update(self._server.get("env") or {})
        # No shell: `command`/`args` are a real argv, exactly the argv-safety
        # rule `claude_config/mcp.py`'s own comment states for a server name —
        # there is no shell here for anything to inject INTO.
        #
        # `close_fds=False`/`encoding="utf-8"`/`errors="replace"` are the same
        # trio `claude_config/lib.py`'s own `SUBPROCESS_KWARGS` spawns every
        # `claude`/`git` child with, for the identical reasons stated there at
        # length: `close_fds=True`'s default forces CPython onto the fork()
        # path, which runs this server's own `pthread_atfork` handler against
        # a live PROJ/SQLite handle and SIGSEGVs the child; and `text=True`
        # alone decodes with the process locale, which a GUI-launched server
        # often has none of, turning the first non-ASCII byte an MCP server
        # writes (a real risk here — an arbitrary user-configured child, not
        # one of this app's own) into a dead request instead of a readable
        # reply.
        self._proc = subprocess.Popen(
            [self._server["command"], *self._server.get("args", [])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True, encoding="utf-8", errors="replace",
            close_fds=False, bufsize=1)
        try:
            self._handshake()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_exc) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

    def _write(self, message: dict) -> None:
        line = json.dumps(message) + "\n"
        try:
            self._proc.stdin.write(line)
            self._proc.stdin.flush()
        except (OSError, ValueError, BrokenPipeError) as exc:
            raise McpError(
                f"MCP server {self._server['name']!r}: could not write to it ({exc})") from exc

    def _read_response(self, request_id, timeout: float) -> dict:
        """The `jsonrpc` message answering `request_id` — a notification or a
        response to a DIFFERENT id (should not happen; this client only ever
        has one request in flight at a time, per `_lock`) is skipped rather
        than treated as the answer."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpError(
                    f"MCP server {self._server['name']!r}: did not answer within "
                    f"{timeout:.0f}s")
            # `_readline` itself raises on a TIMEOUT (it cannot be retried —
            # there is no way to cancel the blocking read already in flight,
            # only to give up on it), so the only thing this line can be is a
            # real line or "" — `readline()`'s own EOF convention, checked
            # BEFORE stripping: a blank line ("\n") is not EOF, only a
            # genuinely empty read is.
            line = self._readline(remaining)
            if not line:
                stderr = self._drain_stderr()
                raise McpError(
                    f"MCP server {self._server['name']!r}: exited before answering"
                    + (f" ({stderr})" if stderr else ""))
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue  # a server writing non-JSON noise to stdout; skip it
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id and ("result" in message or "error" in message):
                return message

    def _readline(self, timeout: float) -> str:
        """One line off the child's stdout, `readline()`'s own vocabulary
        verbatim (a trailing `"\\n"` for a real line, `""` for EOF) — bounded
        by `timeout` via a reader thread, since `TextIOWrapper.readline()` has
        no timeout parameter of its own and this must not hang the whole
        request on a server that stops answering mid-stream.

        **Raises on timeout, rather than returning a sentinel the caller
        retries** — the first cut of this returned `None` for BOTH "timed
        out" and "EOF", which made `_read_response` treat a merely SLOW
        server as one that had exited, misreporting the failure. There is
        nothing to retry here either way: the reader thread's own
        `readline()` call cannot be cancelled once started (Python has no
        API for that), so once this gives up on it the call is over — a
        second attempt would only stack a second thread on top of the same
        still-blocked read, never actually recovering it.
        """
        box: list = []

        def read():
            try:
                box.append(self._proc.stdout.readline())
            except (OSError, ValueError):
                box.append("")

        thread = threading.Thread(target=read, daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            raise McpError(
                f"MCP server {self._server['name']!r}: did not answer within "
                f"{timeout:.0f}s")
        return box[0] if box else ""

    def _drain_stderr(self) -> str:
        try:
            return (self._proc.stderr.read(4000) or "").strip()[-500:]
        except (OSError, ValueError):
            return ""

    def request(self, method: str, params: dict | None = None,
               timeout: float = _STDIO_TIMEOUT_S) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method,
                        "params": params or {}})
            message = self._read_response(request_id, timeout)
        if "error" in message:
            error = message["error"] or {}
            raise McpError(
                f"MCP server {self._server['name']!r}: {method} failed — "
                f"{error.get('message', error)}")
        return message.get("result") or {}

    def notify(self, method: str, params: dict | None = None) -> None:
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _handshake(self) -> None:
        self.request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": _CLIENT_INFO,
        }, timeout=_INIT_TIMEOUT_S)
        self.notify("notifications/initialized")


def _http_request(server: dict, method: str, params: dict | None,
                  session_id: list) -> dict:
    """One JSON-RPC call over MCP's Streamable HTTP transport.

    `session_id` is a one-element list used as an in/out box: the server's
    `initialize` response may carry an `Mcp-Session-Id` header that every
    later call on the same logical session must echo back — a plain `list`
    rather than `nonlocal` because this is a free function called by a loop
    in `_http_list_tools`/`_http_call_tool`, not a closure.
    """
    import httpx

    request_id = uuid.uuid4().hex
    body = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    headers = dict(server.get("headers") or {})
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"
    if session_id[0]:
        headers["Mcp-Session-Id"] = session_id[0]
    try:
        response = httpx.post(server["url"], json=body, headers=headers,
                              timeout=_HTTP_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise McpError(f"MCP server {server['name']!r}: {exc}") from exc
    if response.status_code >= 400:
        raise McpError(
            f"MCP server {server['name']!r}: HTTP {response.status_code} from {method}")
    new_session = response.headers.get("Mcp-Session-Id")
    if new_session:
        session_id[0] = new_session
    content_type = response.headers.get("Content-Type", "")
    if "text/event-stream" in content_type:
        message = _parse_sse_json(response.text, request_id)
    else:
        try:
            message = response.json()
        except ValueError as exc:
            raise McpError(f"MCP server {server['name']!r}: reply was not JSON") from exc
    if not isinstance(message, dict):
        raise McpError(f"MCP server {server['name']!r}: reply had an unexpected shape")
    if "error" in message:
        error = message["error"] or {}
        raise McpError(
            f"MCP server {server['name']!r}: {method} failed — {error.get('message', error)}")
    return message.get("result") or {}


def _parse_sse_json(text: str, request_id) -> dict:
    """The last well-formed `data:` payload in an SSE body whose `id` matches
    `request_id` — a one-shot response stream (this client sends one request
    and reads one reply), never a persistent subscription, so there is
    nothing else in the stream worth reading past that."""
    for block in text.split("\n\n"):
        data_lines = [line[5:].lstrip() for line in block.splitlines()
                     if line.startswith("data:")]
        if not data_lines:
            continue
        try:
            message = json.loads("".join(data_lines))
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            return message
    raise McpError("the server's event stream carried no matching reply")


def list_tools(server: dict) -> list[dict]:
    """Every tool `server` currently advertises: `[{name, description,
    inputSchema}, ...]`, MCP's own shape (`tools/list`'s `result.tools`) —
    which is also already the JSON-schema `parameters` a chat-template
    `tools=` kwarg or a Claude Code `--mcp-config` expects, so nothing here
    reshapes it further; `tool_calls.py` is where that happens.

    Raises `McpError` naming the server on anything that goes wrong — a
    caller that asked for this server's tools and got a silent empty list
    back would read as "this server has none" rather than "this server could
    not be reached", which is the wrong story to tell a user picking tools
    for a chat.
    """
    validate_server(server)
    if server["type"] == "stdio":
        with _StdioSession(server) as session:
            result = session.request("tools/list")
    else:
        result = _http_request(server, "tools/list", {}, [None])
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise McpError(f"MCP server {server['name']!r}: tools/list returned no tool list")
    return tools


def call_tool(server: dict, name: str, arguments: dict) -> str:
    """Run `name` on `server` with `arguments`; the result flattened to
    plain text for the chat turn it becomes (`server/ai.py`'s tool loop
    appends it as a `{"role": "tool", ...}` message, and every chat template
    this app renders wants that content to be a string, not MCP's own
    `content: [{type, text}, ...]` block list).

    An MCP tool can report failure two ways — a JSON-RPC error (raised by
    the transport already, as `McpError`) or a successful call whose own
    `result.isError` is true (the tool RAN and reported a problem, which is
    not a transport failure) — both are surfaced to the model as text
    describing the error, never silently dropped: a tool call the model
    made and got nothing back for reads as the tool doing nothing, where the
    truth is that it failed.
    """
    validate_server(server)
    params = {"name": name, "arguments": arguments}
    if server["type"] == "stdio":
        with _StdioSession(server) as session:
            result = session.request("tools/call", params)
    else:
        result = _http_request(server, "tools/call", params, [None])
    content = result.get("content")
    text = _flatten_content(content)
    if result.get("isError"):
        return text or f"tool {name!r} reported an error with no further detail"
    return text or "(tool returned no content)"


def _flatten_content(content) -> str:
    """MCP's `content` block list -> plain text. Only `type: "text"` blocks
    are readable as text at all (an image/audio block has no textual form a
    chat turn can carry); anything else is named rather than dropped, so a
    tool that returns a picture reads as "returned a non-text result" instead
    of silently returning nothing."""
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(f"[{block.get('type', 'non-text')} content omitted]")
    return "\n".join(parts)


def list_all_tools(servers: list[dict]) -> tuple[list[dict], list[str]]:
    """`(tools, errors)` across every server in `servers` — each tool tagged
    with which server it came from (`_server`, popped back off before a call
    and never sent to a model as part of its own schema), so a name
    collision between two servers' tools is decided by `tool_calls.py`
    (namespacing), not silently overwritten here.

    A server that fails to list is REPORTED, not fatal to the others — one
    misconfigured MCP server must not take every other one's tools off the
    table for a chat that named several.
    """
    tools, errors = [], []
    for server in servers:
        try:
            for tool in list_tools(server):
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    tools.append({**tool, "_server": server})
        except McpError as exc:
            errors.append(str(exc))
    return tools, errors
