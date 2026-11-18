"""Claude Code hook for the specialists feature (docs/specialists PLAN A.7, C.2).

agent.py attaches it (through `--settings`) as:

  specialist_hook.py prompt   UserPromptSubmit on a task with the specialist tools (pull by signal)
  specialist_hook.py failure  PostToolUseFailure on a task with the tools (stuck detection, signals)
  specialist_hook.py stop     Stop in a specialist's own main chat / breakout (history, changes, drafts)

Each event posts the hook's stdin (plus "event") to `$FUSED_RENDER_ORIGIN/api/specialists/hook`; when the server
answers `additionalContext`, it is printed as `hookSpecificOutput` so it reaches the model.

FAILS OPEN, always, like canvas_hook.py: no origin, no server, a slow or odd answer — print nothing, exit 0.
Standard library only, copied out of the package by agent.py (`_stable_script`).
"""
import json
import os
import sys
import urllib.parse
import urllib.request

_ORIGIN_ENV = "FUSED_RENDER_ORIGIN"
_TIMEOUT_S = 2.0
_EVENTS = {"prompt": "UserPromptSubmit", "failure": "PostToolUseFailure", "stop": "Stop"}
# What is forwarded of the hook's stdin; the rest (a whole tool response) is not needed.
_KEYS = ("session_id", "cwd", "prompt", "transcript_path", "tool_name", "tool_input", "error",
         "is_interrupt", "hook_event_name", "stop_hook_active")


def _ask(origin: str, payload: dict) -> dict:
    url = origin.rstrip("/") + "/api/specialists/hook"
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


def _live_origin(failed: str) -> str:
    """The origin in `$FUSED_RENDER_HOME_DIR/server.json` when it differs from `failed` and its pid is alive."""
    try:
        home = os.environ.get("FUSED_RENDER_HOME_DIR") or ""
        if not home:
            return ""
        with open(os.path.join(home, "server.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        origin = data.get("origin") if isinstance(data, dict) else None
        pid = data.get("pid") if isinstance(data, dict) else None
        if (not isinstance(origin, str) or not origin.startswith(("http://", "https://"))
                or origin.rstrip("/") == failed.rstrip("/")
                or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0):
            return ""
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass
        except (OSError, OverflowError):
            return ""
        return origin
    except Exception:  # noqa: BLE001
        return ""


def main(argv: list) -> int:
    event = argv[1] if len(argv) > 1 else ""
    origin = os.environ.get(_ORIGIN_ENV, "")
    if event not in _EVENTS or not origin:
        return 0
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    payload = {k: data[k] for k in _KEYS if k in data}
    payload["event"] = event
    try:
        answer = _ask(origin, payload)
    except Exception:  # noqa: BLE001 — fail open
        other = _live_origin(origin)
        if not other:
            return 0
        try:
            answer = _ask(other, payload)
        except Exception:  # noqa: BLE001
            return 0
    context = answer.get("additionalContext")
    if event == "stop" or not isinstance(context, str) or not context:
        return 0
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": _EVENTS[event],
                                                        "additionalContext": context}}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception:  # noqa: BLE001 — fail open
        sys.exit(0)
