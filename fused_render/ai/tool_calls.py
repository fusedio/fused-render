"""Turning MCP tools into a chat-template `tools=` schema, and turning a
local model's raw text back into a tool call — the two halves of the local
tool loop `server/ai.py`'s `_generate_with_tools` drives.

**Why this is a heuristic, not a protocol.** The Claude tier needs none of
this: the `claude` CLI has its own, real MCP client and its own tool-call
parsing, built against every model OpenAI/Anthropic actually trains for
structured tool use. A model run through `mlx-text`/`llamacpp-text` has
neither — it is handed a `tools=` kwarg its own chat template renders
however that template's author chose to, and it answers in whatever raw text
its OWN fine-tuning taught it, which is a per-model-FAMILY convention, not a
wire format. This module recognises the one convention shared by the widest
set of tool-trained open models available through this app's catalog
(Qwen and Hermes-style `<tool_call>{...}</tool_call>` — `registry.py`'s
`tool-use` tag already names Qwen3/Qwen2.5/Hermes among the families it
marks), plus a bare-JSON fallback for a model that skips the tags. A model
whose family uses a different convention (Mistral's `[TOOL_CALLS]`, Llama's
`<|python_tag|>`, …) will not be recognised here — it will just answer in
plain text, tools ignored, exactly as it would on a caller that never sent
any.
"""

from __future__ import annotations

import json
import re


def build_tool_schema(mcp_tools: list[dict]) -> tuple[list[dict], dict[str, dict]]:
    """`(schema, index)` from `mcp_client.list_all_tools`'s own output.

    `schema` is the JSON-schema/OpenAI "function" shape a chat template's
    `tools=` kwarg (and Claude's own `--mcp-config`) already expects — MCP's
    `{name, description, inputSchema}` IS that shape once wrapped one level
    (`{"type": "function", "function": {...}}`), so nothing here invents a
    new vocabulary, only the wrapping.

    `index` maps the WIRE name (see `_wire_name`) back to `(server,
    real_name)` — what `run_tool_call` needs to know which server to ask and
    what the tool is actually called there, since the wire name may have
    been disambiguated and no longer matches either directly.
    """
    schema, index, seen = [], {}, set()
    for tool in mcp_tools:
        server = tool["_server"]
        real_name = tool["name"]
        wire_name = _wire_name(server["name"], real_name, seen)
        seen.add(wire_name)
        index[wire_name] = {"server": server, "name": real_name}
        schema.append({
            "type": "function",
            "function": {
                "name": wire_name,
                "description": tool.get("description") or "",
                "parameters": tool.get("inputSchema") or {"type": "object", "properties": {}},
            },
        })
    return schema, index


def _wire_name(server_name: str, tool_name: str, seen: set[str]) -> str:
    """The name a model is actually shown for one tool.

    The bare tool name when nothing else has claimed it — most chats name
    one server, and a bare `search` reads better in a model's own output
    than `docs__search`. Namespaced with the server (`server__tool`, MCP's
    own separator convention) only once a SECOND server offers a tool of the
    same name, so two servers that both expose `search` do not silently
    collide into whichever loaded first.
    """
    if tool_name not in seen:
        return tool_name
    return f"{server_name}__{tool_name}"


#: Qwen/Hermes-family tool-call markup: `<tool_call>{"name": ..., "arguments":
#: {...}}</tool_call>`, one or more, anywhere in the reply. `re.DOTALL` since
#: an argument value can itself contain a newline (a multi-line string
#: argument, say).
_TAGGED_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def find_tool_calls(text: str) -> list[dict]:
    """Every `{"name": ..., "arguments": {...}}` call this local model's raw
    output seems to be making, or `[]` when it is not calling anything (the
    ordinary case, and it must read as ordinary — a false positive here
    turns a model just ANSWERING into a broken tool loop).

    Tagged calls first (`_TAGGED_CALL_RE`) — the deliberate, models-that-
    were-actually-trained-for-this signal. Only when there is NOT ONE tagged
    call anywhere does this fall back to asking whether the WHOLE reply,
    trimmed, is by itself one bare JSON object shaped like a call — a model
    that skips the wrapper tags but was still trained to answer with
    `{"name": ..., "arguments": ...}` and nothing else. That fallback is
    narrow on purpose: it does not fire on a reply that merely CONTAINS
    JSON (an example, a code block) alongside its own prose, only on one
    that IS nothing else.
    """
    calls = []
    for match in _TAGGED_CALL_RE.finditer(text):
        parsed = _parse_call_object(match.group(1))
        if parsed is not None:
            calls.append(parsed)
    if calls:
        return calls
    parsed = _parse_call_object(text.strip())
    return [parsed] if parsed is not None else []


def _parse_call_object(raw: str) -> dict | None:
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = obj.get("arguments", {})
    if isinstance(arguments, str):
        # Some templates render arguments as an already-JSON-encoded string
        # rather than a nested object; a tool call is not wrong for using
        # that shape, only differently wrapped.
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return None
    if not isinstance(arguments, dict):
        return None
    return {"name": name, "arguments": arguments}


def strip_tool_calls(text: str) -> str:
    """`text` with every tagged tool call removed — what is left over is
    whatever prose the model wrote AROUND the call, if any, which belongs in
    the assistant turn this round's call is recorded under (a model that
    says "Let me check that." and then calls a tool should not lose the
    sentence)."""
    return _TAGGED_CALL_RE.sub("", text).strip()
