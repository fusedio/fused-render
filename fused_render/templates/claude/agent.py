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
      -> {"text": ..., "done": bool, "session_id": ..., "error": ..., "tokens": N,
          "phase": ..., "message": <the run's first message, for re-attach>}
  main(action="sessions", file=...)   -> {"sessions": [...]}   (sidecar only)
  main(action="history", file=..., session_id=...) -> {"turns": [...]}
  main(action="cancel", run_id=...)   -> {"cancelled": ...}
"""
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time

RUNS = os.path.join(tempfile.gettempdir(), "fused_render_claude", "runs")
PROJECTS = os.path.expanduser("~/.claude/projects")


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


def _mount_read_only(file: str) -> bool:
    """True when `file` sits under a read-only remote mount, where the sidecar
    write can never be accepted — with CacheMode=full the doomed upload lands
    in the VFS cache and 403-loops forever (the sidecar-write incident).

    Guarded lazy import: in the app this reads the mount store; a standalone
    copy of this template (no fused_render on the path) degrades to False, the
    pre-guard behavior. Deliberately not the stdlib-only rule the rest of this
    file follows (cf. templates/zarr_aoi/tile_server.py, which also reaches for
    a fused_render internal) — os.access(W_OK) can't see a remote's read-only
    -ness, so only the shell's flag can answer this."""
    try:
        from fused_render.shell.mounts import mount_read_only
        return mount_read_only(file)
    except Exception:
        return False


def _load_sidecar(file: str) -> dict:
    # Preserve every key we don't own (bookmarkHistory, lastSession, ...) so a
    # claude turn round-trips them instead of clobbering them off disk. Only the
    # claudeSessions key is normalised to a list. The remaining loss window is a
    # true read-modify-write interleave between the two writers (both read the
    # old file, both write) — acceptable under D3 (single local user, both
    # writes human-paced).
    try:
        with open(_sidecar_path(file), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        data = None
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("claudeSessions"), list):
        data["claudeSessions"] = []
    return data


def _save_sidecar(file: str, data: dict) -> None:
    path = _sidecar_path(file)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
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

    Plain --resume keeps the session id, but --fork-session (and older
    claude versions) mint a new one — a resumed turn therefore replaces the
    old entry's id in place (keeping created_at/preview) so one conversation
    stays one row. `cwd` tracks where the transcript lives so a moved file
    can migrate it (see _migrate_session); refreshed every turn.

    No-op when `file` is inside a read-only remote mount: the sidecar write
    can't be accepted there (the sidecar-write incident). The chat and its
    transcript (~/.claude/projects) are unaffected — only this file's session
    list stays empty, so past conversations won't be listed/resumable from the
    template UI for a mounted file.
    """
    if _mount_read_only(file):
        return
    data = _load_sidecar(file)
    now = time.time()
    cwd = os.path.dirname(file)
    for entry in data["claudeSessions"]:
        if entry.get("id") in (session_id, resumed_from):
            entry["id"] = session_id
            entry["last_used"] = now
            entry["cwd"] = cwd
            return _save_sidecar(file, data)
    data["claudeSessions"].append({
        "id": session_id,
        "preview": message.strip()[:80],
        "created_at": now,
        "last_used": now,
        "cwd": cwd,
    })
    _save_sidecar(file, data)


# ---------------------------------------------------------- session transfer

def _munge(path: str) -> str:
    """A cwd's project-dir name under ~/.claude/projects: every
    non-alphanumeric char becomes '-' (claude-code's own rule, verified
    against real project dirs — '/', '.', '_' all map to '-')."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(path))


def _migrate_session(file: str, session_id: str) -> None:
    """Copy-on-resume: claude's --resume only finds transcripts under the
    CURRENT cwd's project dir, so when the target file (plus sidecar) has
    been moved, copy the transcript from the sidecar's recorded `cwd` into
    the new directory's project dir. No-op when it is already there; never
    overwrites an existing destination (the destination is where new turns
    append — it is always the newer copy). Best-effort: any failure just
    means claude reports the session as not found."""
    if not session_id or "/" in session_id:
        return
    new_cwd = os.path.dirname(file)
    dest_dir = os.path.join(PROJECTS, _munge(new_cwd))
    dest = os.path.join(dest_dir, session_id + ".jsonl")

    data = _load_sidecar(file)
    entry = next((e for e in data["claudeSessions"] if e.get("id") == session_id), None)

    if not os.path.exists(dest):
        old_cwd = (entry or {}).get("cwd", "")
        if not old_cwd or os.path.abspath(old_cwd) == os.path.abspath(new_cwd):
            return  # nowhere to copy from
        src = os.path.join(PROJECTS, _munge(old_cwd), session_id + ".jsonl")
        if not os.path.isfile(src):
            return  # transcript gone; claude will surface the error
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.copy2(src, dest)
        except OSError:
            return

    # keep the sidecar's cwd truthful so later resumes skip straight through
    if entry is not None and entry.get("cwd") != new_cwd:
        entry["cwd"] = new_cwd
        try:
            _save_sidecar(file, data)
        except OSError:
            pass


# ----------------------------------------------------------------- start/poll

def _start(file: str, message: str, session_id: str, model: str,
           effort: str) -> dict:
    file = os.path.abspath(file)
    if not os.path.isfile(file):
        return {"error": f"target file not found: {file}"}
    if session_id:
        _migrate_session(file, session_id)

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
    with open(os.path.join(run_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"file": file, "message": message,
                   "resumed_from": session_id}, f)

    with open(os.path.join(run_dir, "out.jsonl"), "w", encoding="utf-8") as out, \
         open(os.path.join(run_dir, "err.log"), "w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, stdout=out, stderr=err,
                                cwd=os.path.dirname(file),
                                stdin=subprocess.DEVNULL,
                                start_new_session=True)
    with open(os.path.join(run_dir, "pid"), "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    return {"run_id": run_id}


def _alive(run_dir: str) -> bool:
    try:
        pid = int(open(os.path.join(run_dir, "pid"), encoding="utf-8").read())
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
    pending_sep = False  # a message ended; separate it from the next one's text

    try:
        lines = open(os.path.join(run_dir, "out.jsonl"), encoding="utf-8",
                     errors="replace").read().splitlines()
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
                    if pending_sep:
                        text_parts.append("\n\n")
                        pending_sep = False
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
                # A tool-using turn is several assistant messages; without a
                # break their texts concatenate mid-word ("orange.After").
                pending_sep = bool(text_parts)
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
        # Dead without a `result` row = abnormal exit (crash, OOM, cancel),
        # even if some text streamed first. Report it as an error regardless
        # of partial text, so the UI doesn't render a truncated reply as a
        # clean success and the sidecar-record guard below skips it.
        done = True
        try:
            tail = open(os.path.join(run_dir, "err.log"), encoding="utf-8",
                        errors="replace").read().strip()
        except FileNotFoundError:
            tail = ""
        error = tail or ("claude exited before completing the reply"
                         if text_parts else "claude exited unexpectedly")

    # The run's own first message rides back on every poll so a re-attaching
    # page (mode switch / reload killed the poll loop, subprocess kept going)
    # can restore the user turn it never saw.
    try:
        with open(os.path.join(run_dir, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            meta = {}
    except (OSError, json.JSONDecodeError):
        meta = {}

    # First poll that sees the session id writes it to the sidecar (marker
    # file keeps the write one-shot across the remaining polls).
    marker = os.path.join(run_dir, "recorded")
    if new_session and not error and not os.path.exists(marker) and "file" in meta:
        try:
            _record_session(meta["file"], new_session, meta.get("message", ""),
                            meta.get("resumed_from", ""))
            open(marker, "w", encoding="utf-8").close()
        except OSError:
            pass  # sidecar bookkeeping must never break the chat itself

    # The streamed deltas are the full turn; the `result` row holds only the
    # LAST assistant message, so swapping to it after a tool-using turn threw
    # away every earlier message (the mid-sentence-freeze bug). Keep the
    # accumulated stream; fall back to `result` only when nothing streamed
    # (older CLI without --include-partial-messages).
    text = "".join(text_parts)
    if not text and done and result_text and not error:
        text = result_text
    return {"text": text, "done": done, "session_id": new_session, "error": error,
            "tokens": tokens_done + tokens_current, "phase": phase,
            "message": meta.get("message", "")}


# ------------------------------------------------------- sessions & history

def _sessions(file: str) -> dict:
    """Sessions recorded in THIS file's sidecar, newest activity first."""
    file = os.path.abspath(file)
    sessions = sorted(_load_sidecar(file)["claudeSessions"],
                      key=lambda s: s.get("last_used", 0), reverse=True)
    return {"sessions": sessions}


def _history(file: str, session_id: str) -> dict:
    """Rebuild the conversation from the Claude Code session transcript.

    Resolved ONLY at the target file's own project dir — with copied files
    the same session id exists in several project dirs with divergent
    content, and a glob would render some other copy's conversation while
    resume continues this one's. Migrates first (same as `start`) so a moved
    file's saved session shows its turns immediately, without waiting for the
    user to send a message."""
    if not session_id or "/" in session_id:
        return {"turns": []}
    file = os.path.abspath(file)
    _migrate_session(file, session_id)
    path = os.path.join(PROJECTS, _munge(os.path.dirname(file)),
                        session_id + ".jsonl")
    if not os.path.isfile(path):
        return {"turns": []}

    turns = []
    for line in open(path, encoding="utf-8", errors="replace"):
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
    # Same guard as _poll: run_id is joined into a path and drives a kill,
    # so reject anything that could resolve outside the runs dir.
    if "/" in run_id or run_id.startswith(".") or not os.path.isdir(run_dir):
        return {"cancelled": run_id}
    try:
        pid = int(open(os.path.join(run_dir, "pid"), encoding="utf-8").read())
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
        if not file:
            return {"error": "missing target file (no _file param?)"}
        return _history(file, session_id)
    if action == "cancel":
        return _cancel(run_id)
    return {"error": f"unknown action: {action}"}
