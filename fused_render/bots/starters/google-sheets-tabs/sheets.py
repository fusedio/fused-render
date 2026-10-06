"""Google Sheets Tabs — Python side. Called as main(action=..., ...) by index.html
and by the bot (see BOT.md). Runs on the user's own machine and authenticates
with a Google service-account key stored in .fused/data, so it is a bare
main(), not a @fused.udf. Spreadsheets must be shared with the service
account's email. Companion to the google-docs-tabs app: same key, same flow.
"""
import csv
import io
import json
import os
import re
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, ".fused", "data")
KEY_PATH = os.path.join(DATA_DIR, "service_account.json")
DOCS_PATH = os.path.join(DATA_DIR, "sheets.json")          # saved spreadsheets library
SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive.readonly"]   # drive: comments live in the Drive API
MAX_READ_ROWS = 10000


def _unshadow_google():
    """FusedRender's bundled python312.zip carries its own (older, partial)
    `google` package, which hides the venv's google-* packages. Put the venv's
    google/ first on the package path and drop anything already imported."""
    import sysconfig
    venv_google = os.path.join(sysconfig.get_paths()["purelib"], "google")
    if not os.path.isdir(venv_google):
        return
    try:
        import google
    except ImportError:
        return
    path = getattr(google, "__path__", None)
    if path is None:
        return
    path[:] = [venv_google] + [p for p in list(path) if p != venv_google]
    for name in [m for m in sys.modules if m.startswith("google.")]:
        del sys.modules[name]


_unshadow_google()


# ---------------------------------------------------------------- utilities

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def doc_url(sheet_id, gid=None):
    url = "https://docs.google.com/spreadsheets/d/%s/edit" % sheet_id
    return url + ("#gid=%s" % gid if gid is not None else "")


def raw_doc_id(doc):
    """Spreadsheet id from a Sheets URL or bare id, or None."""
    doc = (doc or "").strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", doc)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", doc):
        return doc
    return None


def doc_id_of(doc):
    """Resolve a Sheets URL, a bare id, or the name of a saved spreadsheet."""
    sheet_id = raw_doc_id(doc)
    if sheet_id:
        return sheet_id
    saved = find_saved(doc)
    if saved:
        return saved["id"]
    docs = load_docs()
    hint = (" Saved spreadsheets: %s." % ", ".join(d["name"] for d in docs)) if docs else ""
    raise ValueError("Paste a Google Sheets URL or a spreadsheet id, or use the name of a saved "
                     "spreadsheet (see list_docs).%s" % hint)


# ------------------------------------------------------- saved spreadsheets

def load_docs():
    """Saved spreadsheets: [{id, name, title, url, added_at, last_used}], most recently used first."""
    return read_json(DOCS_PATH, [])


def save_docs(docs):
    docs.sort(key=lambda d: -(d.get("last_used") or 0))
    write_json(DOCS_PATH, docs)


def find_saved(key, docs=None):
    """Find a saved spreadsheet by id, URL, or name (exact then unique substring, case-insensitive)."""
    key = (key or "").strip()
    if not key:
        return None
    docs = load_docs() if docs is None else docs
    sheet_id = raw_doc_id(key)
    if sheet_id:
        return next((d for d in docs if d["id"] == sheet_id), None)
    low = key.lower()
    for d in docs:
        if d["name"].strip().lower() == low or (d.get("title") or "").strip().lower() == low:
            return d
    hits = [d for d in docs if low in d["name"].lower() or low in (d.get("title") or "").lower()]
    return hits[0] if len(hits) == 1 else None


def upsert_doc(sheet_id, title="", name=""):
    docs = load_docs()
    d = next((d for d in docs if d["id"] == sheet_id), None)
    now = time.time()
    if d is None:
        d = {"id": sheet_id, "name": "", "title": "", "url": doc_url(sheet_id), "added_at": now, "last_used": now}
        docs.append(d)
    if title:
        d["title"] = title
    if name.strip():
        d["name"] = name.strip()
    if not d["name"]:
        d["name"] = d["title"] or sheet_id
    d["last_used"] = now
    save_docs(docs)
    return d


# ---------------------------------------------------------------- google

def account_email():
    return read_json(KEY_PATH, {}).get("client_email", "")


def load_key(text):
    """Parse and sanity-check a service-account key JSON."""
    try:
        data = json.loads(text or "")
    except Exception:
        raise ValueError("That is not JSON. Paste the whole key file Google downloaded.")
    if not isinstance(data, dict) or data.get("type") != "service_account":
        if isinstance(data, dict) and ("installed" in data or "web" in data):
            raise ValueError("That is an OAuth client, not a service-account key. "
                             "In Cloud Console open the service account → Keys → Add key → JSON.")
        raise ValueError("That is not a service-account key (expected \"type\": \"service_account\").")
    for k in ("client_email", "private_key"):
        if not data.get(k):
            raise ValueError("The key is missing '%s'. Download a fresh JSON key." % k)
    return data


def credentials(path=KEY_PATH):
    from google.oauth2.service_account import Credentials
    if not os.path.exists(path):
        raise RuntimeError("No Google service-account key saved. Paste one in Setup first.")
    return Credentials.from_service_account_file(path, scopes=SCOPES)


def get_service():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=credentials(), cache_discovery=False)


def get_drive():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=credentials(), cache_discovery=False)


META_FIELDS = ("spreadsheetId,properties(title),sheets(properties(sheetId,title,index,hidden,sheetType,"
               "gridProperties(rowCount,columnCount,frozenRowCount)))")


def fetch_meta(svc, sheet_id):
    return svc.spreadsheets().get(spreadsheetId=sheet_id, fields=META_FIELDS).execute()


def flatten_tabs(meta):
    out = []
    for s in meta.get("sheets", []):
        p = s.get("properties", {})
        g = p.get("gridProperties", {})
        out.append({
            "id": str(p.get("sheetId")),
            "title": p.get("title", ""),
            "index": p.get("index", 0),
            "type": p.get("sheetType", "GRID"),
            "hidden": bool(p.get("hidden")),
            "rows": g.get("rowCount", 0),
            "cols": g.get("columnCount", 0),
            "frozen_rows": g.get("frozenRowCount", 0),
        })
    out.sort(key=lambda t: t["index"])
    return out


def find_tab(tabs, key, exact=False):
    """Tab by id (gid) or title: exact, then (unless exact=True) substring."""
    key = (key or "").strip()
    if not key:
        raise ValueError("Which tab? Give a worksheet id (gid) or title.")
    for t in tabs:
        if t["id"] == key:
            return t
    low = key.lower()
    for t in tabs:
        if t["title"].strip().lower() == low:
            return t
    if not exact:
        for t in tabs:
            if low in t["title"].lower():
                return t
    raise ValueError("No tab %s '%s'. Tabs: %s" % ("exactly called" if exact else "called", key,
                                                  ", ".join(t["title"] for t in tabs)))


def quote_title(title):
    return "'%s'" % title.replace("'", "''")


def a1(tab, rng=""):
    """A1 range on a worksheet. rng may be empty (whole sheet), 'A1:D20', or already 'Sheet!A1'."""
    rng = (rng or "").strip()
    if "!" in rng:
        return rng
    return quote_title(tab["title"]) + ("!" + rng if rng else "")


def col_letter(n):
    """1 -> A, 27 -> AA."""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def rect(values):
    """Pad ragged rows to a rectangle of strings/numbers."""
    width = max((len(r) for r in values), default=0)
    return [list(r) + [""] * (width - len(r)) for r in values], width


# ----------------------------------------------------------- content -> rows

def parse_rows(content):
    """Turn `content` into a list of rows.

    Accepts a JSON array of arrays (rows), a JSON array of objects (header row
    from the keys), a JSON array of scalars (one row), TSV (any tab present,
    e.g. pasted from a spreadsheet) or CSV. Strings starting with '=' are
    formulas when the write uses USER_ENTERED (the default).
    """
    text = (content or "").replace("\r\n", "\n").strip("\n")
    if not text.strip():
        return []
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except Exception:
            data = None
        if isinstance(data, list):
            if data and all(isinstance(r, dict) for r in data):
                header = []
                for r in data:
                    for k in r:
                        if k not in header:
                            header.append(k)
                return [header] + [[cell(r.get(k)) for k in header] for r in data]
            if all(isinstance(r, list) for r in data):
                return [[cell(v) for v in r] for r in data]
            return [[cell(v) for v in data]]
    delim = "\t" if "\t" in text else ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delim)]


def cell(v):
    if v is None:
        return ""
    if isinstance(v, (bool, int, float, str)):
        return v
    return json.dumps(v)


def ensure_grid(svc, sheet_id, tab, need_rows, need_cols):
    """values.update refuses to write past the grid; grow the sheet first if needed."""
    rows = max(tab["rows"], need_rows)
    cols = max(tab["cols"], need_cols)
    if rows == tab["rows"] and cols == tab["cols"]:
        return
    svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
        "updateSheetProperties": {
            "properties": {"sheetId": int(tab["id"]), "gridProperties": {"rowCount": rows, "columnCount": cols}},
            "fields": "gridProperties(rowCount,columnCount)",
        }}]}).execute()
    tab["rows"], tab["cols"] = rows, cols


def start_cell(rng):
    """Top-left of an A1 range like 'B3:D9' -> (row, col) 1-based. Defaults to A1."""
    m = re.match(r"^\$?([A-Za-z]+)\$?(\d+)", (rng or "").split("!")[-1].strip())
    if not m:
        return 1, 1
    col = 0
    for ch in m.group(1).upper():
        col = col * 26 + (ord(ch) - 64)
    return int(m.group(2)), col


def write_values(svc, sheet_id, tab, rows, rng="", raw=False):
    """Replace a worksheet (or `rng` on it): clear, then write `rows` from its top-left cell."""
    svc.spreadsheets().values().clear(spreadsheetId=sheet_id, range=a1(tab, rng), body={}).execute()
    if not rows:
        return {"updatedRows": 0, "updatedCells": 0, "updatedRange": a1(tab, rng)}
    r0, c0 = start_cell(rng)
    width = max(len(r) for r in rows)
    ensure_grid(svc, sheet_id, tab, r0 + len(rows) - 1, c0 + width - 1)
    target = "%s%d" % (col_letter(c0), r0)
    return svc.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=a1(tab, target),
        valueInputOption="RAW" if raw else "USER_ENTERED",
        body={"values": rows}).execute()


def append_values(svc, sheet_id, tab, rows, raw=False):
    resp = svc.spreadsheets().values().append(
        spreadsheetId=sheet_id, range=a1(tab),
        valueInputOption="RAW" if raw else "USER_ENTERED",
        insertDataOption="INSERT_ROWS", body={"values": rows}).execute()
    return resp.get("updates", {})


def header_previews(svc, sheet_id, tabs):
    """First row of every grid worksheet in one call, for the tab list."""
    grid = [t for t in tabs if t["type"] == "GRID"]
    if not grid:
        return {}
    try:
        resp = svc.spreadsheets().values().batchGet(
            spreadsheetId=sheet_id, ranges=[a1(t, "1:1") for t in grid]).execute()
    except Exception as e:  # noqa: BLE001 — previews are decoration
        print("header preview failed:", e)
        return {}
    out = {}
    for t, vr in zip(grid, resp.get("valueRanges", [])):
        first = (vr.get("values") or [[]])[0]
        out[t["id"]] = " · ".join(str(v) for v in first if str(v).strip())[:160]
    return out


# --------------------------------------------------------------- comments
# Comments on a spreadsheet live in the Drive API (comments / replies), not
# the Sheets API. Their anchors are opaque to the API, so the list shows the
# quoted cell text when Google provides it.

COMMENT_FIELDS = ("id,content,author(displayName,emailAddress),createdTime,modifiedTime,resolved,"
                  "anchor,quotedFileContent(value),replies(id,content,author(displayName,emailAddress),"
                  "createdTime,action)")


def public_comment(c):
    return {
        "id": c.get("id"),
        "content": c.get("content", ""),
        "author": (c.get("author") or {}).get("displayName", ""),
        "created": c.get("createdTime"),
        "modified": c.get("modifiedTime"),
        "resolved": bool(c.get("resolved")),
        "quoted": (c.get("quotedFileContent") or {}).get("value", ""),
        "anchor": c.get("anchor", ""),
        "replies": [{
            "id": r.get("id"),
            "content": r.get("content", ""),
            "author": (r.get("author") or {}).get("displayName", ""),
            "created": r.get("createdTime"),
            "action": r.get("action"),
        } for r in c.get("replies", [])],
    }


def list_comments(drv, sheet_id, include_resolved=False):
    out, token = [], None
    while True:
        resp = drv.comments().list(fileId=sheet_id, pageSize=100, pageToken=token,
                                   fields="nextPageToken,comments(%s)" % COMMENT_FIELDS).execute()
        out.extend(resp.get("comments", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return [public_comment(c) for c in out if include_resolved or not c.get("resolved")]


# ------------------------------------------------------------------ main

def main(action: str = "status", doc: str = "", title: str = "", content: str = "",
         tab: str = "", range: str = "", key_json: str = "", raw: bool = False,
         limit: int = 1000, resolved: bool = False):
    try:
        return run(action, doc, title, content, tab, range, key_json, raw, limit, resolved)
    except Exception as e:  # noqa: BLE001 — surface as data for the page/bot
        return {"error": explain_error(e, action), "action": action}


def explain_error(e, action=""):
    """A Google/auth exception as one plain sentence for the page or bot (also used by styling.py)."""
    msg = str(e)
    name = type(e).__name__
    if name == "RefreshError":
        msg = ("Google rejected the service-account key (%s). It may be deleted or disabled; "
               "click Setup and paste a fresh key." % msg.split("\n")[0][:160])
    elif name == "ValueError" and ("private key" in msg.lower() or "malformed" in msg.lower()):
        msg = "The saved key is unreadable. Click Setup and paste it again."
    if "HttpError" in name:
        status = getattr(getattr(e, "resp", None), "status", None)
        try:
            msg = json.loads(e.content.decode())["error"]["message"]
        except Exception:
            pass
        email = account_email()
        low = msg.lower()
        if status == 403 and ("has not been used" in low or "is disabled" in low):
            which = "Google Drive API (comments need it)" if "drive" in low else "Google Sheets API"
            msg = ("The %s is not enabled for the service account's project. Enable it in Cloud Console "
                   "(APIs & Services → Library). Google said: %s" % (which, msg))
        elif status == 400 and "not supported for this document" in low:
            msg = ("That file is an Excel/CSV file stored in Drive, not a Google Sheet. Open it in Sheets "
                   "and use File → Save as Google Sheets, then load the new link.")
        elif status in (403, 404) and email and action not in ("status", "save_key", "disconnect"):
            msg = ("The service account cannot open this spreadsheet. Share it with %s "
                   "as an Editor (Share → add people), then try again. Google said: %s" % (email, msg))
    return msg


def run(action, doc, title, content, tab, rng, key_json, raw=False, limit=1000, resolved=False):
    os.makedirs(DATA_DIR, exist_ok=True)

    if action == "status":
        return {"connected": os.path.exists(KEY_PATH), "email": account_email(), "docs": load_docs()}

    if action == "list_docs":
        docs = load_docs()
        return {"count": len(docs), "docs": docs}

    if action == "rename_doc":
        d = find_saved(doc)
        if not d:
            raise ValueError("No saved spreadsheet matches '%s'. Saved: %s"
                             % (doc, ", ".join(x["name"] for x in load_docs()) or "none"))
        if not title.strip():
            raise ValueError("Give the spreadsheet a new name in `title`.")
        return {"ok": True, "doc": upsert_doc(d["id"], name=title)}

    if action == "remove_doc":
        d = find_saved(doc)
        if not d:
            raise ValueError("No saved spreadsheet matches '%s'." % doc)
        save_docs([x for x in load_docs() if x["id"] != d["id"]])
        return {"ok": True, "removed": d}

    if action == "save_key":
        data = load_key(key_json)
        write_json(KEY_PATH, data)
        # prove the key works: mint an access token now, not on the first sheet call
        try:
            from google.auth.transport.requests import Request
            credentials().refresh(Request())
        except Exception as e:
            os.remove(KEY_PATH)
            raise RuntimeError("Google rejected that key: %s" % str(e).split("\n")[0][:200])
        return {"ok": True, "email": data["client_email"]}

    if action == "disconnect":
        if os.path.exists(KEY_PATH):
            os.remove(KEY_PATH)
        return {"ok": True}

    # ---- everything below talks to Google
    sheet_id = doc_id_of(doc)

    if action == "list_comments":
        cs = list_comments(get_drive(), sheet_id, include_resolved=bool(resolved))
        return {"doc_id": sheet_id, "url": doc_url(sheet_id), "count": len(cs), "comments": cs}

    svc = get_service()
    meta = fetch_meta(svc, sheet_id)
    tabs = flatten_tabs(meta)
    sheet_title = meta.get("properties", {}).get("title", "")
    saved = upsert_doc(sheet_id, sheet_title, name=title if action == "add_doc" else "")

    if action == "add_doc":
        return {"ok": True, "doc": saved, "tabs": len(tabs)}

    if action == "list_tabs":
        previews = header_previews(svc, sheet_id, tabs)
        for t in tabs:
            t["preview"] = previews.get(t["id"], "")
            t["url"] = doc_url(sheet_id, t["id"])
        return {"doc_id": sheet_id, "title": sheet_title, "url": doc_url(sheet_id), "tabs": tabs}

    if action == "read_tab":
        t = find_tab(tabs, tab)
        if t["type"] != "GRID":
            return {"tab": t, "values": [], "rows": 0, "cols": 0, "truncated": False,
                    "url": doc_url(sheet_id, t["id"]), "note": "This tab is a %s, not a grid of cells." % t["type"].lower()}
        resp = svc.spreadsheets().values().get(spreadsheetId=sheet_id, range=a1(t, rng),
                                               valueRenderOption="FORMATTED_VALUE").execute()
        values, width = rect(resp.get("values", []))
        cap = max(1, min(int(limit or 1000), MAX_READ_ROWS))
        return {"tab": t, "range": resp.get("range", a1(t, rng)), "rows": len(values), "cols": width,
                "truncated": len(values) > cap, "values": values[:cap], "url": doc_url(sheet_id, t["id"])}

    if action == "create_tab":
        if not title.strip():
            raise ValueError("A tab title is required.")
        rows = parse_rows(content)
        width = max((len(r) for r in rows), default=0)
        props = {"title": title.strip(),
                 "gridProperties": {"rowCount": max(1000, len(rows)), "columnCount": max(26, width)}}
        resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet_id, body={"requests": [{"addSheet": {"properties": props}}]}).execute()
        p = resp["replies"][0]["addSheet"]["properties"]
        g = p.get("gridProperties", {})
        new_tab = {"id": str(p["sheetId"]), "title": p["title"], "index": p.get("index", len(tabs)),
                   "type": p.get("sheetType", "GRID"), "hidden": False,
                   "rows": g.get("rowCount", 0), "cols": g.get("columnCount", 0), "frozen_rows": 0}
        written = 0
        if rows:
            written = write_values(svc, sheet_id, new_tab, rows, raw=raw).get("updatedRows", 0)
        return {"ok": True, "tab": new_tab, "written_rows": written, "url": doc_url(sheet_id, new_tab["id"])}

    if action == "append":
        t = find_tab(tabs, tab)
        rows = parse_rows(content)
        if not rows:
            raise ValueError("Nothing to append. Give rows as CSV, TSV or a JSON array of arrays.")
        up = append_values(svc, sheet_id, t, rows, raw=raw)
        return {"ok": True, "tab": t, "appended_rows": up.get("updatedRows", len(rows)),
                "updated_range": up.get("updatedRange", ""), "url": doc_url(sheet_id, t["id"])}

    if action == "write_tab":
        t = find_tab(tabs, tab)
        rows = parse_rows(content)
        up = write_values(svc, sheet_id, t, rows, rng, raw=raw)
        return {"ok": True, "tab": t, "written_rows": up.get("updatedRows", 0),
                "updated_range": up.get("updatedRange", ""), "url": doc_url(sheet_id, t["id"])}

    if action == "rename_tab":
        t = find_tab(tabs, tab)
        new_title = title.strip()
        if not new_title:
            raise ValueError("Give the tab a new name in `title`.")
        clash = next((x for x in tabs if x["id"] != t["id"] and x["title"].strip().lower() == new_title.lower()), None)
        if clash:
            raise ValueError("Another tab is already called '%s'." % clash["title"])
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
            "updateSheetProperties": {"properties": {"sheetId": int(t["id"]), "title": new_title},
                                      "fields": "title"}}]}).execute()
        old_title, t["title"] = t["title"], new_title
        return {"ok": True, "tab": t, "old_title": old_title, "url": doc_url(sheet_id, t["id"])}

    if action == "delete_tab":
        # exact match only: a substring guess must never pick the tab to delete
        t = find_tab(tabs, tab, exact=True)
        if len(tabs) <= 1:
            raise ValueError("'%s' is the only tab in this spreadsheet; a spreadsheet must keep at "
                             "least one tab, so it was not deleted." % t["title"])
        if not t["hidden"] and not any(not x["hidden"] for x in tabs if x["id"] != t["id"]):
            raise ValueError("'%s' is the last visible tab; Google Sheets needs one visible tab, so it "
                             "was not deleted. Unhide another tab first." % t["title"])
        svc.spreadsheets().batchUpdate(spreadsheetId=sheet_id, body={"requests": [{
            "deleteSheet": {"sheetId": int(t["id"])}}]}).execute()
        remaining = [x for x in tabs if x["id"] != t["id"]]
        return {"ok": True, "deleted": t, "remaining": [{"id": x["id"], "title": x["title"]} for x in remaining],
                "url": doc_url(sheet_id)}

    raise ValueError("Unknown action: %s" % action)
