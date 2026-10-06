"""Apple Notes on this Mac, through Notes.app's own scripting (JavaScript for
Automation). No iCloud web, no network, no Shortcuts: every account and folder the
app shows is reachable.

Three ways in, tried in order:
1. `osascript` from this process (works when the host app may control Notes);
2. NotesBridge.app, a tiny applet built here with osacompile and started through
   `open`, so macOS asks the user once "NotesBridge wants to control Notes" (the
   host, FusedRender, cannot get that prompt: its Info.plist lacks
   NSAppleEventsUsageDescription);
3. reads only: the Notes database, read-only.

main(action=..., ...) returns a JSON dict and never raises; failures come back as
{"error": "...", "action": "..."}.
"""
import json
import os
import plistlib
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zlib

LIMIT_MAX = 100
SNIPPET = 160
TIMEOUT_S = 55  # the page's runner stops at 60 s

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
  const limit = Math.max(1, Math.min(p.limit || 20, 100));
  let out;
  switch (p.action) {
    case "status": {
      out = { ok: true, app: app.name(), version: app.version(), accounts: app.accounts().map(a => a.name()), notes: app.notes.length, folders: app.folders.length };
      break; }
    case "folders": {
      out = { folders: app.folders().map(f => { let acct = ""; try { acct = f.container().name(); } catch (e) {}
        return { name: f.name(), account: acct, notes: f.notes.length, id: f.id() }; }) };
      break; }
    case "search": {
      const q = p.query || "";
      let src = p.folder ? app.folders.whose({ name: p.folder })() : null;
      if (p.folder && !src.length) { out = { error: "no folder named " + p.folder }; break; }
      const scope = src ? src[0].notes : app.notes;
      const cond = p.in_body ? { _or: [{ name: { _contains: q } }, { plaintext: { _contains: q } }] } : { name: { _contains: q } };
      const hits = scope.whose(cond)();
      const rows = hits.map(n => row(n, false)).sort(sortDesc);
      out = { query: q, in_body: !!p.in_body, folder: p.folder || "", count: rows.length, notes: rows.slice(0, limit) };
      break; }
    case "read": {
      const n = byTitleOrId(); if (!n) { out = { error: "no note matches id " + (p.id || "") + " or title " + (p.title || "") }; break; }
      out = { note: row(n, true) }; break; }
    case "recent": {
      const days = Math.max(1, p.days || 7), since = new Date(Date.now() - days * 86400000);
      let scope = app.notes;
      if (p.folder) { const f = app.folders.whose({ name: p.folder })(); if (!f.length) { out = { error: "no folder named " + p.folder }; break; } scope = f[0].notes; }
      const hits = scope.whose({ modificationDate: { _greaterThan: since } })();
      const rows = hits.map(n => row(n, false)).sort(sortDesc);
      out = { days, folder: p.folder || "", count: rows.length, notes: rows.slice(0, limit) }; break; }
    case "create": {
      const html = "<div><h1>" + esc(p.title || String(p.body || "").split(/\r?\n/)[0] || "Note") + "</h1></div>" + (p.body ? divs(p.body) : "");
      let folder = null;
      if (p.folder) { const f = app.folders.whose({ name: p.folder })(); if (!f.length) { out = { error: "no folder named " + p.folder }; break; } folder = f[0]; }
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
"""
DIRECT = JXA + 'function run(argv) { return JSON.stringify(notes(JSON.parse(argv[0] || "{}"))); }\n'

# ---- NotesBridge.app: Notes automation under its own name ---------------------
# The applet is generic: it evals the script file NB_SCRIPT, calls notes(<NB_REQ>)
# and writes JSON to NB_OUT. JXA changes therefore never rebuild it, and the user's
# "allow NotesBridge to control Notes" answer (keyed to its signature) sticks.
# It lives in .fused/data, not cache: deleting it costs a new permission prompt.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.join(APP_DIR, ".fused", "data", "NotesBridge.app")
BRIDGE_ID = "io.fused.apple-notes.bridge"
BRIDGE_VERSION = "1"  # bump only when BRIDGE_JS changes: a rebuilt applet is asked about again
BRIDGE_JS = r"""ObjC.import("Foundation");
function run() {
  const env = $.NSProcessInfo.processInfo.environment;
  const get = k => ObjC.unwrap(env.objectForKey(k)) || "";
  const read = p => ObjC.unwrap($.NSString.stringWithContentsOfFileEncodingError(p, $.NSUTF8StringEncoding, null)) || "";
  let res;
  try { const __req = JSON.parse(read(get("NB_REQ")) || "{}"); res = JSON.stringify(eval(read(get("NB_SCRIPT")) + "\nnotes(__req)")); }
  catch (e) { res = JSON.stringify({ error: String(e), errno: e.errorNumber || 0 }); }
  $(res).writeToFileAtomicallyEncodingError(get("NB_OUT"), true, $.NSUTF8StringEncoding, null);
}
"""
DENIED_HOWTO = ("macOS has not let NotesBridge control Notes. Open System Settings > Privacy & Security > Automation, "
                "turn on Notes under NotesBridge, and retry. (Reads still work from the Notes database.)")


def _bridge_ready():
    try:
        with open(os.path.join(BRIDGE, "Contents", "Info.plist"), "rb") as f:
            return plistlib.load(f).get("FusedBridgeVersion") == BRIDGE_VERSION
    except (OSError, ValueError):
        return False


def _build_bridge():
    parent = os.path.dirname(BRIDGE)
    os.makedirs(parent, exist_ok=True)
    work = tempfile.mkdtemp(prefix="build-", dir=parent)
    try:
        src, app = os.path.join(work, "bridge.js"), os.path.join(work, "NotesBridge.app")
        with open(src, "w") as f:
            f.write(BRIDGE_JS)
        subprocess.run(["osacompile", "-l", "JavaScript", "-o", app, src], check=True, capture_output=True, timeout=30)
        plist = os.path.join(app, "Contents", "Info.plist")
        with open(plist, "rb") as f:
            info = plistlib.load(f)
        info.update(CFBundleIdentifier=BRIDGE_ID, LSUIElement=True, FusedBridgeVersion=BRIDGE_VERSION,
                    NSAppleEventsUsageDescription="Apple Notes (a fused-render app) reads your notes, creates notes and appends lines to them.")
        with open(plist, "wb") as f:
            plistlib.dump(info, f)
        subprocess.run(["codesign", "--force", "-s", "-", app], check=True, capture_output=True, timeout=30)
        if os.path.exists(BRIDGE):
            shutil.rmtree(BRIDGE, ignore_errors=True)
        try:
            os.replace(app, BRIDGE)
        except OSError:
            pass  # another call built it first
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _bridge(req):
    """Run the JXA program inside NotesBridge.app. Returns the result dict; raises
    PermissionError when macOS refuses (or the prompt went unanswered)."""
    if not _bridge_ready():
        _build_bridge()
    cache = os.path.join(APP_DIR, ".fused", "cache")
    os.makedirs(cache, exist_ok=True)
    work = tempfile.mkdtemp(prefix="call-", dir=cache)
    try:
        paths = {k: os.path.join(work, k) for k in ("script.js", "req.json", "out.json")}
        with open(paths["script.js"], "w") as f:
            f.write(JXA)
        with open(paths["req.json"], "w") as f:
            json.dump(req, f)
        try:
            subprocess.run(["open", "-W", "-n", "-g", "-a", BRIDGE, "--env", "NB_SCRIPT=" + paths["script.js"],
                            "--env", "NB_REQ=" + paths["req.json"], "--env", "NB_OUT=" + paths["out.json"]],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise PermissionError(f"Notes did not answer within {TIMEOUT_S} s. If macOS is asking whether NotesBridge "
                                  "may control Notes, click OK and retry.")
        try:
            with open(paths["out.json"]) as f:
                out = json.load(f)
        except (OSError, ValueError):
            raise RuntimeError("NotesBridge exited without an answer")
        if out.get("errno") == -1743 or "not authorized" in str(out.get("error", "")).lower():
            raise PermissionError(DENIED_HOWTO)
        out.pop("errno", None)
        out["backend"] = "bridge"
        return out
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- fallback: read the Notes database directly ------------------------------
# When neither osascript nor NotesBridge may control Notes, reads come from
# ~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite (needs Full
# Disk Access, as the iMessage bridge does): folders, search, read, recent. Bodies
# are gzip + protobuf (Document.2 Note.3 .2 = the text). Nothing is written there;
# ids match Notes.app's (x-coredata://<store uuid>/ICNote/p<pk>).

STORE = os.path.expanduser("~/Library/Group Containers/group.com.apple.notes/NoteStore.sqlite")
CD_EPOCH = 978307200  # Core Data dates count from 2001-01-01
def _pb_fields(buf):
    """Yield (field, wire_type, value) for one protobuf message; value is bytes for
    length-delimited fields, int for varints; other wire types are skipped."""
    i, n = 0, len(buf)
    while i < n:
        tag, i = _varint(buf, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(buf, i)
            yield f, wt, v
        elif wt == 2:
            ln, i = _varint(buf, i)
            yield f, wt, buf[i:i + ln]
            i += ln
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        else:
            return


def _varint(buf, i):
    out, shift = 0, 0
    while i < len(buf):
        b = buf[i]
        i += 1
        out |= (b & 0x7f) << shift
        if not b & 0x80:
            break
        shift += 7
    return out, i


def _note_text(blob):
    """Plain text of a note from its ZICNOTEDATA blob, or "" (locked or unknown layout)."""
    if not blob:
        return ""
    try:
        raw = zlib.decompress(blob, 16 + zlib.MAX_WBITS)
    except zlib.error:
        return ""
    cur = raw
    for want in (2, 3, 2):  # Document.note (2) -> .3 -> text (2)
        nxt = None
        for f, wt, v in _pb_fields(cur):
            if f == want and wt == 2:
                nxt = v
                break
        if nxt is None:
            break
        cur = nxt
    else:
        try:
            return cur.decode("utf-8")
        except UnicodeDecodeError:
            pass
    # Layout changed: take the longest UTF-8 string two levels down.
    best = ""
    for _, wt, v in _pb_fields(raw):
        if wt != 2:
            continue
        for _, wt2, v2 in _pb_fields(v):
            if wt2 != 2:
                continue
            for _, wt3, v3 in _pb_fields(v2):
                if wt3 == 2:
                    try:
                        t = v3.decode("utf-8")
                    except UnicodeDecodeError:
                        continue
                    if len(t) > len(best) and t.isprintable() or "\n" in t:
                        best = t if len(t) > len(best) else best
    return best


def _iso(cd):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(cd + CD_EPOCH)) if cd else ""


def _snip(t, n):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t[:n] + "…" if len(t) > n else t


def _db():
    if not os.path.exists(STORE):
        raise FileNotFoundError("Notes database not found at " + STORE)
    try:
        return sqlite3.connect(f"file:{STORE}?mode=ro", uri=True, timeout=5)
    except sqlite3.OperationalError as e:
        raise PermissionError("cannot open the Notes database (Full Disk Access for FusedRender is needed): " + str(e))


_NOTE_SQL = """select n.Z_PK, n.ZIDENTIFIER, n.ZTITLE1, n.ZSNIPPET, n.ZMODIFICATIONDATE1, coalesce(n.ZCREATIONDATE1, n.ZCREATIONDATE3, n.ZCREATIONDATE), f.ZTITLE2, n.ZISPASSWORDPROTECTED, f.ZFOLDERTYPE
  from ZICCLOUDSYNCINGOBJECT n left join ZICCLOUDSYNCINGOBJECT f on f.Z_PK = n.ZFOLDER
  where n.ZTITLE1 is not null and ifnull(n.ZMARKEDFORDELETION, 0) = 0 and ifnull(f.ZMARKEDFORDELETION, 0) = 0
    and ifnull(f.ZTITLE2, '') <> 'Recently Deleted' """


def _rows(c, where="", params=(), body=False):
    out = []
    store = (c.execute("select Z_UUID from Z_METADATA").fetchone() or [""])[0]
    for pk, ident, title, snip, mod, cre, folder, locked, _ in c.execute(_NOTE_SQL + where + " order by n.ZMODIFICATIONDATE1 desc", params):
        r = {"id": f"x-coredata://{store}/ICNote/p{pk}" if store else (ident or str(pk)), "title": title or "", "folder": folder or "", "modified": _iso(mod), "created": _iso(cre)}
        if body:
            blob = c.execute("select ZDATA from ZICNOTEDATA where ZNOTE = ?", (pk,)).fetchone()
            r["body"] = "" if locked else _note_text(blob[0] if blob else b"")
            r["password_protected"] = bool(locked)
            r["shared"] = False
        else:
            r["snippet"] = _snip(snip or "", SNIPPET)
            r["_pk"] = pk
        out.append(r)
    return out


def _sqlite(req):
    a = req["action"]
    c = _db()
    try:
        if a == "status":
            notes = c.execute(_NOTE_SQL.replace("select n.Z_PK, n.ZIDENTIFIER, n.ZTITLE1, n.ZSNIPPET, n.ZMODIFICATIONDATE1, coalesce(n.ZCREATIONDATE1, n.ZCREATIONDATE3, n.ZCREATIONDATE), f.ZTITLE2, n.ZISPASSWORDPROTECTED, f.ZFOLDERTYPE", "select count(*)")).fetchone()[0]
            folders = c.execute("select count(*) from ZICCLOUDSYNCINGOBJECT where ZTITLE2 is not null and ifnull(ZMARKEDFORDELETION,0)=0").fetchone()[0]
            return {"ok": True, "app": "Notes (database, read-only)", "backend": "sqlite", "notes": notes, "folders": folders, "accounts": [], "writes": False, "note": DENIED_HOWTO}
        if a == "folders":
            rows = c.execute("""select f.Z_PK, f.ZTITLE2, (select count(*) from ZICCLOUDSYNCINGOBJECT n where n.ZFOLDER = f.Z_PK and n.ZTITLE1 is not null and ifnull(n.ZMARKEDFORDELETION,0)=0)
                                from ZICCLOUDSYNCINGOBJECT f where f.ZTITLE2 is not null and f.ZTITLE2 <> 'Recently Deleted' and ifnull(f.ZMARKEDFORDELETION,0)=0 order by f.ZTITLE2""").fetchall()
            return {"backend": "sqlite", "folders": [{"name": t, "account": "", "notes": n, "id": str(pk)} for pk, t, n in rows]}
        if a in ("search", "recent"):
            where, params = "", []
            if req["folder"]:
                where += " and f.ZTITLE2 = ?"
                params.append(req["folder"])
            if a == "recent":
                where += " and n.ZMODIFICATIONDATE1 > ?"
                params.append(time.time() - CD_EPOCH - req["days"] * 86400)
            else:
                where += " and (n.ZTITLE1 like ? or n.ZSNIPPET like ?)" if not req["in_body"] else ""
                if not req["in_body"]:
                    params += [f"%{req['query']}%", f"%{req['query']}%"]
            rows = _rows(c, where, tuple(params))
            if a == "search" and req["in_body"]:
                q = req["query"].lower()
                hits = []
                for r in rows[:2000]:
                    if q in r["title"].lower() or q in r["snippet"].lower():
                        hits.append(r)
                        continue
                    blob = c.execute("select ZDATA from ZICNOTEDATA where ZNOTE = ?", (r["_pk"],)).fetchone()
                    if blob and q in _note_text(blob[0]).lower():
                        hits.append(r)
                rows = hits
            for r in rows:
                r.pop("_pk", None)
            out = {"backend": "sqlite", "folder": req["folder"], "count": len(rows), "notes": rows[:req["limit"]]}
            if a == "search":
                out.update(query=req["query"], in_body=req["in_body"])
            else:
                out["days"] = req["days"]
            return out
        if a == "read":
            rid = req["id"]
            m = re.search(r"/p(\d+)$", rid or "")
            if rid:
                rows = _rows(c, " and (n.ZIDENTIFIER = ? or n.Z_PK = ?)", (rid, int(m.group(1)) if m else (int(rid) if rid.isdigit() else -1)), body=True)
            else:
                rows = _rows(c, " and n.ZTITLE1 = ?", (req["title"],), body=True) or _rows(c, " and n.ZTITLE1 like ?", (f"%{req['title']}%",), body=True)
            if not rows:
                return {"error": f"no note matches id {rid!r} or title {req['title']!r}"}
            return {"backend": "sqlite", "note": rows[0]}
        if a in WRITES:
            return {"error": DENIED_HOWTO}
        return {"error": "unknown action " + a}
    finally:
        c.close()


WRITES = ("create", "append")


def _direct(req):
    """osascript from this process. Returns the result dict, or None when macOS
    refuses this host Apple Events (-1743), so the caller tries NotesBridge."""
    try:
        r = subprocess.run(["osascript", "-l", "JavaScript", "-e", DIRECT, json.dumps(req)],
                           capture_output=True, text=True, timeout=TIMEOUT_S)
    except FileNotFoundError:
        return {"error": "osascript not found: this only works on macOS"}
    except subprocess.TimeoutExpired:
        return {"error": f"Notes did not answer within {TIMEOUT_S} s (a huge library or a password prompt?)"}
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if "-1743" in err or "not allowed" in err.lower() or "not authorized" in err.lower():
            return None
        if "-600" in err or "isn't running" in err.lower():
            err = "Notes.app could not be launched: " + err
        return {"error": err[:600]}
    try:
        return json.loads(r.stdout.strip())
    except ValueError:
        return {"error": "Notes returned something that is not JSON: " + r.stdout[:200]}


def run(action="status", query="", folder="", id="", title="", body="", text="", days=7, limit=20, in_body=False, snippet=SNIPPET, backend=""):
    req = {"action": action, "query": query, "folder": folder, "id": id, "title": title, "body": body, "text": text,
           "days": int(days or 7), "limit": max(1, min(int(limit or 20), LIMIT_MAX)), "in_body": bool(in_body), "snippet": int(snippet or SNIPPET)}
    if action == "search" and not query.strip():
        return {"error": "search needs a query", "action": action}
    if action in ("read", "append") and not (id or title):
        return {"error": f"{action} needs an id or a title", "action": action}
    if action == "create" and not (title.strip() or body.strip()):
        return {"error": "create needs a title or a body", "action": action}
    if action == "append" and not text.strip():
        return {"error": "append needs text", "action": action}
    out = None
    if backend in ("", "osascript"):
        out = _direct(req)
    if out is None and backend in ("", "bridge"):
        try:
            out = _bridge(req)
        except PermissionError as e:
            out = {"error": str(e)} if action in WRITES or backend == "bridge" else None
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            out = {"error": f"NotesBridge failed: {e}"} if action in WRITES or backend == "bridge" else None
    if out is None:  # reads only: neither path may control Notes
        try:
            out = _sqlite(req)
        except Exception as e:
            out = {"error": f"Notes automation is refused and the database fallback failed: {e}"}
    out.setdefault("action", action)
    return out


def main(action: str = "status", query: str = "", folder: str = "", id: str = "", title: str = "", body: str = "", text: str = "",
         days: int = 7, limit: int = 20, in_body: bool = False, snippet: int = SNIPPET, backend: str = "") -> dict:
    try:
        return run(action, query, folder, id, title, body, text, days, limit, in_body, snippet, backend)
    except Exception as e:  # never raise: the page and the bots read `error`
        return {"error": f"{type(e).__name__}: {e}", "action": action}


if __name__ == "__main__":
    import sys
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    print(json.dumps(main(**args), indent=2, ensure_ascii=False))
