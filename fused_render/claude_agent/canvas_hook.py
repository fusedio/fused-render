"""Claude Code hook for a session working inside a canvas clone.

agent.py attaches it (through `--settings`) to every Claude session whose
target is in a canvas clone, as two hooks:

  canvas_hook.py pre    PreToolUse on Write|Edit|MultiEdit|NotebookEdit
  canvas_hook.py post   PostToolUse on every tool

The clone is shared with the user's live canvas: their adds, deletes, renames
and drags in the embedded workbench land in canvas.toml and the source files
while the session works. Both hooks ask the fused-render server
(`POST $FUSED_RENDER_ORIGIN/api/canvases/agent/hook`), which knows what the
user did:

  pre   may refuse, once, a write that would recreate the file of a node the
        user deleted on the canvas (the user's delete wins; told why, the
        session can still write it again when the user asked for it).
  post  may add context: what the user changed since the session's previous
        tool call, so it re-reads canvas.toml instead of editing from memory.

FAILS OPEN, always: no origin, no server, a slow or odd answer — print
nothing, exit 0. A hook that blocks or crashes would stop the session for a
sync it is only advising. Standard library only: it runs under whatever
interpreter agent.py hands the CLI (`_stable_python`), copied out of the
package (`_stable_script`) so an app update never pulls it from under a live
session.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

_ORIGIN_ENV = "FUSED_RENDER_ORIGIN"
_TIMEOUT_S = 3.0


def _ask(origin: str, payload: dict) -> dict:
    url = origin.rstrip("/") + "/api/canvases/agent/hook"
    host = urllib.parse.urlsplit(url).hostname or ""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "X-Fused": "1"})
    handlers: list = [urllib.request.ProxyHandler({})]
    if url.startswith("https:") and host in ("127.0.0.1", "localhost", "::1"):
        import ssl
        handlers.append(urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
    with urllib.request.build_opener(*handlers).open(request, timeout=_TIMEOUT_S) as response:
        answer = json.loads(response.read().decode("utf-8"))
    return answer if isinstance(answer, dict) else {}


def main(argv: list[str]) -> int:
    event = argv[1] if len(argv) > 1 else ""
    origin = os.environ.get(_ORIGIN_ENV, "")
    if event not in ("pre", "post") or not origin:
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    payload = {"event": event, "session_id": data.get("session_id"), "cwd": data.get("cwd"),
               "tool_name": data.get("tool_name")}
    if event == "pre":
        payload["tool_input"] = data.get("tool_input")
    try:
        answer = _ask(origin, payload)
    except Exception:  # noqa: BLE001 — fail open (module docstring)
        return 0
    if event == "pre" and isinstance(answer.get("deny"), str) and answer["deny"]:
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                      "permissionDecision": "deny",
                                      "permissionDecisionReason": answer["deny"]}}
    elif event == "post" and isinstance(answer.get("context"), str) and answer["context"]:
        out = {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                      "additionalContext": answer["context"]}}
    else:
        return 0
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # noqa: BLE001 — fail open
        sys.exit(0)
