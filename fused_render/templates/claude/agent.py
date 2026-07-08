"""runPython target for claude/template.html: chat with the Claude Code CLI
about the target file (POC).

The browser never owns the work: `start` detaches a claude subprocess whose
stream-json stdout goes to a log file in tmp; `poll` re-reads that file and
returns the accumulated assistant text so the page can render the reply as it
streams in. Stdlib only.

Sessions are per-file. Every conversation started from this template is
recorded in a sidecar next to the target file — `<file>.json`, e.g.
`my-folder/sample.html` -> `my-folder/sample.html.json` — and the template
lists ONLY the sessions in that sidecar, never the user's global session
history. Claude runs with cwd = the target file's directory and an appended
system prompt that scopes it (softly) to the file.

Actions:
  main(action="start", file=..., message=..., session_id="", model="", effort="")
      -> {"run_id": ...}
  main(action="poll", run_id=...)
      -> {"text": ..., "done": bool, "session_id": ..., "error": ..., "tokens": N, "phase": ...}
  main(action="sessions", file=...)   -> {"sessions": [...]}   (sidecar only)
  main(action="history", session_id=...) -> {"turns": [...]}
  main(action="cancel", run_id=...)   -> {"cancelled": ...}
"""
import glob
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

RUNS = os.path.join(tempfile.gettempdir(), "fused_render_claude", "runs")


def _claude_bin() -> str:
    found = shutil.which("claude")
    if found:
        return found
    for candidate in ("~/.local/bin/claude", "/opt/homebrew/bin/claude",
                      "/usr/local/bin/claude"):
        candidate = os.path.expanduser(candidate)
        if os.path.exists(candidate):
            return candidate
    raise FileNotFoundError(
        "claude CLI not found — install Claude Code or put `claude` on the "
        "PATH of the environment that launched fused-render"
    )


def _system_prompt(file: str) -> str:
    name = os.path.basename(file)
    return (
        f"You are embedded in a local file viewer, opened on {file}. "
        f"The user is looking at {name} right now; treat that file as the "
        "subject of this conversation — answer questions about it and make "
        "requested edits to it. Keep your work scoped to this file (and "
        "assets it directly references) unless the user explicitly asks for "
        "something broader. This is guidance, not a hard rule: follow "
        "explicit user instructions even when they go beyond the file."
    )


# ------------------------------------------------------------- sidecar store

def _sidecar_path(file: str) -> str:
    return file + ".json"


def _load_sidecar(file: str) -> dict:
    try:
        with open(_sidecar_path(file)) as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("sessions"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"sessions": []}


def _save_sidecar(file: str, data: dict) -> None:
    path = _sidecar_path(file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _record_session(file: str, session_id: str, message: str,
                    resumed_from: str) -> None:
    """Add/refresh a sidecar entry.

    Claude Code mints a NEW session id on every --resume, so a resumed turn
    must replace the old entry's id in place (keeping created_at/preview) —
    otherwise every turn of one conversation shows up as a separate session.
    """
    data = _load_sidecar(file)
    now = time.time()
    for entry in data["sessions"]:
        if entry.get("id") in (session_id, resumed_from):
            entry["id"] = session_id
            entry["last_used"] = now
            return _save_sidecar(file, data)
    data["sessions"].append({
        "id": session_id,
        "preview": message.strip()[:80],
        "created_at": now,
        "last_used": now,
    })
    _save_sidecar(file, data)


# ----------------------------------------------------------------- start/poll

def _start(file: str, message: str, session_id: str, model: str,
           effort: str) -> dict:
    file = os.path.abspath(file)
    if not os.path.isfile(file):
        return {"error": f"target file not found: {file}"}

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    run_dir = os.path.join(RUNS, run_id)
    os.makedirs(run_dir)

    cmd = [_claude_bin(), "-p", message,
           "--output-format", "stream-json",
           "--verbose", "--include-partial-messages",
           "--append-system-prompt", _system_prompt(file),
           "--permission-mode", "acceptEdits"]
    if session_id:
        cmd += ["--resume", session_id]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]

    # poll() records the session into the sidecar once claude reports its id;
    # it needs the file + first message, so stash them with the run.
    with open(os.path.join(run_dir, "meta.json"), "w") as f:
        json.dump({"file": file, "message": message,
                   "resumed_from": session_id}, f)

    with open(os.path.join(run_dir, "out.jsonl"), "w") as out, \
         open(os.path.join(run_dir, "err.log"), "w") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err,
                                cwd=os.path.dirname(file),
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)
    with open(os.path.join(run_dir, "pid"), "w") as f:
        f.write(str(proc.pid))
    return {"run_id": run_id}


def _alive(run_dir: str) -> bool:
    try:
        pid = int(open(os.path.join(run_dir, "pid")).read())
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _poll(run_id: str) -> dict:
    run_dir = os.path.join(RUNS, run_id)
    if "/" in run_id or run_id.startswith(".") or not os.path.isdir(run_dir):
        return {"text": "", "done": True, "session_id": "", "error": "unknown run_id"}

    text_parts = []
    result_text = None
    new_session = ""
    done = False
    error = ""
    tokens_done = 0      # output tokens of finished messages this turn
    tokens_current = 0   # cumulative usage of the in-flight message
    phase = "thinking"

    try:
        lines = open(os.path.join(run_dir, "out.jsonl")).read().splitlines()
    except FileNotFoundError:
        lines = []

    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # half-written last line; next poll gets it
        t = row.get("type")
        if t == "system":
            new_session = row.get("session_id", new_session)
        elif t == "stream_event":
            ev = row.get("event", {})
            et = ev.get("type")
            if et == "content_block_delta":
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    text_parts.append(delta.get("text", ""))
                    phase = "composing"
                elif delta.get("type") == "thinking_delta":
                    phase = "thinking"
            elif et == "message_delta":
                usage = ev.get("usage") or {}
                tokens_current = usage.get("output_tokens", tokens_current)
            elif et == "message_stop":
                tokens_done += tokens_current
                tokens_current = 0
            elif et == "content_block_start":
                block = (ev.get("content_block") or {}).get("type")
                if block == "tool_use":
                    phase = "tooling"
        elif t == "result":
            done = True
            new_session = row.get("session_id", new_session)
            result_text = row.get("result")
            if row.get("is_error"):
                error = str(result_text or "claude exited with an error")

    if not done and not _alive(run_dir):
        done = True
        if not text_parts:
            try:
                error = open(os.path.join(run_dir, "err.log")).read().strip() \
                    or "claude exited unexpectedly"
            except FileNotFoundError:
                error = "claude exited unexpectedly"

    # First poll that sees the session id writes it to the sidecar (marker
    # file keeps the write one-shot across the remaining polls).
    marker = os.path.join(run_dir, "recorded")
    if new_session and not error and not os.path.exists(marker):
        try:
            with open(os.path.join(run_dir, "meta.json")) as f:
                meta = json.load(f)
            _record_session(meta["file"], new_session, meta["message"],
                            meta.get("resumed_from", ""))
            open(marker, "w").close()
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # sidecar bookkeeping must never break the chat itself

    # The final result is authoritative; partial deltas cover the streaming window.
    text = result_text if (done and result_text and not error) else "".join(text_parts)
    return {"text": text, "done": done, "session_id": new_session, "error": error,
            "tokens": tokens_done + tokens_current, "phase": phase}


# ------------------------------------------------------- sessions & history

def _sessions(file: str) -> dict:
    """Sessions recorded in THIS file's sidecar, newest activity first."""
    file = os.path.abspath(file)
    sessions = sorted(_load_sidecar(file)["sessions"],
                      key=lambda s: s.get("last_used", 0), reverse=True)
    return {"sessions": sessions}


def _history(session_id: str) -> dict:
    """Rebuild the conversation from the Claude Code session transcript on disk."""
    if not session_id or "/" in session_id:
        return {"turns": []}
    matches = glob.glob(os.path.expanduser(
        f"~/.claude/projects/*/{session_id}.jsonl"))
    if not matches:
        return {"turns": []}

    turns = []
    for line in open(matches[0], errors="replace"):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("isMeta") or row.get("isSidechain"):
            continue
        msg = row.get("message") or {}
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                text = content
            else:
                text = "\n".join(b.get("text", "") for b in content
                                 if isinstance(b, dict) and b.get("type") == "text")
            if text.strip() and not text.startswith(("<local-command", "<command-name")):
                turns.append({"role": "user", "text": text})
        elif role == "assistant" and isinstance(content, list):
            text = "\n".join(b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text")
            if text.strip():
                # consecutive assistant rows are one streamed turn; keep merged
                if turns and turns[-1]["role"] == "assistant":
                    turns[-1]["text"] += "\n" + text
                else:
                    turns.append({"role": "assistant", "text": text})
    return {"turns": turns}


def _cancel(run_id: str) -> dict:
    run_dir = os.path.join(RUNS, run_id)
    try:
        pid = int(open(os.path.join(run_dir, "pid")).read())
        os.killpg(pid, signal.SIGTERM)  # start_new_session=True -> pid is pgid
    except (OSError, ValueError):
        pass
    return {"cancelled": run_id}


def main(action: str = "start", file: str = "", message: str = "",
         session_id: str = "", model: str = "", effort: str = "",
         run_id: str = "") -> dict:
    if action == "start":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        if not message:
            return {"error": "(empty message)"}
        return _start(file, message, session_id, model, effort)
    if action == "poll":
        return _poll(run_id)
    if action == "sessions":
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _sessions(file)
    if action == "history":
        return _history(session_id)
    if action == "cancel":
        return _cancel(run_id)
    return {"error": f"unknown action: {action}"}
