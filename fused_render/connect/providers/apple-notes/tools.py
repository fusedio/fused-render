"""Apple Notes' tools, native: Notes.app on this Mac through `osascript`
(JavaScript for Automation), the `local-app` kind (SPEC-connect-oauth §2, §11).

No token and no network: `ctx.token` is `""`, and every call is one JXA
program handed to `connect/applescript.run_jxa` with the request as argv (user
text is never interpolated into the script). The program is the one the Apple
Notes starter (`bots/starters/apple-notes/notes.py`) runs; it is copied here
rather than imported by path because a connect pack must load on its own
(the registry picks it up by folder) and the starter folder is not a package.
Keep the two in step: a fix to one is a fix to the other.

Six tools, the starter's six: folders, search, read, recent (reads), create,
append (writes, never destructive: nothing deletes or overwrites a note).
Results are the app's own ids (`x-coredata://.../ICNote/p123`), which Notes
keeps stable, so a read can follow a search without a title lookup.

A withdrawn Automation grant (-1743) is `ToolAuthError`: the core marks the
card Reconnect and the message says where in System Settings to allow it.
"""
from __future__ import annotations

import json

from fused_render.connect import applescript
from fused_render.connect.tools import ToolAuthError, ToolError

APP = "Notes"

# One JXA program: `notes(p)` takes the request object and returns the result object.
JXA = r"""
function notes(p) {
  const app = Application("Notes");
  app.includeStandardAdditions = false;
  const iso = d => { try { return d ? new Date(d).toISOString() : ""; } catch (e) { return ""; } };
  const snip = (t, n) => { t = (t || "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n) + "…" : t; };
  const folderOf = n => { try { return n.container().name(); } catch (e) { return ""; } };
  const row = (n, full) => { const r = { id: n.id(), title: n.name(), folder: folderOf(n), modified: iso(n.modificationDate()), created: iso(n.creationDate()) };
    if (full) { r.body = n.plaintext(); r.shared = !!n.shared(); r.password_protected = !!n.passwordProtected(); }
    else r.snippet = snip(n.plaintext(), p.snippet || 160);
    return r; };
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const divs = t => String(t).split(/\r?\n/).map(l => "<div>" + (l ? esc(l) : "<br>") + "</div>").join("");
  const byTitleOrId = () => {
    if (p.id) { const m = app.notes.whose({ id: p.id })(); if (m.length) return m[0]; }
    if (p.title) { let m = app.notes.whose({ name: p.title })(); if (!m.length) m = app.notes.whose({ name: { _contains: p.title } })(); if (m.length) return m[0]; }
    return null; };
  // A write must hit exactly one note: id, else an exact title, else a title fragment only one note contains.
  const oneNote = () => {
    if (p.id) { const m = app.notes.whose({ id: p.id })(); return m.length ? { n: m[0] } : { error: "no note with id " + p.id }; }
    const live = l => l.filter(n => folderOf(n) !== "Recently Deleted");
    let m = live(app.notes.whose({ name: p.title })());
    if (!m.length) m = live(app.notes.whose({ name: { _contains: p.title } })());
    if (m.length === 1) return { n: m[0] };
    if (!m.length) return { error: "no note title contains " + p.title };
    return { error: m.length + " notes match title " + p.title + "; pass the id of one", candidates: m.slice(0, 5).map(n => row(n, false)) }; };
  const sortDesc = (a, b) => (b.modified > a.modified ? 1 : b.modified < a.modified ? -1 : 0);
  // A read scoped to a folder name covers every folder of that name (one per account); null = every note.
  const inFolders = (folders, pick) => folders ? folders.flatMap(f => pick(f.notes)) : pick(app.notes);
  const limit = Math.max(1, Math.min(p.limit || 20, 100));
  let out;
  switch (p.action) {
    case "folders": {
      out = { folders: app.folders().map(f => { let acct = ""; try { acct = f.container().name(); } catch (e) {}
        return { name: f.name(), account: acct, notes: f.notes.length, id: f.id() }; }) };
      break; }
    case "search": {
      const q = p.query || "";
      const src = p.folder ? app.folders.whose({ name: p.folder })() : null;
      if (p.folder && !src.length) { out = { error: "no folder named " + p.folder }; break; }
      const cond = p.in_body ? { _or: [{ name: { _contains: q } }, { plaintext: { _contains: q } }] } : { name: { _contains: q } };
      const hits = inFolders(src, s => s.whose(cond)());
      const rows = hits.map(n => row(n, false)).sort(sortDesc);
      out = { query: q, in_body: !!p.in_body, folder: p.folder || "", count: rows.length, notes: rows.slice(0, limit) };
      break; }
    case "read": {
      const n = byTitleOrId(); if (!n) { out = { error: "no note matches id " + (p.id || "") + " or title " + (p.title || "") }; break; }
      out = { note: row(n, true) }; break; }
    case "recent": {
      const days = Math.max(1, p.days || 7), since = new Date(Date.now() - days * 86400000);
      const src = p.folder ? app.folders.whose({ name: p.folder })() : null;
      if (p.folder && !src.length) { out = { error: "no folder named " + p.folder }; break; }
      const hits = inFolders(src, s => s.whose({ modificationDate: { _greaterThan: since } })());
      const rows = hits.map(n => row(n, false)).sort(sortDesc);
      out = { days, folder: p.folder || "", count: rows.length, notes: rows.slice(0, limit) }; break; }
    case "create": {
      const html = "<div><h1>" + esc(p.title || String(p.body || "").split(/\r?\n/)[0] || "Note") + "</h1></div>" + (p.body ? divs(p.body) : "");
      let folder = null;
      if (p.folder) { const f = app.folders.whose({ name: p.folder })(); if (!f.length) { out = { error: "no folder named " + p.folder }; break; }
        // Two accounts can each have a "Work": a write must not guess which.
        if (f.length > 1) { out = { error: f.length + " folders are named " + p.folder + " (in different accounts); give one a unique name in Notes" }; break; }
        folder = f[0]; }
      else folder = app.defaultAccount().defaultFolder();
      const n = app.Note({ body: html }); folder.notes.push(n);
      out = { ok: true, note: row(n, false) }; break; }
    case "append": {
      const hit = oneNote(); if (hit.error) { out = hit; break; }
      const n = hit.n;
      if (n.passwordProtected()) { out = { error: "note " + n.name() + " is locked; unlock it in Notes first" }; break; }
      n.body = n.body() + divs(p.text);  // Notes keeps attachments when the body is re-set
      out = { ok: true, appended: String(p.text), note: row(n, false) }; break; }
    default: out = { error: "unknown action " + p.action };
  }
  return out;
}
function run(argv) { return JSON.stringify(notes(JSON.parse(argv[0] || "{}"))); }
"""


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_FOLDER = {"type": "string", "description": "folder name; every folder when empty", "default": ""}
_LIMIT = {"type": "integer", "description": "max notes (1-100)", "default": 20}

TOOLS = [
    {"name": "notes_folders",
     "description": "List the folders in Notes on this Mac, with their account and note count.",
     "input_schema": _obj({}, []),
     "read_only": True, "destructive": False},
    {"name": "notes_search",
     "description": "Find notes whose title (or, with in_body, text) contains the query; newest first.",
     "input_schema": _obj({"query": {"type": "string"}, "folder": _FOLDER,
                           "in_body": {"type": "boolean", "description": "also match the note's text", "default": False},
                           "limit": _LIMIT}, ["query"]),
     "read_only": True, "destructive": False},
    {"name": "notes_read",
     "description": "Read one note in full, by id (from a search) or by title.",
     "input_schema": _obj({"id": {"type": "string", "default": ""}, "title": {"type": "string", "default": ""}}, []),
     "read_only": True, "destructive": False},
    {"name": "notes_recent",
     "description": "Notes edited in the last N days, newest first.",
     "input_schema": _obj({"days": {"type": "integer", "default": 7}, "folder": _FOLDER, "limit": _LIMIT}, []),
     "read_only": True, "destructive": False},
    {"name": "notes_create",
     "description": "Create a note (title as its heading, body one paragraph per line) in a folder, "
                    "or the default account's Notes folder.",
     "input_schema": _obj({"title": {"type": "string", "default": ""}, "body": {"type": "string", "default": ""},
                           "folder": _FOLDER}, []),
     "read_only": False, "destructive": False},
    {"name": "notes_append",
     "description": "Add lines to the end of one existing note, by id or by a title only one note has.",
     "input_schema": _obj({"text": {"type": "string"}, "id": {"type": "string", "default": ""},
                           "title": {"type": "string", "default": ""}}, ["text"]),
     "read_only": False, "destructive": False},
]
_ACTIONS = {t["name"]: t["name"][len("notes_"):] for t in TOOLS}
_PARAMS = {t["name"]: set(t["input_schema"]["properties"]) for t in TOOLS}


def _run(req: dict) -> dict:
    try:
        out = applescript.run_jxa(JXA, [json.dumps(req)], app=APP)
    except applescript.AutomationDenied as e:
        raise ToolAuthError(str(e)) from None
    except applescript.AutomationError as e:
        raise ToolError(str(e)) from None
    if out.get("error"):
        # An error with company (append on an ambiguous title: `candidates`) is still a
        # failed call: `ok: false`, with the candidates kept in the text for the caller.
        raise ToolError(json.dumps(out, ensure_ascii=False, default=str))
    return out


def call(ctx, name: str, args: dict):
    action = _ACTIONS.get(name)
    if action is None:
        raise ToolError(f"unknown tool {name}")
    a = {k: v for k, v in (args or {}).items() if k in _PARAMS[name]}
    if action == "search" and not str(a.get("query") or "").strip():
        raise ToolError("missing argument: query")
    if action in ("read", "append") and not (a.get("id") or a.get("title")):
        raise ToolError("pass an id or a title")
    if action == "create" and not (str(a.get("title") or "").strip() or str(a.get("body") or "").strip()):
        raise ToolError("pass a title or a body")
    if action == "append" and not str(a.get("text") or "").strip():
        raise ToolError("missing argument: text")
    req = {"action": action, "query": str(a.get("query") or ""), "folder": str(a.get("folder") or ""),
           "id": str(a.get("id") or ""), "title": str(a.get("title") or ""), "body": str(a.get("body") or ""),
           "text": str(a.get("text") or ""), "in_body": bool(a.get("in_body"))}
    for k, default, hi in (("days", 7, 3650), ("limit", 20, 100)):
        try:
            req[k] = max(1, min(hi, int(a.get(k) or default)))
        except (TypeError, ValueError):
            req[k] = default
    return _run(req)
