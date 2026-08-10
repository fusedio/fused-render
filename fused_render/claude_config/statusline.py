"""Statusline viewer (statusline.md).

Read-only introspection of settings.json -> statusLine: the configured command,
the local script it delegates to (leading-comment description + which payload
fields it reads), and a live preview produced by executing the command against
a synthetic sample payload. Never edits the command or the script.

main(action=...):
  get     -> {configured, type, command, script?}   (statusline.md §5)
  preview -> {ok, output, error?}                    (statusline.md §6)

Ports readStatusline / runStatuslinePreview and the pure helpers from
server/lib.ts. Stdlib only.
"""
import json
import os
import subprocess

from . import lib

# statusline.md §4: payload dotted-path -> human label. Matched by exact path OR
# as a dotted prefix (so `.rate_limits.five_hour.used_percentage` maps via
# `.rate_limits.five_hour`). Several paths collapsing to one label is intended.
FIELD_LABELS = {
    ".cwd": "Working directory",
    ".workspace.current_dir": "Working directory",
    ".workspace.project_dir": "Project directory",
    ".workspace.repo": "Repository (owner/name)",
    ".model.display_name": "Model",
    ".model.id": "Model",
    ".effort.level": "Effort level",
    ".context_window.used_percentage": "Context usage %",
    ".context_window.total_input_tokens": "Session tokens",
    ".context_window.total_output_tokens": "Session tokens",
    ".rate_limits.five_hour": "5-hour rate limit",
    ".rate_limits.seven_day": "7-day rate limit",
    ".cost.total_cost_usd": "Session cost / duration",
    ".cost.total_duration_ms": "Session cost / duration",
    ".output_style.name": "Output style",
    ".version": "Claude Code version",
    ".session_id": "Session id",
}

# statusline.md §6: fixed, non-secret sample of Claude Code's statusline payload,
# piped to the command on stdin for the preview. Illustrative values only.
SAMPLE_PAYLOAD = {
    "hook_event_name": "Status",
    "session_id": "sample-session",
    "version": "2.1.199",
    "cwd": os.path.expanduser("~/Work/claude-config-ui"),
    "model": {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"},
    "effort": {"level": "high"},
    "workspace": {
        "current_dir": os.path.expanduser("~/Work/claude-config-ui"),
        "project_dir": os.path.expanduser("~/Work/claude-config-ui"),
        "repo": {"owner": "iamsdas", "name": "claude-config-ui"},
    },
    "output_style": {"name": "default"},
    "context_window": {
        "used_percentage": 42,
        "total_input_tokens": 900,
        "total_output_tokens": 300,
    },
    "rate_limits": {
        "five_hour": {"used_percentage": 18},
        "seven_day": {"used_percentage": 55},
    },
    "cost": {"total_cost_usd": 0.12, "total_duration_ms": 84000},
}


def resolve_script(command):
    """statusline.md §2: the local script a command delegates to, or None.
    Tokenize on whitespace; expand a leading ~/$HOME/$CLAUDE_DIR; return the
    first token resolving to an existing FILE inside CLAUDE_DIR. Trust boundary:
    absolute-only, `..` rejected, must stay under CLAUDE_DIR. Read as text only,
    never executed as a resolved path."""
    home = os.path.expanduser("~")
    base = lib.CLAUDE_DIR
    for raw in command.split():
        tok = raw
        if tok == "~":
            tok = home
        elif tok.startswith("~/"):
            tok = os.path.join(home, tok[2:])
        elif tok.startswith("$HOME/"):
            tok = os.path.join(home, tok[6:])
        elif tok.startswith("$CLAUDE_DIR/"):
            tok = os.path.join(base, tok[12:])
        if not tok.startswith("/") or ".." in tok:
            continue
        if tok != base and not tok.startswith(base + os.sep):
            continue
        if os.path.isfile(tok):
            return tok
    return None


def describe_script(text):
    """statusline.md §3: the leading comment block as a one-line description.
    Skip an optional shebang and leading blanks, join the contiguous `#` run."""
    lines = text.split("\n")
    i = 1 if lines and lines[0].startswith("#!") else 0
    out = []
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        if stripped.startswith("#"):
            out.append(stripped[1:].lstrip())
        elif line.strip() == "" and not out:
            i += 1
            continue
        else:
            break
        i += 1
    return " ".join(out).strip()


def statusline_fields(text):
    """statusline.md §4: which payload fields the script displays. Drop full-line
    comments first, extract dotted paths, map recognized ones to labels; list
    unrecognized dotted paths raw so the label map can lag the schema."""
    import re
    code = "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))
    matches = re.findall(r"\.[A-Za-z_][A-Za-z0-9_.]*", code)
    seen, seen_label = set(), set()
    fields, other = [], []
    for path in matches:
        if path in seen:
            continue
        seen.add(path)
        label = None
        for k, v in FIELD_LABELS.items():
            if path == k or path.startswith(k + "."):
                label = v
                break
        if label:
            if label not in seen_label:
                seen_label.add(label)
                fields.append(label)
        else:
            other.append(path)
    return fields, other


def _get():
    s = lib.read_settings()
    sl = s.get("statusLine")
    if not isinstance(sl, dict):
        return {"configured": False, "type": None, "command": None, "script": None}
    typ = sl.get("type") if isinstance(sl.get("type"), str) else None
    command = sl.get("command") if isinstance(sl.get("command"), str) else None
    script = None
    if command:
        path = resolve_script(command)
        if path:
            try:
                text = lib.read_text(path) or ""
                st = os.stat(path)
                rel = path[len(lib.CLAUDE_DIR) + 1:] if path.startswith(lib.CLAUDE_DIR + os.sep) else path
                tracked = lib.git("ls-files", "--error-unmatch", rel, check=False).strip() != ""
                fields, other = statusline_fields(text)
                script = {
                    "path": path,
                    "tracked": tracked,
                    "size": st.st_size,
                    "modified": _iso(st.st_mtime),
                    "description": describe_script(text),
                    "fields": fields,
                    "otherFields": other,
                }
            except OSError:
                script = None  # unreadable — still show the command
    return {"configured": True, "type": typ, "command": command, "script": script}


def _iso(mtime):
    import datetime
    return datetime.datetime.fromtimestamp(mtime).astimezone().isoformat()


def _preview():
    """statusline.md §6: execute the configured command against SAMPLE_PAYLOAD.
    The one place the app runs an arbitrary user command; guarded: sh -c, sample
    stdin, cwd=CLAUDE_DIR, short timeout, output capped,
    ANSI intact, never throws, mutates nothing."""
    s = lib.read_settings()
    command = (s.get("statusLine") or {}).get("command")
    if not isinstance(command, str) or not command:
        return {"ok": False, "output": "", "error": "no status line configured"}
    try:
        res = subprocess.run(
            ["sh", "-c", command],
            input=json.dumps(SAMPLE_PAYLOAD),
            capture_output=True,
            text=True,
            cwd=lib.CLAUDE_DIR,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "", "error": "preview timed out (5s)"}
    cap = lambda v: v[:4096] if len(v) > 4096 else v
    stdout = cap(res.stdout or "")
    if res.returncode == 0:
        return {"ok": True, "output": stdout}
    stderr = (res.stderr or "").strip()
    return {"ok": False, "output": stdout,
            "error": stderr or f"command exited with code {res.returncode}"}


def main(action: str = "get") -> dict:
    if action == "get":
        return _get()
    if action == "preview":
        return _preview()
    return {"error": f"unknown action: {action}"}
