"""App MCP tools for bots (port of OpenBot apptools.py; docs/bots.md §1).

Every fused-render app on this Mac that ships an `mcp.toml` (curated in
fused-render's MCP panel, served to MCP hosts by `fused-render app serve <dir>`)
is visible to every bot through the `tool` action. Every app under ROOTS,
with or without a manifest, is listed in each bot's APPS prompt section
(list_apps / apps_section at the bottom). This module owns:

- discovery: which apps have manifests (apps root + fused-render's registered
  and linked app lists), cached and refreshed on manifest mtime;
- execution: the vendored runner `fused_render.compute.app_mcp._run_app_tool`
  (the exact code path `fused-render app serve` uses), so a bot and an MCP host
  run an app's tool identically;
- the write heuristic that decides which tools go through the approval gate;
- the APP TOOLS prompt section;
- app skills: an app's SKILL.md and its `.py` files run through `/api/run`.

`SKIP_DIR` is None, so `discover()` and `list_apps()` skip nothing unless
told to, and the apps root comes from `fused_render.bots.paths`.
`FUSED_HOME` is fused-render's `shell.storage.home_dir()` at import, and
`REGISTRY_FILES` its `registered_apps.json` (registered_apps.py); FusedBot's
second file, `linked_apps.json`, has no fused-render counterpart.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from fused_render.bots import paths as _bpaths

from fused_render.compute.app_mcp import (  # noqa: F401
    AppManifestError,
    _run_app_tool,
    build_app_tool_spec,
    load_app_manifest,
)


def _fused_home():
    from fused_render.shell import storage

    return storage.home_dir()


FUSED_HOME = _fused_home()


def apps_root():
    """`<Fused workspace>/app`, where builds land and every bot's APPS live
    (`FUSED_RENDER_DIR` or `~/Fused`, see bots/paths.py)."""
    return _bpaths.apps_root()


ROOTS = [apps_root()]  # bot.py's builds root is ROOTS[0]
REGISTRY_FILES = [os.path.join(FUSED_HOME, "registered_apps.json")]
SKIP_DIR = None  # OpenBot skipped its own folder; this app has none under ROOTS
TTL_S = 10
def available():
    """App tools run on the vendored runner, so they are always available."""
    return True


@dataclass
class ToolRec:
    app: str
    app_dir: str
    name: str
    description: str
    params: list = field(default_factory=list)
    spec: object = None
    tools_in_app: int = 0


def count_tools(app_dir):
    """Number of `[[tool]]` tables in <app_dir>/mcp.toml; 0 when absent or unreadable."""
    p = os.path.join(app_dir, "mcp.toml")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip() == "[[tool]]")
    except OSError:
        return 0


def _registry_paths(files):
    out = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        entries = data.get("entries") if isinstance(data, dict) else None
        for e in entries if isinstance(entries, list) else []:
            p = e.get("path") if isinstance(e, dict) else None
            if isinstance(p, str) and p:
                out.append(p)
    return out


def _candidate_dirs(roots, registry_files):
    dirs = []
    for root in roots:
        try:
            for name in sorted(os.listdir(root)):
                if not name.startswith("."):
                    dirs.append(os.path.join(root, name))
        except OSError:
            pass
    dirs.extend(_registry_paths(registry_files))
    return dirs


_bad_logged = {}  # manifest path -> mtime already reported
_targets = {}     # real app dir -> target files of its tools (edits/deletions must refresh the cache)


def _load_dir(d):
    """ToolRecs for one app dir, or [] (no manifest, or invalid: logged once per mtime)."""
    mp = os.path.join(d, "mcp.toml")
    if not os.path.isfile(mp):
        return []
    try:
        manifest = load_app_manifest(Path(d))
        recs = []
        _targets[os.path.realpath(d)] = sorted({str(t.target_path(manifest.app_dir)) for t in manifest.tools})
        for t in manifest.tools:
            spec = build_app_tool_spec(manifest, t)
            recs.append(ToolRec(app=os.path.basename(d.rstrip("/")), app_dir=d, name=t.name,
                                description=t.description or "", params=list(spec.params), spec=spec,
                                tools_in_app=len(manifest.tools)))
        return recs
    except Exception as e:  # AppManifestError, OSError, SyntaxError in a target file
        try:
            now = os.path.getmtime(mp)
        except OSError:
            now = 0
        if _bad_logged.get(mp) != now:
            _bad_logged[mp] = now
            print(f"[apptools] skipping {mp}: {e}", file=sys.stderr)
        return []


def discover(roots, registry_files, skip_dir=None):
    """All tool records from every app with a valid manifest, deduplicated by real path."""
    if not available():
        return []
    seen, out = set(), []
    skip = os.path.realpath(skip_dir) if skip_dir else None
    for d in _candidate_dirs(roots, registry_files):
        try:
            rp = os.path.realpath(d)
        except OSError:
            continue
        if rp in seen or rp == skip or not os.path.isdir(rp):
            continue
        seen.add(rp)
        out.extend(_load_dir(d))
    return out


# ---- cache: rebuilt at most every TTL_S, and only re-parsed when a manifest changed ----
_lock = threading.Lock()
_cache = []
_cache_at = 0.0
_cache_sig = None


def _signature(roots, registry_files, skip_dir):
    sig = []
    for d in _candidate_dirs(roots, registry_files):
        mp = os.path.join(d, "mcp.toml")
        rp = os.path.realpath(d)
        try:
            sig.append((rp, mp, os.path.getmtime(mp)))
        except OSError:
            continue
        # The served code is a snapshot of the entrypoint file: a changed or vanished
        # target must rebuild the app's specs, not keep running stale source.
        for tp in _targets.get(rp, ()):
            try:
                sig.append((rp, tp, os.path.getmtime(tp)))
            except OSError:
                sig.append((rp, tp, -1.0))
    return tuple(sorted(set(sig)))


def registry(force=False):
    global _cache, _cache_at, _cache_sig
    if not available():
        return []
    with _lock:
        now = time.time()
        if not force and now - _cache_at < TTL_S:
            return _cache
        sig = _signature(ROOTS, REGISTRY_FILES, SKIP_DIR)
        if force or sig != _cache_sig:
            _cache = discover(ROOTS, REGISTRY_FILES, SKIP_DIR)
            _cache_sig = sig
        _cache_at = now
        return _cache


def tool_ref(d):
    """(app, name, args) from the model's decision, with the same fallbacks everywhere.
    tools.py resolves a `tool` action through this in risk, describe and execute
    alike, or a name placed in `value` could reach execute without passing the gate."""
    app = str(d.get("app") or "").strip()
    name = str(d.get("name") or d.get("value") or "").strip()
    args = d.get("args")
    return app, name, args if isinstance(args, dict) else ({} if args is None else args)


def find(recs, app, name):
    """The record for app folder + tool name (app matched case-insensitively, `-`/`_`/space alike), or None."""
    def norm(s):
        return re.sub(r"[-_\s]+", "-", (s or "").strip().lower())
    a, n = norm(app), (name or "").strip()
    for r in recs:
        if r.name == n and (not a or norm(r.app) == a):
            return r
    return None


# ---- execution ------------------------------------------------------------------
import asyncio  # noqa: E402
import concurrent.futures  # noqa: E402

RESULT_CAP = 4000
TIMEOUT_S = 180  # the first call to a tool with requirements builds a venv; the backend itself stops at 300 s


@dataclass
class RunResult:
    ok: bool
    text: str
    dropped: list = field(default_factory=list)


def _call(rec, kwargs):
    # The runner is async: asyncio.run in a worker thread (the bot loop is a
    # thread; no running loop there).
    return asyncio.run(_run_app_tool(rec.spec, kwargs))


def run_tool(rec, args, timeout_s=TIMEOUT_S):
    """Run one tool with the model's args. Never raises: failures come back as
    `error: ...` text so the bot can retry or fall back to browsing."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return RunResult(False, f"error: `args` must be a JSON object of parameters, got {type(args).__name__}")
    allowed = {p.name for p in rec.params}
    kwargs = {k: v for k, v in args.items() if k in allowed}
    dropped = [k for k in args if k not in allowed]
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(_call, rec, kwargs)
        value = fut.result(timeout=timeout_s)
    except concurrent.futures.TimeoutError:
        return RunResult(False, (f"error: {rec.app} › {rec.name} timed out after {int(timeout_s)} s. The call may still complete "
                                 "in the background: do not retry a tool that changes something; check its effect first "
                                 "(a read tool, or tell the user)."), dropped)
    except Exception as e:
        return RunResult(False, f"error: {e}", dropped)
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception as e:
        return RunResult(False, f"error: result not serialisable: {e}", dropped)
    return RunResult(True, text, dropped)


# ---- approval: which tools change something ----------------------------------------
_WRITE_VERBS = ("send|reply|create|delete|remove|update|write|set|save|post|publish|label|move|archive|mark"
                "|rename|add|append|insert|edit|modify|change|replace|resolve|reopen|run|execute|submit|upload|apply|clear|trash"
                "|share|invite|forward|cancel|comment|draft|schedule|book|pay|order|purchase|buy|unsubscribe|subscribe|grant|revoke"
                "|notify|import|sync|start|stop|enable|disable|assign|approve|reject|merge|push|deploy|reset|revert|restore|kill")
# Names are canonical verbs: any inflection counts (send, sends, sending) and camelCase is split first.
WRITE_NAME_RX = re.compile(rf"\b(?:{_WRITE_VERBS})(?:s|es|d|ed|ing)?\b", re.I)
# Descriptions are prose: only the bare and third-person forms ("Sends a message"), so "whether a key is saved"
# does not drag a status check through the gate.
# Words that are usually nouns in prose ("threads in a label", "a comment", "a post", "a set of") are
# left to the name check only.
_DESC_NOUNS = {"label", "comment", "order", "book", "draft", "post", "set", "mark", "share", "run", "start", "stop", "push"}
_DESC_VERBS = "|".join(v for v in _WRITE_VERBS.split("|") if v not in _DESC_NOUNS)
WRITE_DESC_RX = re.compile(rf"\b(?:{_DESC_VERBS})(?:s|es)?\b", re.I)
WRITE_RX = WRITE_NAME_RX  # back-compat alias


def _words(name):
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "").replace("_", " ").replace("-", " ")


def is_write(rec):
    """Heuristic: a tool whose name or description reads as a write goes through the gate."""
    return bool(WRITE_NAME_RX.search(_words(rec.name)) or WRITE_DESC_RX.search(rec.description or ""))


# ---- trusted apps: a per-bot whitelist that skips the gate -----------------------
# bot.json `trusted_apps`: app folder names (normalised as `find` matches them) whose
# `tool` and `py` calls never raise an approval card for that bot, whatever the write
# heuristic, the app's SKILL.md or the bot's own `risky: true` say (owner, 2026-10-22:
# one list, ticked = without asking, nothing else). It is the ONE app-level permission
# (Settings › Permissions › "Apps it may use without asking"): a build this bot
# finished and an "Always approve" on a tool/py card both land here, so the checkbox
# never shows off while the app runs silently. Browser actions, texts and builds are
# not apps and keep their gates.
def norm_app(s):
    """One spelling for an app folder: lower, `-`/`_`/space collapsed to `-`."""
    return re.sub(r"[-_\s]+", "-", (s or "").strip().lower())


def clean_trusted_apps(v):
    """The stored form of a trusted_apps setting: normalised, deduped, strings only, order kept."""
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        n = norm_app(x) if isinstance(x, str) else ""
        if n and n not in out:
            out.append(n)
    return out


def trusted_set(meta):
    return set(clean_trusted_apps((meta or {}).get("trusted_apps")))


def is_trusted(meta, app):
    """Does this bot's meta whitelist the app (folder name, any spelling)?"""
    return bool(app) and norm_app(app) in trusted_set(meta)


def trust_app(meta, app) -> bool:
    """Tick `app` for this bot (caller holds bot.lock and saves). True when meta changed."""
    key = norm_app(app)
    if not key:
        return False
    cur = clean_trusted_apps(meta.get("trusted_apps"))
    if key in cur:
        return False
    meta["trusted_apps"] = [*cur, key]
    return True


def set_trusted_apps(meta, add=(), remove=()):
    """The dialog's edit: additions and removals against the live list (trusted_apps has other writers, a build
    finishing and a card answered "Always approve", so a full list from the dialog would clobber them)."""
    cur = clean_trusted_apps(meta.get("trusted_apps"))
    drop = set(clean_trusted_apps(list(remove)))
    cur = [a for a in cur if a not in drop]
    for a in clean_trusted_apps(list(add)):
        if a not in cur:
            cur.append(a)
    meta["trusted_apps"] = cur


# ---- prompt -------------------------------------------------------------------------
SECTION_CAP = 6000
DESC_CAP = 160
PER_APP_MAX = 12
_TYPES = {str: "str", int: "int", float: "float", bool: "bool", list: "list", dict: "dict"}


def _sig(rec):
    parts = []
    for p in rec.params:
        t = _TYPES.get(p.annotation, "")
        s = f"{p.name}: {t}" if t else p.name
        if p.has_default:
            s += "=" + json.dumps(p.default, default=str)
        parts.append(s)
    return ", ".join(parts)


def _line(rec, trusted=False):
    d = (rec.description or "").strip().replace("\n", " ")
    if len(d) > DESC_CAP:
        d = d[:DESC_CAP].rstrip() + "…"
    return f"- {rec.name}({_sig(rec)}): {d}" + (" [approval]" if is_write(rec) and not trusted else "")


def _render(recs, limit, trusted=frozenset()):
    by_app = {}
    for r in recs:
        by_app.setdefault(r.app, []).append(r)
    out = []
    for app, rs in by_app.items():
        head = app
        tr = norm_app(app) in trusted
        if tr:
            head += " (trusted: its tools never ask)"
        shown = rs if limit is None or len(rs) <= limit else rs[:limit]
        if len(shown) < len(rs):
            head += f" ({len(rs)} tools; first {limit} shown, all callable)"
        out.append(head + "\n" + "\n".join(_line(r, tr) for r in shown))
    return "\n".join(out)


def prompt_section(recs, trusted=None):
    """The APP TOOLS prompt section; `trusted` (normalised folder names, see
    trusted_set) drops the [approval] tag from those apps' tools."""
    if not recs:
        return ""
    trusted = frozenset(trusted or ())
    hdr = ("\n\nAPP TOOLS (local apps on this Mac; call one with `tool` app name args. Prefer a tool over browsing when it "
           "does the job directly. Tools marked [approval] change something and pause for the user's yes; the rest run at once):\n")
    body = _render(recs, None, trusted)
    if len(body) > SECTION_CAP:
        body = _render(recs, PER_APP_MAX, trusted)
    return hdr + body


# ---- every app, with or without tools -------------------------------------------
# Bots see ALL fused apps under ROOTS, not only those that ship an mcp.toml: the APPS
# prompt section lists each one (folder, name, README line, tool count, link) so a bot
# can `show` it as a card or `goto` its link. Needs no bundled fused module.
APPS_CAP = 3000
_apps_cache = []
_apps_cache_at = 0.0


def readme_summary(d):
    """(title, first paragraph line) from <d>/README.md; ('', '') when absent."""
    p = os.path.join(d, "README.md")
    if not os.path.isfile(p):
        return "", ""
    title, desc = "", ""
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip()
                if not s:
                    continue
                if s.startswith("#"):
                    if not title:
                        title = s.lstrip("#").strip()
                    continue
                if not desc:
                    desc = re.sub(r"[*_`]", "", s)
                    break
    except OSError:
        pass
    return title, desc


def list_apps(roots=None, skip_dir=None):
    """Every fused-render app folder under `roots` (an index.html whose first 4 KiB carry
    the fused-app marker), deduplicated by real path and sorted by folder:
    [{"folder", "dir", "name", "desc", "tools", "skill"}]; `skill` is the SKILL.md
    description ("-" when it has none, "" when the app has no SKILL.md), so the
    APPS line can mark which apps take `py`. `skip_dir` None means SKIP_DIR
    (itself None here: skip nothing)."""
    roots = ROOTS if roots is None else roots
    sd = SKIP_DIR if skip_dir is None else skip_dir
    skip = os.path.realpath(sd) if sd else None
    out, seen = [], set()
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.startswith("."):
                continue
            d = os.path.join(root, name)
            rp = os.path.realpath(d)
            if rp in seen or rp == skip:
                continue
            try:
                with open(os.path.join(d, "index.html"), "rb") as f:
                    head = f.read(4096).decode("utf-8", "replace")
            except OSError:
                continue
            if 'name="fused-app"' not in head:
                continue
            seen.add(rp)
            m = re.search(r"<title>(.*?)</title>", head, re.S | re.I)
            title, desc = readme_summary(d)
            sk = read_skill(d)
            out.append({"folder": name, "dir": d, "name": title or (m.group(1).strip() if m else "") or name,
                        "desc": desc, "tools": count_tools(d), "skill": (sk["description"] or "-") if sk else ""})
    return out


def apps(force=False):
    """list_apps() over ROOTS, rebuilt at most every TTL_S."""
    global _apps_cache, _apps_cache_at
    with _lock:
        now = time.time()
        if force or now - _apps_cache_at >= TTL_S:
            _apps_cache = list_apps()
            _apps_cache_at = now
        return _apps_cache


def apps_section(items, link=None):
    """The APPS prompt section: one line per app; `link(dir)` (optional) appends its open link."""
    if not items:
        return ""
    hdr = (f"\n\nAPPS (every fused app under {', '.join(ROOTS)}; all of them are available to you. `show folder` puts one "
           "in the chat as a card the user opens; `goto` its link opens it in the browser; the ones with tools are also "
           "listed under APP TOOLS and called with `tool`; [py] ones ship a SKILL.md: `py` app with no file loads it, then "
           "`py` app file args runs one of its files):\n")
    lines = []
    for a in items:
        s = f"- {a['folder']}: {a['name']}"
        desc = a["skill"] if a.get("skill") and a["skill"] != "-" else a.get("desc")
        if desc:
            s += f" — {desc[:DESC_CAP]}"
        if a.get("skill"):
            s += " [py]"
        if a.get("tools"):
            s += f" [{a['tools']} tool{'s' if a['tools'] != 1 else ''}]"
        if link:
            s += f"  {link(a['dir'])}"
        lines.append(s)
    body = "\n".join(lines)
    if len(body) > APPS_CAP:
        body = body[:APPS_CAP].rsplit("\n", 1)[0] + f"\n… ({len(items)} apps in all; folders not shown are still available by name)"
    return hdr + body


# ---- app skills: an app's Python, called directly (fused-render SPEC §49) -------
# What a bot knows about an app's `.py` files is the app's own SKILL.md (frontmatter
# name / description / approve, then one `## <file>.py` section per callable file),
# read off disk here: nothing parses the code. A file runs through the page's OWN
# `POST /api/run` (same runner, same envelope, same 60 s bound as
# `fused.runPython`), over HTTP to the server; `origin` is passed in so this
# module never imports bot.py.
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

PY_TIMEOUT_S = 75  # the server kills the run at 60 s; the extra covers the round trip
SKILL_FILE = "SKILL.md"
SKILL_CAP = 4000  # one app's body in the prompt; the rest is cut with a note
_skills = {}  # path -> (mtime, parsed)
_FILE_HEAD = re.compile(r"^##\s+`?([\w.\-]+\.py)`?(?:\s.*)?$")  # anything after the name is decoration


def _frontmatter(text):
    """(fields, body) for a `---`-fenced header of plain `key: value` lines. Lists
    may be `[a, b]` or `- a` lines under the key. No YAML dependency: the three
    keys a SKILL.md carries do not need one."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    head, body = text[3:end], text[end + 4:].lstrip("\n")
    out, key = {}, None
    for raw in head.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip():
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if val.startswith("[") and val.endswith("]"):
                out[key] = [v.strip().strip("'\"") for v in val[1:-1].split(",") if v.strip()]
            else:
                out[key] = val.strip("'\"") if val else []
        elif key and line.lstrip().startswith("- "):
            if not isinstance(out.get(key), list):
                out[key] = []
            out[key].append(line.lstrip()[2:].strip().strip("'\""))
    return out, body


def read_skill(app_dir):
    """The app's SKILL.md, parsed: {"name", "description", "approve": set of files,
    "body", "files": {file: first line of its section}}; None when absent.
    Cached on mtime, so an update build's new SKILL.md is seen at the next step."""
    p = os.path.join(app_dir, SKILL_FILE)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    hit = _skills.get(p)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read(64 * 1024)
    except OSError:
        return None
    fm, body = _frontmatter(text)
    files, cur = {}, None
    for line in body.splitlines():
        m = _FILE_HEAD.match(line.strip())
        if m:
            cur = m.group(1)
            files[cur] = ""
        elif line.startswith("#"):
            cur = None
        elif cur and not files[cur] and line.strip():
            files[cur] = re.sub(r"[*_`]", "", line.strip())
    approve = fm.get("approve") or []
    out = {"name": str(fm.get("name") or ""), "description": str(fm.get("description") or ""),
           "approve": {a for a in (approve if isinstance(approve, list) else [approve]) if a},
           "body": body.strip(), "files": files}
    _skills[p] = (mt, out)
    return out


def skill_file(skill, file):
    """The documented filename for the model's `file` (exact, or without .py), or None."""
    f = (file or "").strip()
    if not f or not skill:
        return None
    for name in skill["files"]:
        if name == f or name == f + ".py":
            return name
    return None


def _api(origin, method, path, body=None, timeout=20):
    url = origin.rstrip("/") + path
    data = json.dumps(body or {}).encode() if method != "GET" else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Fused": "1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            said = json.loads(e.read() or b"{}")
            said = said.get("error") or said.get("detail") or f"HTTP {e.code}"
        except Exception:
            said = f"HTTP {e.code}"
        raise RuntimeError(str(said))
    except urllib.error.URLError as e:
        raise RuntimeError(f"fused-render is not reachable: {e.reason}")


def resolve_app(app):
    """The app folder for the model's `app` (a folder name under ROOTS, an app's
    README name, or an absolute path under ROOTS), or None."""
    a = (app or "").strip()
    if not a:
        return None
    if os.path.isabs(a):
        rp = os.path.realpath(a)
        return rp if os.path.isdir(rp) and any(rp.startswith(os.path.realpath(r) + os.sep) for r in ROOTS) else None

    def norm(x):
        return re.sub(r"[-_\s]+", "-", (x or "").strip().lower())
    for it in apps():
        if norm(it["folder"]) == norm(a) or norm(it["name"]) == norm(a):
            return it["dir"]
    return None


def is_owned(app_dir, builds):
    """True when `app_dir` is one of this bot's own builds (`meta["builds"]`): the
    bot wrote the code, the build passed the gate, so a `py` call runs at once."""
    rp = os.path.realpath(app_dir)
    for bd in builds or []:
        d = bd.get("dir") if isinstance(bd, dict) else None
        if d and os.path.realpath(d) == rp:
            return True
    return False


def run_py(origin, app_dir, file, args, html=None, timeout_s=PY_TIMEOUT_S):
    """Run one documented file with the model's args through POST /api/run. Never
    raises: failures come back as `error: ...` text so the bot can retry or fall
    back to browsing. Args go through as given: SKILL.md is prose, so the
    runner's own binding is the check (it drops keys main() does not take and
    fails a missing required one with ParamError, which gets a pointer to the skill)."""
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return RunResult(False, f"error: `args` must be a JSON object of parameters, got {type(args).__name__}")
    body = {"py": os.path.join(app_dir, file), "html": html or os.path.join(app_dir, "index.html"), "params": args}
    try:
        res = _api(origin, "POST", "/api/run", body, timeout=timeout_s)
    except Exception as e:
        return RunResult(False, f"error: {e}")
    if not res.get("ok"):
        err = res.get("error") or {}
        if isinstance(err, dict):
            msg = f"{err.get('type') or 'Error'}: {err.get('message') or ''}".strip()
            tb = (err.get("traceback") or "").strip().splitlines()
            if tb:
                msg += "\n" + "\n".join(tb[-6:])
            if err.get("type") in ("ParamError", "TypeError"):
                msg += f"\n(the args do not match {file}'s main(); re-read its section under APP SKILLS and call again)"
        else:
            msg = str(err)
        if res.get("stdout"):
            msg += "\nstdout:\n" + str(res["stdout"])[-800:]
        return RunResult(False, "error: " + msg)
    try:
        text = json.dumps(res.get("result"), ensure_ascii=False, default=str)
    except Exception as e:
        return RunResult(False, f"error: result not serialisable: {e}")
    return RunResult(True, text)


def skill_section(app_dirs):
    """The APP SKILLS prompt section: the full SKILL.md of each app in `app_dirs`
    (already chosen by the caller: loaded with `py app`, built this task, or named
    by the task), each capped at SKILL_CAP and the whole at SECTION_CAP."""
    blocks, seen = [], set()
    for d in app_dirs:
        rp = os.path.realpath(d)
        if rp in seen:
            continue
        seen.add(rp)
        sk = read_skill(d)
        if not sk:
            continue
        body = sk["body"]
        if len(body) > SKILL_CAP:
            body = body[:SKILL_CAP].rsplit("\n", 1)[0] + "\n… (cut; the sections above are complete)"
        blocks.append(f"=== {os.path.basename(d)} ===\n{body}")
    if not blocks:
        return ""
    hdr = ("\n\nAPP SKILLS (how to call these apps' Python with `py` app file args: each `## file.py` section says what the file "
           "does, its args and an example. Call only files that have a section here; `py` app with no file loads another app's):\n")
    body = "\n\n".join(blocks)
    if len(body) > SECTION_CAP:
        body = body[:SECTION_CAP].rsplit("\n", 1)[0] + "\n… (more; `py` app with no file reloads one)"
    return hdr + body
