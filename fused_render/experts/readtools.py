"""The only way an expert touches the disk (docs/experts.md §4 `readtools.py`).

Three read-only tools — `read_file`, `list_dir`, `search` — fenced to a list of
root folders. Every path is realpath'd before the fence (`bots.bot.inside`),
so a symlink that points out of the roots is refused like any outside path.
Outputs are capped; the handler never raises.
"""
from __future__ import annotations

import fnmatch
import os

READ_PAGE_CHARS = 20_000
LIST_MAX = 200
SEARCH_MAX = 50
SEARCH_DEPTH = 8
SEARCH_FILE_MAX = 2 * 1024 * 1024
SEARCH_LINE = 200
SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__"})


def _canvas_roots(scopes) -> list[str]:
    out = []
    try:
        from fused_render import canvases
        base = canvases.canvases_base()
        try:
            from fused_render import canvas_marker
            known = tuple(canvas_marker.SCOPES)
        except Exception:  # noqa: BLE001
            known = ("user", "team", "public")
    except Exception:  # noqa: BLE001
        return out
    wanted = [s for s in (scopes or known) if s in known]
    try:
        envs = sorted(os.listdir(base))
    except OSError:
        return out
    for env in envs:
        if env.startswith("."):
            continue
        env_root = os.path.join(base, env)
        if not os.path.isdir(env_root):
            continue
        for scope in wanted:
            p = os.path.join(env_root, scope)
            if os.path.isdir(p):
                out.append(p)
    return out


def resolve_reads(manifest) -> list[str]:
    """The manifest's `reads` as absolute existing folders (realpath, no duplicates)."""
    out: list[str] = []
    reads = (manifest or {}).get("reads") or []
    for r in reads:
        if not isinstance(r, dict):
            continue
        try:
            if r.get("kind") == "canvas-mirror":
                scopes = r.get("scopes") if isinstance(r.get("scopes"), list) else None
                found = _canvas_roots(scopes)
            elif r.get("kind") == "folder":
                p = os.path.expanduser(str(r.get("path") or ""))
                found = [p] if p and os.path.isabs(p) and os.path.isdir(p) else []
            else:
                found = []
        except Exception:  # noqa: BLE001
            found = []
        for p in found:
            real = os.path.realpath(p)
            if real not in out:
                out.append(real)
    return out


def specs() -> list[dict]:
    return [
        {"name": "read_file",
         "description": "Read one file inside the folders you may read. Text comes in pages of "
                        f"{READ_PAGE_CHARS} characters; ask for the next `page` when the header says there is more.",
         "parameters": {"type": "object", "properties": {
             "path": {"type": "string", "description": "Absolute path, or relative to the only folder."},
             "page": {"type": "integer", "minimum": 1, "description": "1-based page, default 1."}},
             "required": ["path"]}},
        {"name": "list_dir",
         "description": "List a folder's entries (folders end with /). With no path, lists the folders you may read.",
         "parameters": {"type": "object", "properties": {
             "path": {"type": "string", "description": "A folder inside the readable folders."}}}},
        {"name": "search",
         "description": "Find files whose NAME contains every word of `query`; with `content: true`, find lines "
                        f"inside text files containing `query` instead. At most {SEARCH_MAX} hits. "
                        "`glob` (e.g. *.py) narrows the files; `path` narrows the folder.",
         "parameters": {"type": "object", "properties": {
             "query": {"type": "string"},
             "path": {"type": "string"},
             "content": {"type": "boolean"},
             "glob": {"type": "string"}},
             "required": ["query"]}},
    ]


def _inside(path, roots):
    try:
        from fused_render.bots.bot import inside
        return inside(path, roots)
    except ImportError:
        real = os.path.realpath(path)
        for r in roots:
            try:
                if os.path.commonpath([real, r]) == r:
                    return r
            except ValueError:
                continue
        return None


def make_handler(roots, on_tool=None):
    """`handler(name, input) -> (text, is_error)` over `roots`. Never raises."""
    roots = [] if roots is None else roots
    real_roots: list[str] = []
    for r in roots:
        try:
            rr = os.path.realpath(os.path.expanduser(str(r)))
        except Exception:  # noqa: BLE001
            continue
        if os.path.isdir(rr) and rr not in real_roots:
            real_roots.append(rr)

    def allowed(path):
        if not real_roots:
            return None
        try:
            return _inside(path, real_roots)
        except Exception:  # noqa: BLE001
            return None

    def resolve(raw):
        """(realpath, None) or (None, refusal)."""
        raw = str(raw or "").strip()
        if not raw:
            return None, "error: a path is needed"
        p = os.path.expanduser(raw)
        if not os.path.isabs(p):
            candidates = [os.path.join(r, p) for r in real_roots]
            existing = [c for c in candidates if os.path.lexists(c)]
            if not candidates:
                return None, f"not allowed: {raw} (no readable folders)"
            p = (existing or candidates)[0]
        real = os.path.realpath(p)
        if allowed(real) is None:
            return None, f"not allowed: {raw} is outside the folders this expert may read"
        return real, None

    def do_read(inp):
        real, err = resolve(inp.get("path"))
        if err:
            return err, True
        if os.path.isdir(real):
            return f"error: {real} is a folder; use list_dir", True
        if not os.path.isfile(real):
            return f"error: no such file: {real}", True
        try:
            page = max(1, int(inp.get("page") or 1))
        except (TypeError, ValueError):
            page = 1
        from fused_render.bots import filereader
        text, jpeg = filereader.read_file(real, page)
        if jpeg is not None:
            return f"{os.path.basename(real)} is an image; this tool reads text only.", True
        is_err = text.startswith("error:")
        if len(text) > READ_PAGE_CHARS + 2000:
            text = text[:READ_PAGE_CHARS + 2000] + "\n…(cut)"
        return f"{real}\n{text}", is_err

    def do_list(inp):
        raw = inp.get("path")
        if not raw:
            if not real_roots:
                return "no readable folders", True
            return "Readable folders:\n" + "\n".join(f"- {r}/" for r in real_roots), False
        real, err = resolve(raw)
        if err:
            return err, True
        if not os.path.isdir(real):
            return f"error: not a folder: {real}", True
        try:
            names = sorted(os.listdir(real))
        except OSError as e:
            return f"error: cannot list {real}: {e.strerror or e}", True
        rows = []
        for n in names:
            if n.startswith("."):
                continue
            full = os.path.join(real, n)
            if os.path.islink(full) and allowed(os.path.realpath(full)) is None:
                continue  # a link out of the fence is not shown
            rows.append(n + "/" if os.path.isdir(full) else n)
        more = len(rows) - LIST_MAX
        rows = rows[:LIST_MAX]
        out = f"{real}/ ({len(rows) + max(0, more)} entries)\n" + "\n".join(rows)
        if more > 0:
            out += f"\n…and {more} more (search narrows it)"
        return out, False

    def do_search(inp):
        from fused_render.bots import filereader
        query = str(inp.get("query") or "").strip()
        if not query:
            return "error: search needs `query`", True
        glob = str(inp.get("glob") or "").strip()
        content = bool(inp.get("content"))
        if inp.get("path"):
            start, err = resolve(inp.get("path"))
            if err:
                return err, True
            if not os.path.isdir(start):
                return f"error: not a folder: {start}", True
            starts = [start]
        else:
            starts = list(real_roots)
        if not starts:
            return "no readable folders", True
        words = [w.lower() for w in query.split()]
        q = query.lower()
        hits: list[str] = []
        scanned = 0
        for root in starts:
            base_depth = root.rstrip(os.sep).count(os.sep)
            for cur, dirs, files in os.walk(root):  # followlinks=False
                dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIP_DIRS)
                if cur.count(os.sep) - base_depth >= SEARCH_DEPTH:
                    dirs[:] = []
                for n in sorted(files):
                    if len(hits) >= SEARCH_MAX:
                        break
                    if n.startswith("."):
                        continue
                    if glob and not fnmatch.fnmatch(n, glob):
                        continue
                    full = os.path.join(cur, n)
                    if os.path.islink(full) and allowed(os.path.realpath(full)) is None:
                        continue
                    if not content:
                        if all(w in n.lower() for w in words):
                            hits.append(f"- {full}")
                        continue
                    try:
                        if os.path.getsize(full) > SEARCH_FILE_MAX or filereader.kind(full) != "text":
                            continue
                        scanned += 1
                        with open(full, encoding="utf-8", errors="replace") as fh:
                            for i, line in enumerate(fh, 1):
                                if q in line.lower():
                                    hits.append(f"- {full}:{i}: {line.strip()[:SEARCH_LINE]}")
                                    if len(hits) >= SEARCH_MAX:
                                        break
                    except OSError:
                        continue
                if len(hits) >= SEARCH_MAX:
                    break
            if len(hits) >= SEARCH_MAX:
                break
        if not hits:
            if content:
                return f"no line in {scanned} text files contains \"{query}\"", False
            return f"no file name contains \"{query}\"", False
        cap = f"\n(first {SEARCH_MAX}; narrow the query, path or glob)" if len(hits) >= SEARCH_MAX else ""
        return f"{len(hits)} hits:\n" + "\n".join(hits) + cap, False

    ops = {"read_file": do_read, "list_dir": do_list, "search": do_search}

    def handler(name, tool_input):
        inp = tool_input if isinstance(tool_input, dict) else {}
        fn = ops.get(name)
        try:
            if fn is None:
                result = (f"unknown tool: {name}", True)
            else:
                result = fn(inp)
        except Exception as e:  # noqa: BLE001 — a tool never crashes the turn
            result = (f"error: {type(e).__name__}: {e}", True)
        if on_tool is not None:
            try:
                on_tool(name, inp, result[0])
            except Exception:  # noqa: BLE001
                pass
        return result

    return handler
