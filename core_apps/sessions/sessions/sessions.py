"""Example runPython target: scans local Claude Code session transcripts and
summarizes them for display. Stdlib only."""
import datetime
import json
import os
import sys
import glob


PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
# The fused-render runner (app >= Jul 2026) exec()s the entry file without
# __file__; its preamble puts the script's directory at sys.path[0].
_HERE = (os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals()
         else os.path.abspath(sys.path[0]))

# Mutable state lives in the user-data dir, never next to the scripts — the
# shipped copy is mounted read-only (see ensure_builtin_mounts). Mirrors
# shell/storage.home_dir()'s FUSED_RENDER_HOME override.
_STATE_DIR = os.path.join(
    os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render"),
    "claude-sessions")

NAMES_FILE = os.path.join(_STATE_DIR, "session_names.json")

# Parsed-transcript cache. A full scan reads and json-parses EVERY line of
# every *.jsonl under PROJECTS_DIR — tens of megabytes for a heavy user — and
# each runPython call is a fresh subprocess, so an in-process cache would never
# survive to the next call. The cache therefore lives on disk beside the other
# state, keyed by (path -> file mtime+size): an unchanged transcript is never
# re-parsed, which makes every scan after the first a stat-only pass.
#
# Correctness notes:
#  * The stamp is the file's own (mtime, size), so any append invalidates it.
#  * The volatile "mtime" field (the _activity_mtime badge value, which depends
#    on the wall clock) is NEVER cached — main() recomputes it every call.
#  * The cache is REPLACED, not merged, on each scan: entries for transcripts
#    that no longer exist drop out, so it stays the size of the projects dir.
CACHE_FILE = os.path.join(_STATE_DIR, "summary_cache.json")
CACHE_VERSION = 1


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return {}  # a format change invalidates everything, silently
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _save_cache(entries: dict) -> None:
    # Atomic replace via a pid-suffixed temp file: concurrent runPython
    # subprocesses (the page's poll and a reload can overlap) must never leave
    # a half-written cache behind. A failure here is not worth surfacing — the
    # next scan just re-parses.
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        tmp = "%s.%d.tmp" % (CACHE_FILE, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": CACHE_VERSION, "entries": entries}, f)
        os.replace(tmp, CACHE_FILE)
    except OSError:
        pass


def _load_names() -> dict:
    try:
        with open(NAMES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _decode_project_dir(dirname: str) -> str:
    # Claude Code encodes cwd paths as dir names like "-Users-sina-Desktop-foo".
    if dirname.startswith("-"):
        return "/" + dirname[1:].replace("-", "/")
    return dirname


def _first_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
            if isinstance(block, dict) and "text" in block:
                return block.get("text", "")
    return ""


# Entry types Claude Code appends to a transcript after the turn is over
# (idle housekeeping: away summaries, turn timing, last-prompt records).
# These bump the file mtime but don't mean a session is active.
_HOUSEKEEPING_TYPES = {"system", "last-prompt", "summary"}


def _activity_mtime(path: str) -> float:
    """File mtime, except housekeeping-only tail writes don't count as activity.

    Cheap: only tail-parses the file when the mtime is fresh enough to matter
    for the "running" badge (last 90s); otherwise returns raw mtime.
    """
    mtime = os.path.getmtime(path)
    now = datetime.datetime.now().timestamp()
    if now - mtime > 90:
        return mtime
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16384))
            chunk = f.read().decode("utf-8", "replace")
    except OSError:
        return mtime
    lines = [ln for ln in chunk.split("\n") if ln.strip()]
    if size > 16384 and lines:
        lines = lines[1:]  # drop the partial first line from mid-file seek
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            return mtime  # partial last line: a write is literally in flight
        if obj.get("type") in _HOUSEKEEPING_TYPES:
            # a turn_duration entry newer than any real message means the
            # turn just finished — the session is idle right now, so don't
            # let the trailing 45s window keep the badge lit
            if obj.get("subtype") == "turn_duration":
                return 0.0
            continue
        ts = obj.get("timestamp")
        if ts:
            try:
                return datetime.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")
                ).timestamp()
            except ValueError:
                return mtime
    # only housekeeping entries in the tail — not real activity
    return 0.0


def _summarize_session(path: str, project_dirname: str) -> dict:
    """Parse one transcript into a summary. Everything here is derived from the
    file's CONTENT, so the result is cacheable against the file's mtime — the
    clock-dependent "mtime" activity field is added by main(), never here."""
    session_id = os.path.splitext(os.path.basename(path))[0]
    first_ts = None
    last_ts = None
    cwd = None
    git_branch = None
    first_prompt = ""
    user_count = 0
    assistant_count = 0
    tool_calls = 0
    models = set()

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue

                ts = obj.get("timestamp")
                if ts:
                    if first_ts is None:
                        first_ts = ts
                    last_ts = ts

                if cwd is None and obj.get("cwd"):
                    cwd = obj.get("cwd")
                if git_branch is None and obj.get("gitBranch"):
                    git_branch = obj.get("gitBranch")

                msg = obj.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")

                if role == "user" and obj.get("type") == "user":
                    content = msg.get("content")
                    text = _first_text(content)
                    if text:
                        user_count += 1
                        if not first_prompt:
                            first_prompt = text.strip()

                elif role == "assistant":
                    assistant_count += 1
                    model = msg.get("model")
                    if model:
                        models.add(model)
                    content = msg.get("content")
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_calls += 1
    except OSError:
        return None

    if first_ts is None:
        return None

    project_path = _decode_project_dir(project_dirname)
    title = first_prompt[:140] if first_prompt else "(no user message)"

    return {
        "sessionId": session_id,
        "project": project_path,
        "cwd": cwd or project_path,
        "transcriptPath": path,
        "gitBranch": git_branch,
        "startedAt": first_ts,
        "endedAt": last_ts,
        "userMessages": user_count,
        "assistantMessages": assistant_count,
        "toolCalls": tool_calls,
        "models": sorted(models),
        "title": title,
    }


def _find_session_path(session_id: str):
    if not os.path.isdir(PROJECTS_DIR):
        return None, None
    for project_dirname in os.listdir(PROJECTS_DIR):
        candidate = os.path.join(PROJECTS_DIR, project_dirname, f"{session_id}.jsonl")
        if os.path.isfile(candidate):
            return candidate, project_dirname
    return None, None


def _text_and_tool_uses(content):
    text_parts = []
    tool_uses = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_uses.append({
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input"),
                    "result": None,
                    "isError": False,
                })
    return "\n".join(p for p in text_parts if p), tool_uses


def _tool_results(content):
    results = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                result_content = block.get("content")
                if isinstance(result_content, list):
                    result_text = "\n".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                else:
                    result_text = result_content if isinstance(result_content, str) else json.dumps(result_content)
                results.append({
                    "id": block.get("tool_use_id"),
                    "text": result_text,
                    "isError": bool(block.get("is_error")),
                })
    return results


def session_detail(session_id: str) -> dict:
    path, project_dirname = _find_session_path(session_id)
    if not path:
        return {"sessionId": session_id, "found": False, "turns": []}

    turns = []
    tool_by_id = {}
    current_assistant = None
    cwd = None
    project_path = _decode_project_dir(project_dirname)

    def flush_assistant():
        nonlocal current_assistant
        if current_assistant is not None:
            turns.append(current_assistant)
            current_assistant = None

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue

            if cwd is None and obj.get("cwd"):
                cwd = obj.get("cwd")

            msg = obj.get("message")
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            ts = obj.get("timestamp")
            content = msg.get("content")

            if role == "user" and obj.get("type") == "user":
                results = _tool_results(content)
                if results:
                    for r in results:
                        tool = tool_by_id.get(r["id"])
                        if tool:
                            tool["result"] = r["text"]
                            tool["isError"] = r["isError"]
                    continue

                text, _ = _text_and_tool_uses(content)
                if text.strip():
                    flush_assistant()
                    turns.append({
                        "role": "user",
                        "timestamp": ts,
                        "text": text.strip(),
                        "toolCalls": [],
                    })

            elif role == "assistant":
                text, tool_uses = _text_and_tool_uses(content)
                if current_assistant is None:
                    current_assistant = {
                        "role": "assistant",
                        "timestamp": ts,
                        "text": "",
                        "toolCalls": [],
                    }
                if text:
                    current_assistant["text"] = (current_assistant["text"] + "\n" + text).strip()
                for tu in tool_uses:
                    current_assistant["toolCalls"].append(tu)
                    if tu.get("id"):
                        tool_by_id[tu["id"]] = tu

    flush_assistant()

    return {
        "sessionId": session_id,
        "found": True,
        "project": project_path,
        "cwd": cwd or project_path,
        "turns": turns,
    }


def main(search: str = "", project: str = "", limit: int = 500, mode: str = "") -> dict:
    # mode="status": cheap poll — just session ids and file mtimes, no parsing.
    # Used by the UI's 1s "running" indicator refresh.
    if mode == "status":
        statuses = []
        if os.path.isdir(PROJECTS_DIR):
            for project_dirname in os.listdir(PROJECTS_DIR):
                project_dir = os.path.join(PROJECTS_DIR, project_dirname)
                if not os.path.isdir(project_dir):
                    continue
                for jsonl_path in glob.glob(os.path.join(project_dir, "*.jsonl")):
                    try:
                        mtime = _activity_mtime(jsonl_path)
                    except OSError:
                        continue
                    statuses.append({
                        "sessionId": os.path.splitext(os.path.basename(jsonl_path))[0],
                        "mtime": datetime.datetime.fromtimestamp(
                            mtime, datetime.timezone.utc
                        ).isoformat(),
                    })
        return {"statuses": statuses}

    sessions = []
    # Every session id seen on disk, INCLUDING transcripts that produce no
    # summary (a just-created file with no timestamped line yet, or one holding
    # only housekeeping entries) and the ones the `limit` truncates away. The
    # page's cheap status poll compares its ids against this set to decide "a
    # new session appeared → full reload"; without the dropped ids in it, one
    # unsummarizable file makes every poll trigger a full rescan forever.
    all_ids = []
    cache = _load_cache()
    fresh = {}
    if os.path.isdir(PROJECTS_DIR):
        for project_dirname in sorted(os.listdir(PROJECTS_DIR)):
            project_dir = os.path.join(PROJECTS_DIR, project_dirname)
            if not os.path.isdir(project_dir):
                continue
            for jsonl_path in glob.glob(os.path.join(project_dir, "*.jsonl")):
                all_ids.append(os.path.splitext(os.path.basename(jsonl_path))[0])
                try:
                    st = os.stat(jsonl_path)
                except OSError:
                    continue
                # list, not tuple: the stamp round-trips through JSON
                stamp = [st.st_mtime, st.st_size]
                hit = cache.get(jsonl_path)
                if isinstance(hit, dict) and hit.get("stamp") == stamp:
                    cached = hit.get("summary")
                    if not isinstance(cached, dict):
                        cached = None  # a miss OR a remembered "no summary"
                else:
                    cached = _summarize_session(jsonl_path, project_dirname)
                # Cached as parsed — content only, no clock-dependent field, so
                # the entry is comparable and can never go stale in place.
                fresh[jsonl_path] = {"stamp": stamp, "summary": cached}
                if cached:
                    summary = dict(cached)
                    # activity mtime catches in-flight writes (thinking/tool
                    # output) that haven't produced a message timestamp yet, but
                    # ignores post-turn housekeeping appends — used for
                    # "running". Clock-dependent, so never served from cache.
                    try:
                        activity = _activity_mtime(jsonl_path)
                    except OSError:
                        activity = st.st_mtime
                    summary["mtime"] = datetime.datetime.fromtimestamp(
                        activity, datetime.timezone.utc
                    ).isoformat()
                    sessions.append(summary)
    if fresh != cache:
        _save_cache(fresh)

    names = _load_names()
    for s in sessions:
        custom = names.get(s["sessionId"])
        s["name"] = custom if custom else s["title"]
        s["hasCustomName"] = bool(custom)

    sessions.sort(key=lambda s: s["endedAt"] or "", reverse=True)

    search_lc = search.strip().lower()
    project_lc = project.strip().lower()
    if search_lc:
        sessions = [
            s for s in sessions
            if search_lc in s["name"].lower() or search_lc in s["title"].lower() or search_lc in s["project"].lower()
        ]
    if project_lc:
        sessions = [s for s in sessions if project_lc in s["project"].lower()]

    total = len(sessions)
    sessions = sessions[: max(0, int(limit))]

    total_user_msgs = sum(s["userMessages"] for s in sessions)
    total_tool_calls = sum(s["toolCalls"] for s in sessions)
    projects = sorted({s["project"] for s in sessions})

    return {
        "sessions": sessions,
        "allSessionIds": sorted(all_ids),
        "total": total,
        "totalUserMessages": total_user_msgs,
        "totalToolCalls": total_tool_calls,
        "projects": projects,
    }


# The fused-render runner (app >= Jul 2026) only invokes @fused.udf-registered
# entrypoints; a bare main() silently returns null. Register main via the shim.
try:
    import fused as _fused
    _udf_main = _fused.udf(main)
except ImportError:
    pass
