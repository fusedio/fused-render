"""A one-tool MCP server, so a PROMPT STEP is observed like every other step.

A workflow node is normally a curated MCP tool. A **prompt step** is not: it is a
sentence the author wants Claude to think about between two tool calls — "decide
which of these accounts is worth reading" — and there is no tool behind it.

The obvious implementation is to write the instruction into the prompt and let
the model do it. That implementation cannot be WATCHED. `run.py::_poll` derives
every node's state from the run's real `tool_use` / `tool_result` records and
deliberately refuses to read the model's prose (WC-5), so a step with no tool
call behind it is a step that sits `pending` for the whole run and then flips to
`error` when the run ends. The alternative — believing a "step 3 done" sentence
in the narration — is the one thing that readout exists to not do.

So a prompt step gets a REAL tool call, and this is the tool:

    step_note(step_id: string, result: string) -> result

It echoes `result` straight back and holds no state, touches no files and reaches
no network. It is a RECEIPT, not a computation: the model does the thinking the
author asked for and then hands the conclusion to this tool, which makes the
thinking visible to `_poll` as a genuine `tool_use`/`tool_result` pair on the
wire. Three properties fall out and all three are why this shape was chosen over
a second progress mechanism:

* the node lights up from a fact about what the machine did, not from narration;
* attribution is EXACT — `step_id` is an ARGUMENT, so `_poll` knows precisely
  which node a call belongs to, where a real MCP tool can only be matched by
  name against the pending nodes that call it;
* the conclusion is a tool RESULT, so a downstream node's `source: "previous"`
  input reads it exactly like any other step's output, and `_observed` records
  its shape onto the document like any other step's.

Spoken protocol: JSON-RPC 2.0 over stdio, one JSON object per line, protocol
version `2025-06-18` — the same envelope `fused app serve` speaks, so the run's
`--mcp-config` carries two servers of one kind rather than two kinds.

Launched as `<the interpreter running this template> <this file>`; never as
`python`, which may be absent from the PATH of a detached session (the same rule
D334 states for `fused`).

stdlib only. Nothing here imports fused_render (SPEC PY-15 / D166), and it does
not import `../shared` either — it is spawned as a bare script by another
process and has no template environment around it.
"""

import json
import sys

PROTOCOL_VERSION = "2025-06-18"

TOOL_NAME = "step_note"

# A conclusion is a paragraph, not a payload. The result is echoed back into the
# session's own context and then into `run.py`'s capped output, so a model that
# decided to paste a megabyte of mail into it is capped here rather than three
# layers later.
_RESULT_CAP = 64 * 1024

_SCHEMA = {
    "type": "object",
    "properties": {
        "step_id": {
            "type": "string",
            "description": "The id of the workflow step this note records. "
                           "Use exactly the id the step's instruction gives.",
        },
        "result": {
            "type": "string",
            "description": "Your conclusion for that step, as text. This is what "
                           "later steps see as the step's output.",
        },
    },
    "required": ["step_id", "result"],
}

_DESCRIPTION = (
    "Record the conclusion of a workflow prompt step. Call this once per prompt "
    "step, with that step's id, after doing what the step asked. It returns your "
    "own text unchanged — its purpose is to make the step's completion and its "
    "result visible to the workflow's progress readout."
)


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(request_id, payload: dict) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def _error(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}})


def _call(request_id, params: dict) -> None:
    """`tools/call` — echo the note back as the tool's result.

    A missing or non-string argument is answered as a TOOL error (`isError`)
    rather than as a JSON-RPC error: the model can read the sentence and call
    again, whereas a protocol error is the host's problem and not the model's.
    """
    name = params.get("name")
    if name != TOOL_NAME:
        _error(request_id, -32602, "unknown tool %r" % (name,))
        return
    args = params.get("arguments")
    args = args if isinstance(args, dict) else {}
    step_id = args.get("step_id")
    note = args.get("result")
    if not isinstance(step_id, str) or not step_id.strip():
        _result(request_id, {
            "content": [{"type": "text",
                         "text": "step_id is required, and must be the id string "
                                 "the step's instruction gave."}],
            "isError": True})
        return
    if not isinstance(note, str):
        # A model that answered with an object rather than a string is being
        # helpful in the wrong shape; the note is what a later step reads, so it
        # is rendered rather than refused.
        note = "" if note is None else json.dumps(note)
    _result(request_id, {"content": [{"type": "text", "text": note[:_RESULT_CAP]}],
                         "isError": False})


def _handle(request: dict) -> None:
    method = request.get("method")
    request_id = request.get("id")
    # A NOTIFICATION HAS NO ID AND MUST GET NO REPLY. `notifications/initialized`
    # arrives right after the handshake, and answering it is a protocol error
    # that some hosts drop the connection over.
    if request_id is None:
        return
    if method == "initialize":
        _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fused-workflow-steps", "version": "1"},
            "instructions": "Records the conclusion of a workflow prompt step.",
        })
        return
    if method == "tools/list":
        _result(request_id, {"tools": [{"name": TOOL_NAME,
                                        "description": _DESCRIPTION,
                                        "inputSchema": _SCHEMA}]})
        return
    if method == "tools/call":
        params = request.get("params")
        _call(request_id, params if isinstance(params, dict) else {})
        return
    if method == "ping":
        _result(request_id, {})
        return
    # Everything else — prompts/list, resources/list — is answered as "not
    # implemented" rather than ignored, so a host probing capabilities gets an
    # answer instead of hanging on a read.
    _error(request_id, -32601, "method %r is not supported" % (method,))


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            # No id to answer against, so there is nobody to tell. Skipping is
            # the only thing a line-oriented server can do with a broken line.
            continue
        if not isinstance(request, dict):
            continue
        try:
            _handle(request)
        except Exception as exc:  # noqa: BLE001 — a crash here kills the run
            request_id = request.get("id")
            if request_id is not None:
                _error(request_id, -32603, "%s: %s" % (type(exc).__name__, exc))


if __name__ == "__main__":
    main()
