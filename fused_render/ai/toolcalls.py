"""Family-keyed, streaming-safe tool-call parsing for the `/v1` gateway
(docs/LOCAL_MODEL_GATEWAY.md §2, "Tools").

A local model's reply to a request that carried `tools` is plain text in
which the model's chat template taught it to write calls in ITS family's
own marker shape. This module turns that text into the two kinds of event
the gateway speaks — `("text", str)` and `("call", {"name", "arguments"})`
— without ever buffering more than it has to: text outside a call marker is
handed back on the very `feed` that delivered it, a marker split across two
chunks is held until it either completes or turns out to be prose, and a
call is emitted once its closing marker (or, for the end-less shapes, a
complete JSON object) has arrived.

Families and the shapes each one recognises (`parse_family` picks one by
the model id's name prefix; the catalog's `tool_family` field overrides):

* `qwen` / `hermes` — `<tool_call>{"name": …, "arguments": {…}}</tool_call>`,
  one call per block, any number of blocks. A JSON LIST inside the block is
  taken as several calls; `"parameters"` is accepted as an alias of
  `"arguments"`. Bonsai (Ternary-Bonsai) and any model the catalog does not
  name default here — it is the Hermes convention Qwen adopted and the most
  widely trained.
* `llama3` — `<|python_tag|>{"name": …, "parameters": {…}}` (optionally
  terminated by `<|eom_id|>`/`<|eot_id|>`, several objects separated by
  `;`), AND the JSON-only reply shape Llama 3.1+ uses for a single call: a
  reply whose first non-blank character is `{` is held until it parses as a
  `{"name", "parameters"|"arguments"}` object; if it never does it is
  released as text at `finish`.
* `gemma` — a ```` ```tool_code ```` fenced block holding a Python-style
  call `name(k=v, …)` (optionally wrapped in `print(...)`), and the
  `<start_function_call>…<end_function_call>` block holding either the
  same call syntax, `call:name{json}`, or a `{"name", "arguments"}` JSON
  object. Best effort: Gemma's templates vary by release, and a block that
  parses as none of these is released as text.
* `lfm` — `<|tool_call_start|>[{"name": …, "arguments": {…}}]<|tool_call_end|>`
  (a JSON list or object), or LFM2's Python-style `[name(k=v, …)]`.

`arguments` is a dict when the shape gave one and a string otherwise
(LFM's bare positional call, say) — the gateway JSON-encodes a dict and
passes a string through verbatim, which is exactly what OpenAI's
`function.arguments` (always a string) needs.

Pure stdlib, no model and no network: this runs in the server process on a
stream the supervisor is relaying, not in a worker.
"""

from __future__ import annotations

import ast
import json
import re

FAMILIES = ("qwen", "hermes", "llama3", "gemma", "lfm")

_PREFIXES = (
    ("qwen", "qwen"),
    ("qwq", "qwen"),
    ("hermes", "hermes"),
    ("ternary-bonsai", "hermes"),
    ("bonsai", "hermes"),
    ("llama", "llama3"),
    ("meta-llama", "llama3"),
    ("gemma", "gemma"),
    ("lfm", "lfm"),
)


def parse_family(name: str | None) -> str:
    """The family a model id belongs to, by the prefix of its basename —
    `mlx-community/Qwen3.5-4B-OptiQ-4bit` and `Qwen3.5-4B-Q4_K_M.gguf`
    both answer `qwen`. Unknown names answer `hermes`, the convention most
    instruction-tuned models share."""
    base = (name or "").rsplit("/", 1)[-1].lower()
    for prefix, family in _PREFIXES:
        if base.startswith(prefix):
            return family
    return "hermes"


# --------------------------------------------------------------- decoders

def _calls_from_json(value) -> list[dict] | None:
    """`{"name", "arguments"|"parameters"}` or a list of them -> calls, or
    None when the value is not call-shaped."""
    if isinstance(value, list):
        out: list[dict] = []
        for item in value:
            calls = _calls_from_json(item)
            if calls is None:
                return None
            out.extend(calls)
        return out
    if isinstance(value, dict):
        # `{"function": {...}}` (OpenAI's own echo shape) unwraps.
        if "name" not in value and isinstance(value.get("function"), dict):
            value = value["function"]
        name = value.get("name")
        if not isinstance(name, str) or not name:
            return None
        args = value.get("arguments", value.get("parameters", {}))
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
            except ValueError:
                parsed = args
            args = parsed if isinstance(parsed, dict) else args
        if not isinstance(args, (dict, str)):
            args = {} if args is None else json.dumps(args)
        return [{"name": name, "arguments": args}]
    return None


def _literal(node: ast.AST):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError):
        return ast.unparse(node)


def _calls_from_python(text: str) -> list[dict] | None:
    """`name(k=v, …)`, `print(name(k=v))`, `[name(k=v), other()]` -> calls.
    None when `text` is not a call expression (or a list of them)."""
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except (SyntaxError, ValueError):
        return None
    body = tree.body
    nodes = body.elts if isinstance(body, (ast.List, ast.Tuple)) else [body]
    out: list[dict] = []
    for node in nodes:
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print" and len(node.args) == 1
                and isinstance(node.args[0], ast.Call)):
            node = node.args[0]
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = ast.unparse(func)
        else:
            return None
        if node.args and not node.keywords:
            # Positional only: nothing to key them by, so the arguments ride
            # as the verbatim string the model wrote.
            arguments: dict | str = ", ".join(ast.unparse(a) for a in node.args)
        else:
            arguments = {kw.arg: _literal(kw.value) for kw in node.keywords
                         if kw.arg is not None}
            if node.args:
                arguments["_args"] = [_literal(a) for a in node.args]
        out.append({"name": name, "arguments": arguments})
    return out


def _decode_json_text(text: str) -> list[dict] | None:
    text = text.strip()
    if not text:
        return None
    try:
        return _calls_from_json(json.loads(text))
    except ValueError:
        pass
    # Several objects back to back (llama3's `;`-separated python_tag form).
    decoder = json.JSONDecoder()
    pos, calls = 0, []
    while pos < len(text):
        while pos < len(text) and text[pos] in " \t\r\n;,":
            pos += 1
        if pos >= len(text):
            break
        try:
            value, end = decoder.raw_decode(text, pos)
        except ValueError:
            return None
        found = _calls_from_json(value)
        if found is None:
            return None
        calls.extend(found)
        pos = end
    return calls or None


_GEMMA_CALL_RE = re.compile(r"^\s*call:\s*([A-Za-z_][\w.]*)\s*(\{.*\})?\s*$", re.S)


def _decode_gemma(text: str) -> list[dict] | None:
    m = _GEMMA_CALL_RE.match(text)
    if m:
        args: dict | str = {}
        if m.group(2):
            try:
                loaded = json.loads(m.group(2))
                args = loaded if isinstance(loaded, dict) else m.group(2)
            except ValueError:
                args = m.group(2)
        return [{"name": m.group(1), "arguments": args}]
    return _decode_json_text(text) or _calls_from_python(text)


def _decode_lfm(text: str) -> list[dict] | None:
    return _decode_json_text(text) or _calls_from_python(text)


def _json_complete(text: str) -> bool:
    """Does `text` (after whitespace) hold at least one complete JSON value
    and nothing but whitespace/separators after the last one?"""
    stripped = text.strip()
    if not stripped:
        return False
    decoder = json.JSONDecoder()
    pos = 0
    try:
        while pos < len(stripped):
            while pos < len(stripped) and stripped[pos] in " \t\r\n;,":
                pos += 1
            if pos >= len(stripped):
                break
            _, pos = decoder.raw_decode(stripped, pos)
    except ValueError:
        return False
    return True


#: Per family: (start marker, end markers (None = ends with a complete JSON
#: value or the stream), decoder). Order matters only for the earliest-match
#: search, which picks the lowest index in the buffer.
_SHAPES: dict[str, list[tuple[str, tuple[str, ...] | None, object]]] = {
    "hermes": [("<tool_call>", ("</tool_call>",), _decode_json_text)],
    "qwen": [("<tool_call>", ("</tool_call>",), _decode_json_text)],
    "llama3": [("<|python_tag|>", None, _decode_json_text)],
    "gemma": [
        ("```tool_code", ("```",), _decode_gemma),
        ("<start_function_call>", ("<end_function_call>",), _decode_gemma),
    ],
    "lfm": [("<|tool_call_start|>", ("<|tool_call_end|>",), _decode_lfm)],
}
#: Terminators the end-less (`None`) shapes also stop at when the model
#: writes them; stripped from the call body, never emitted as text.
_SPECIAL_ENDS = ("<|eom_id|>", "<|eot_id|>", "<|im_end|>")


class StreamParser:
    """Feed chunks in, get `("text", str)` / `("call", {...})` events out.

    `feed` never raises on model output; what it cannot read it hands back
    as text. `finish` flushes whatever is still held: an unterminated call
    is decoded if it can be, released as text if it cannot.
    """

    def __init__(self, family: str | None = None):
        self.family = family if family in _SHAPES else "hermes"
        self._shapes = _SHAPES[self.family]
        self._buffer = ""
        self._active: tuple[str, tuple[str, ...] | None, object] | None = None
        self._emitted_text = ""
        self.calls = 0

    # The llama3 JSON-only shape: a reply that OPENS with `{` is a call until
    # proven otherwise. Only when nothing but whitespace has been emitted so
    # far — a `{` in the middle of prose is prose.
    def _bare_json_shape(self) -> bool:
        return self.family == "llama3" and not self._emitted_text.strip()

    def feed(self, text: str) -> list[tuple]:
        if not text:
            return []
        self._buffer += text
        events: list[tuple] = []
        while True:
            if self._active is not None:
                if not self._close_active(events, final=False):
                    break
                continue
            if not self._open_next(events):
                break
        return events

    def finish(self) -> list[tuple]:
        events: list[tuple] = []
        if self._active is not None:
            self._close_active(events, final=True)
        elif self._buffer:
            if self._bare_json_shape() and self._buffer.lstrip().startswith("{"):
                calls = _decode_json_text(self._buffer)
                if calls:
                    self._emit_calls(events, calls)
                    self._buffer = ""
            if self._buffer:
                self._emit_text(events, self._buffer)
                self._buffer = ""
        return events

    # ------------------------------------------------------------ internals

    def _emit_text(self, events: list, text: str) -> None:
        if text:
            self._emitted_text += text
            events.append(("text", text))

    def _emit_calls(self, events: list, calls: list[dict]) -> None:
        for call in calls:
            self.calls += 1
            events.append(("call", call))

    def _open_next(self, events: list) -> bool:
        """Find the earliest start marker in the buffer. Emits the text before
        it and activates the shape; returns False when nothing more can be
        done with the current buffer."""
        buf = self._buffer
        if self.family == "llama3":
            # Turn terminators are control tokens, never prose: dropped
            # wherever they land (after a python_tag call they are the
            # tail `_close_active` left behind).
            for end in _SPECIAL_ENDS:
                buf = buf.replace(end, "")
        best: tuple[int, tuple] | None = None
        for shape in self._shapes:
            idx = buf.find(shape[0])
            if idx >= 0 and (best is None or idx < best[0]):
                best = (idx, shape)
        if self._bare_json_shape():
            stripped = buf.lstrip()
            if stripped.startswith("{"):
                idx = len(buf) - len(stripped)
                if best is None or idx < best[0]:
                    best = (idx, ("", None, _decode_json_text))
        if best is not None:
            idx, shape = best
            self._emit_text(events, buf[:idx])
            self._buffer = buf[idx + len(shape[0]):]
            self._active = shape
            return True
        # No full marker. Hold a tail that is a prefix of one, emit the rest.
        hold = 0
        markers = [shape[0] for shape in self._shapes]
        if self.family == "llama3":
            markers.extend(_SPECIAL_ENDS)
        for marker in markers:
            for n in range(min(len(marker) - 1, len(buf)), 0, -1):
                if buf.endswith(marker[:n]):
                    hold = max(hold, n)
                    break
        if self._bare_json_shape() and not buf.strip():
            # Leading whitespace of a reply that may still open with `{`.
            hold = len(buf)
        self._emit_text(events, buf[:len(buf) - hold] if hold else buf)
        self._buffer = buf[len(buf) - hold:] if hold else ""
        return False

    def _close_active(self, events: list, *, final: bool) -> bool:
        """Try to complete the active call from the buffer. Returns True when
        the call closed (buffer advanced), False when more input is needed."""
        start, ends, decode = self._active
        buf = self._buffer
        body: str | None = None
        rest = ""
        matched_end = ""
        if ends:
            best: tuple[int, str] | None = None
            for end in ends:
                idx = buf.find(end)
                if idx >= 0 and (best is None or idx < best[0]):
                    best = (idx, end)
            if best is not None:
                body = buf[:best[0]]
                rest = buf[best[0] + len(best[1]):]
                matched_end = best[1]
        else:
            cut = None
            for end in _SPECIAL_ENDS:
                idx = buf.find(end)
                if idx >= 0 and (cut is None or idx < cut[0]):
                    cut = (idx, end)
            if cut is not None:
                body, rest = buf[:cut[0]], buf[cut[0] + len(cut[1]):]
            elif _json_complete(buf) and not final:
                # Complete object(s) so far; a later chunk may add another
                # `;{...}`, but the common case is one call and holding the
                # stream for it would delay the client's whole turn. Close
                # on the first complete value and let a second open again.
                body, rest = buf, ""
        if body is None:
            if not final:
                return False
            body, rest = buf, ""
        calls = decode(body)
        if calls:
            self._emit_calls(events, calls)
        else:
            # Not a call after all: release verbatim, markers included, so
            # the client sees exactly what the model wrote.
            self._emit_text(events, start + body + matched_end)
        self._active = None
        self._buffer = rest
        return True
