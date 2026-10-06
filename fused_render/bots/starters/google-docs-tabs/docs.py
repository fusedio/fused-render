"""Google Docs Tabs — Python side. Called as main(action=..., ...) by index.html
and by the bot (see BOT.md). Runs on the user's own machine and authenticates
with a Google service-account key stored in .fused/data, so it is a bare
main(), not a @fused.udf. Docs must be shared with the service account's email.
"""
import json
import os
import re
import sys
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, ".fused", "data")
KEY_PATH = os.path.join(DATA_DIR, "service_account.json")
RECENT_PATH = os.path.join(DATA_DIR, "recent_docs.json")   # legacy, migrated into DOCS_PATH
DOCS_PATH = os.path.join(DATA_DIR, "docs.json")             # saved documents library
SCOPES = ["https://www.googleapis.com/auth/documents",
          "https://www.googleapis.com/auth/drive"]   # drive: comments live in the Drive API

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


def doc_url(doc_id):
    return "https://docs.google.com/document/d/%s/edit" % doc_id


def raw_doc_id(doc):
    """Document id from a Docs URL or bare id, or None."""
    doc = (doc or "").strip()
    m = re.search(r"/document/d/([A-Za-z0-9_-]+)", doc)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", doc):
        return doc
    return None


def doc_id_of(doc):
    """Resolve a Docs URL, a bare id, or the name of a saved document."""
    doc_id = raw_doc_id(doc)
    if doc_id:
        return doc_id
    saved = find_saved(doc)
    if saved:
        return saved["id"]
    docs = load_docs()
    hint = (" Saved documents: %s." % ", ".join(d["name"] for d in docs)) if docs else ""
    raise ValueError("Paste a Google Docs URL or a document id, or use the name of a saved document (see list_docs).%s" % hint)


# ------------------------------------------------------------ saved docs

def load_docs():
    """Saved documents: [{id, name, title, url, added_at, last_used}], most recently used first.
    Migrates the legacy recent_docs.json list on first read."""
    docs = read_json(DOCS_PATH, None)
    if docs is None:
        docs = []
        for r in read_json(RECENT_PATH, []):
            if r.get("id"):
                docs.append({"id": r["id"], "name": r.get("title") or r["id"], "title": r.get("title", ""),
                             "url": doc_url(r["id"]), "added_at": r.get("at", time.time()), "last_used": r.get("at", 0)})
        if docs:
            save_docs(docs)
    return docs


def save_docs(docs):
    docs.sort(key=lambda d: -(d.get("last_used") or 0))
    write_json(DOCS_PATH, docs)


def find_saved(key, docs=None):
    """Find a saved doc by id, URL, or name (exact then substring, case-insensitive)."""
    key = (key or "").strip()
    if not key:
        return None
    docs = load_docs() if docs is None else docs
    doc_id = raw_doc_id(key)
    if doc_id:
        return next((d for d in docs if d["id"] == doc_id), None)
    low = key.lower()
    for d in docs:
        if d["name"].strip().lower() == low or (d.get("title") or "").strip().lower() == low:
            return d
    hits = [d for d in docs if low in d["name"].lower() or low in (d.get("title") or "").lower()]
    return hits[0] if len(hits) == 1 else None


def upsert_doc(doc_id, title="", name=""):
    docs = load_docs()
    d = next((d for d in docs if d["id"] == doc_id), None)
    now = time.time()
    if d is None:
        d = {"id": doc_id, "name": "", "title": "", "url": doc_url(doc_id), "added_at": now, "last_used": now}
        docs.append(d)
    if title:
        d["title"] = title
    if name.strip():
        d["name"] = name.strip()
    if not d["name"]:
        d["name"] = d["title"] or doc_id
    d["last_used"] = now
    save_docs(docs)
    return d


def u16len(s):
    """Docs API indexes count UTF-16 code units."""
    return len(s.encode("utf-16-le")) // 2


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
    return build("docs", "v1", credentials=credentials(), cache_discovery=False)


def get_drive():
    from googleapiclient.discovery import build
    return build("drive", "v3", credentials=credentials(), cache_discovery=False)


def fetch_doc(svc, doc_id):
    return svc.documents().get(documentId=doc_id, includeTabsContent=True).execute()


def flatten_tabs(doc):
    out = []

    def walk(tabs, depth):
        for t in tabs or []:
            p = t.get("tabProperties", {})
            out.append({
                "id": p.get("tabId"),
                "title": p.get("title", ""),
                "index": p.get("index", 0),
                "depth": depth,
                "parent": p.get("parentTabId"),
                "_raw": t,
            })
            walk(t.get("childTabs"), depth + 1)

    walk(doc.get("tabs"), 0)
    return out


def tab_text(tab_raw):
    body = tab_raw.get("documentTab", {}).get("body", {})
    parts = []
    for el in body.get("content", []):
        para = el.get("paragraph")
        if not para:
            continue
        for run in para.get("elements", []):
            tr = run.get("textRun")
            if tr:
                parts.append(tr.get("content", ""))
    return "".join(parts)


def tab_end_index(tab_raw):
    content = tab_raw.get("documentTab", {}).get("body", {}).get("content", [])
    return content[-1]["endIndex"] if content else 2


def find_tab(tabs, key):
    key = (key or "").strip()
    if not key:
        raise ValueError("Which tab? Give a tab id or title.")
    for t in tabs:
        if t["id"] == key:
            return t
    low = key.lower()
    for t in tabs:
        if t["title"].strip().lower() == low:
            return t
    for t in tabs:
        if low in t["title"].lower():
            return t
    raise ValueError("No tab called '%s'. Tabs: %s" % (key, ", ".join(t["title"] for t in tabs)))


def public_tab(t, preview=True):
    d = {k: v for k, v in t.items() if k != "_raw"}
    if preview:
        d["preview"] = tab_text(t["_raw"]).strip()[:160]
    return d


def tab_start_index(tab_raw):
    """First editable index of a tab body: text starts after the leading
    section break, whose own startIndex (0) is omitted from the JSON."""
    content = tab_raw.get("documentTab", {}).get("body", {}).get("content", [])
    if content and "sectionBreak" in content[0]:
        return content[0].get("endIndex", 1)
    return content[0].get("startIndex", 1) if content else 1


def remember_doc(doc_id, title):
    """Every doc that is used gets saved to the library (name defaults to its title)."""
    upsert_doc(doc_id, title)


# ----------------------------------------------------- content -> requests

HEADING = {"#": "HEADING_1", "##": "HEADING_2", "###": "HEADING_3"}


def build_insert_requests(tab_id, end_index, content):
    """Append `content` at the end of a tab. Returns (requests, char_count).

    Light markdown: '# ' headings, '- ' bullets, '1. ' numbered lines.
    """
    lines = content.replace("\r\n", "\n").rstrip("\n").split("\n")
    insert_at = end_index - 1          # before the tab's final newline
    prefix = "\n" if insert_at > 1 else ""   # body already has text → new paragraph

    plain, kinds = [], []
    for line in lines:
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            plain.append(m.group(2)); kinds.append(("heading", HEADING[m.group(1)])); continue
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            plain.append(m.group(1)); kinds.append(("bullet", "BULLET_DISC_CIRCLE_SQUARE")); continue
        m = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        if m:
            plain.append(m.group(1)); kinds.append(("bullet", "NUMBERED_DECIMAL_ALPHA_ROMAN")); continue
        plain.append(line); kinds.append(("text", None))

    text = prefix + "\n".join(plain)
    reqs = [{"insertText": {"text": text, "location": {"index": insert_at, "tabId": tab_id}}}]

    # paragraph ranges of what we just inserted
    pos = insert_at + u16len(prefix)
    ranges = []
    for p in plain:
        start = pos
        pos += u16len(p) + 1          # + the newline that ends the paragraph
        ranges.append((start, pos))

    # new paragraphs inherit the style of the paragraph they were split from;
    # reset everything to NORMAL_TEXT first, then apply headings/bullets.
    whole = {"startIndex": ranges[0][0], "endIndex": ranges[-1][1], "tabId": tab_id}
    reqs.append({"updateParagraphStyle": {"range": whole,
                                          "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                          "fields": "namedStyleType"}})
    i = 0
    while i < len(kinds):
        kind, val = kinds[i]
        if kind == "heading":
            s, e = ranges[i]
            reqs.append({"updateParagraphStyle": {"range": {"startIndex": s, "endIndex": e, "tabId": tab_id},
                                                  "paragraphStyle": {"namedStyleType": val},
                                                  "fields": "namedStyleType"}})
            i += 1
        elif kind == "bullet":
            j = i
            while j < len(kinds) and kinds[j] == (kind, val):
                j += 1
            s, e = ranges[i][0], ranges[j - 1][1]
            reqs.append({"createParagraphBullets": {"range": {"startIndex": s, "endIndex": e, "tabId": tab_id},
                                                    "bulletPreset": val}})
            i = j
        else:
            i += 1
    return reqs, u16len(text)


def insert_content(svc, doc_id, tab, content):
    if not (content or "").strip():
        return 0
    reqs, n = build_insert_requests(tab["id"], tab_end_index(tab["_raw"]), content)
    svc.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()
    return n


def replace_content(svc, doc_id, tab, content):
    """Overwrite a tab: delete everything in its body, then insert `content`."""
    start, end = tab_start_index(tab["_raw"]), tab_end_index(tab["_raw"])
    if end - 1 > start:   # keep the final newline the API refuses to delete
        svc.documents().batchUpdate(documentId=doc_id, body={"requests": [
            {"deleteContentRange": {"range": {"startIndex": start, "endIndex": end - 1, "tabId": tab["id"]}}}
        ]}).execute()
    if not (content or "").strip():
        return 0
    reqs, n = build_insert_requests(tab["id"], start + 1, content)
    svc.documents().batchUpdate(documentId=doc_id, body={"requests": reqs}).execute()
    return n


# --------------------------------------------------------------- comments
# Comments on a Google Doc are read and written through the Drive API
# (comments / replies resources), not the Docs API. Comments made here are
# unanchored (they sit in the doc's comment list, not on a text selection).

COMMENT_FIELDS = ("id,content,author(displayName,emailAddress),createdTime,modifiedTime,resolved,"
                  "quotedFileContent(value),replies(id,content,author(displayName,emailAddress),"
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
        "replies": [{
            "id": r.get("id"),
            "content": r.get("content", ""),
            "author": (r.get("author") or {}).get("displayName", ""),
            "created": r.get("createdTime"),
            "action": r.get("action"),
        } for r in c.get("replies", [])],
    }


def list_comments(drv, doc_id, include_resolved=False):
    out, token = [], None
    while True:
        resp = drv.comments().list(fileId=doc_id, pageSize=100, pageToken=token,
                                   fields="nextPageToken,comments(%s)" % COMMENT_FIELDS).execute()
        out.extend(resp.get("comments", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return [public_comment(c) for c in out if include_resolved or not c.get("resolved")]


def add_comment(drv, doc_id, content):
    if not (content or "").strip():
        raise ValueError("Comment text is required.")
    c = drv.comments().create(fileId=doc_id, body={"content": content.strip()},
                              fields=COMMENT_FIELDS).execute()
    return public_comment(c)


def reply_comment(drv, doc_id, comment_id, content, action=None):
    if not (comment_id or "").strip():
        raise ValueError("Which comment? Give a comment id (see list_comments).")
    body = {}
    if (content or "").strip():
        body["content"] = content.strip()
    if action:
        body["action"] = action          # "resolve" | "reopen"
    if not body:
        raise ValueError("Reply text is required.")
    drv.replies().create(fileId=doc_id, commentId=comment_id.strip(), body=body, fields="id").execute()
    c = drv.comments().get(fileId=doc_id, commentId=comment_id.strip(), fields=COMMENT_FIELDS).execute()
    return public_comment(c)


# --------------------------------------------------------- plain-English

CREATE_RE = re.compile(
    r"^(?:please\s+)?(?:create|add|make|open|start)\s+(?:a\s+)?(?:new\s+)?tab\s*"
    r"(?:called|named|titled|for|:)?\s*[\"“”']?(?P<title>[^\"“”'\n]+?)[\"“”']?\s*"
    r"(?:(?:,|and|then|;)?\s*(?:add|with|put|write|containing|insert|fill(?: it)? with)\s*"
    r"(?:this|the following|these|info|information|content|text|notes?)?\s*:?\s*(?P<content>.+))?$",
    re.S | re.I)
APPEND_RE = re.compile(
    r"^(?:please\s+)?(?:add|append|write|put|insert)\s+(?P<content>.+?)\s+"
    r"(?:to|in|into|on|under)\s+(?:the\s+)?(?:tab\s+)?[\"“”']?(?P<title>[^\"“”'\n]+?)[\"“”']?\s*(?:tab)?\s*$",
    re.S | re.I)
APPEND_RE2 = re.compile(
    r"^(?:please\s+)?(?:in|on|under)\s+(?:the\s+)?(?:tab\s+)?[\"“”']?(?P<title>[^\"“”'\n:,]+?)[\"“”']?\s*(?:tab)?\s*[:,]?\s*"
    r"(?:add|append|write|put|insert)\s*(?:this|the following)?\s*:?\s*(?P<content>.+)$",
    re.S | re.I)


def parse_command_regex(cmd):
    cmd = cmd.strip()
    if re.match(r"^(?:list|show|what)\b.*\bcomments?\b", cmd, re.I):
        return {"action": "list_comments"}
    m = re.match(r"^(?:please\s+)?(?:add|leave|post|write)\s+(?:a\s+)?comment\s*(?:saying|:)?\s*(?P<content>.+)$", cmd, re.S | re.I)
    if m:
        return {"action": "add_comment", "content": m.group("content").strip().strip("\"'\u201c\u201d")}
    if re.match(r"^(?:list|show|what)\b.*\btabs?\b", cmd, re.I):
        return {"action": "list_tabs"}
    m = CREATE_RE.match(cmd)
    if m:
        return {"action": "create_tab", "title": m.group("title").strip(),
                "content": (m.group("content") or "").strip()}
    m = APPEND_RE2.match(cmd) or APPEND_RE.match(cmd)
    if m:
        return {"action": "append", "title": m.group("title").strip(),
                "content": m.group("content").strip().strip("\"'\u201c\u201d")}
    return None


AI_SYSTEM = (
    "You turn a user's instruction about a Google Doc into one JSON object and nothing else. "
    "Schema: {\"action\": \"create_tab\"|\"append\"|\"write_tab\"|\"list_tabs\"|\"read_tab\""
    "|\"list_comments\"|\"add_comment\", "
    "\"title\": string (tab title), \"content\": string (text to put in the tab or the comment, may be empty)}. "
    "write_tab replaces a tab's whole contents; append adds to the end. add_comment leaves a comment on the doc. "
    "Keep the user's content wording; you may format it with markdown headings (#) and '- ' bullets. "
    "If the user asks for information to be written or generated (e.g. 'add a summary of X'), write that "
    "content yourself in the content field. Output only the JSON."
)


def parse_command(cmd, tabs):
    try:
        import fused_ai
        prompt = "Existing tabs: %s\n\nInstruction: %s" % (
            ", ".join(t["title"] for t in tabs) or "(none)", cmd)
        raw = fused_ai.text(prompt, systemPrompt=AI_SYSTEM)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and parsed.get("action"):
            parsed["via"] = "ai"
            return parsed
    except Exception as e:  # noqa: BLE001 — regex fallback below
        print("fused_ai unavailable or failed, using regex parser:", e)
    parsed = parse_command_regex(cmd)
    if parsed:
        parsed["via"] = "regex"
    return parsed


# ------------------------------------------------------------------ main

def main(action: str = "status", doc: str = "", title: str = "", content: str = "",
         tab: str = "", parent: str = "", key_json: str = "", command: str = "",
         comment: str = "", resolved: bool = False):
    try:
        return run(action, doc, title, content, tab, parent, key_json, command, comment, resolved)
    except Exception as e:  # noqa: BLE001 — surface as data for the page/bot
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
            if status == 403 and ("has not been used" in msg or "is disabled" in msg) and "drive" in msg.lower():
                msg = ("The Google Drive API is not enabled for the service account's project; comments need it. "
                       "Enable it in Cloud Console (APIs & Services → Library → Google Drive API). Google said: %s" % msg)
            elif status in (403, 404) and email and action not in ("status", "save_key", "disconnect"):
                msg = ("The service account cannot open this document. Share the doc with %s "
                       "as an Editor (Share → add people), then try again. Google said: %s" % (email, msg))
        return {"error": msg, "action": action}


COMMENT_ACTIONS = ("list_comments", "add_comment", "reply_comment", "resolve_comment",
                   "reopen_comment", "delete_comment")


def run(action, doc, title, content, tab, parent, key_json, command, comment="", resolved=False):
    os.makedirs(DATA_DIR, exist_ok=True)

    if action == "status":
        return {
            "connected": os.path.exists(KEY_PATH),
            "email": account_email(),
            "docs": load_docs(),
        }

    if action == "list_docs":
        docs = load_docs()
        return {"count": len(docs), "docs": docs}

    if action == "rename_doc":
        d = find_saved(doc)
        if not d:
            raise ValueError("No saved document matches '%s'. Saved: %s" % (doc, ", ".join(x["name"] for x in load_docs()) or "none"))
        if not title.strip():
            raise ValueError("Give the document a new name in `title`.")
        d = upsert_doc(d["id"], name=title)
        return {"ok": True, "doc": d}

    if action == "remove_doc":
        d = find_saved(doc)
        if not d:
            raise ValueError("No saved document matches '%s'." % doc)
        save_docs([x for x in load_docs() if x["id"] != d["id"]])
        return {"ok": True, "removed": d}

    if action == "save_key":
        data = load_key(key_json)
        write_json(KEY_PATH, data)
        # prove the key works: mint an access token now, not on the first doc call
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
    doc_id = doc_id_of(doc)

    if action == "add_doc":
        # verify the doc opens (shared with the service account) and pick up its title
        document = fetch_doc(get_service(), doc_id)
        d = upsert_doc(doc_id, document.get("title", ""), name=title)
        return {"ok": True, "doc": d, "tabs": len(flatten_tabs(document))}

    if action in COMMENT_ACTIONS:
        drv = get_drive()
        url = "https://docs.google.com/document/d/%s/edit" % doc_id
        if action == "list_comments":
            cs = list_comments(drv, doc_id, include_resolved=bool(resolved))
            return {"doc_id": doc_id, "url": url, "count": len(cs), "comments": cs}
        if action == "add_comment":
            return {"ok": True, "url": url, "comment": add_comment(drv, doc_id, content)}
        if action == "reply_comment":
            return {"ok": True, "url": url, "comment": reply_comment(drv, doc_id, comment, content)}
        if action == "resolve_comment":
            return {"ok": True, "url": url, "comment": reply_comment(drv, doc_id, comment, content, "resolve")}
        if action == "reopen_comment":
            return {"ok": True, "url": url, "comment": reply_comment(drv, doc_id, comment, content, "reopen")}
        if action == "delete_comment":
            if not (comment or "").strip():
                raise ValueError("Which comment? Give a comment id (see list_comments).")
            drv.comments().delete(fileId=doc_id, commentId=comment.strip()).execute()
            return {"ok": True, "url": url, "deleted": comment.strip()}

    svc = get_service()
    document = fetch_doc(svc, doc_id)
    tabs = flatten_tabs(document)
    remember_doc(doc_id, document.get("title", ""))

    if action == "list_tabs":
        return {"doc_id": doc_id, "title": document.get("title", ""),
                "url": "https://docs.google.com/document/d/%s/edit" % doc_id,
                "tabs": [public_tab(t) for t in tabs]}

    if action == "read_tab":
        t = find_tab(tabs, tab)
        return {"tab": public_tab(t, preview=False), "text": tab_text(t["_raw"])}

    if action == "create_tab":
        if not title.strip():
            raise ValueError("A tab title is required.")
        props = {"title": title.strip()}
        if parent.strip():
            props["parentTabId"] = find_tab(tabs, parent)["id"]
        resp = svc.documents().batchUpdate(
            documentId=doc_id, body={"requests": [{"addDocumentTab": {"tabProperties": props}}]}).execute()
        new_id = None
        try:
            new_id = resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]
        except Exception:
            pass
        document = fetch_doc(svc, doc_id)
        tabs = flatten_tabs(document)
        if new_id:
            new_tab = next(t for t in tabs if t["id"] == new_id)
        else:
            new_tab = max((t for t in tabs if t["title"] == title.strip()), key=lambda t: t["index"])
        n = insert_content(svc, doc_id, new_tab, content)
        return {"ok": True, "tab": public_tab(new_tab, preview=False), "inserted": n,
                "url": "https://docs.google.com/document/d/%s/edit?tab=%s" % (doc_id, new_tab["id"])}

    if action == "append":
        t = find_tab(tabs, tab)
        n = insert_content(svc, doc_id, t, content)
        return {"ok": True, "tab": public_tab(t, preview=False), "inserted": n,
                "url": "https://docs.google.com/document/d/%s/edit?tab=%s" % (doc_id, t["id"])}

    if action == "write_tab":
        t = find_tab(tabs, tab)
        n = replace_content(svc, doc_id, t, content)
        return {"ok": True, "tab": public_tab(t, preview=False), "inserted": n,
                "url": "https://docs.google.com/document/d/%s/edit?tab=%s" % (doc_id, t["id"])}

    if action == "command":
        parsed = parse_command(command, tabs)
        if not parsed:
            raise ValueError("I could not understand that. Try: create a tab called Notes and add: hello")
        a = parsed.get("action")
        if a == "list_tabs":
            result = {"tabs": [public_tab(t) for t in tabs]}
        elif a == "read_tab":
            result = run("read_tab", doc, "", "", parsed.get("title", ""), "", "", "")
        elif a == "create_tab":
            result = run("create_tab", doc, parsed.get("title", ""), parsed.get("content", ""), "", "", "", "")
        elif a == "append":
            result = run("append", doc, "", parsed.get("content", ""), parsed.get("title", ""), "", "", "")
        elif a == "write_tab":
            result = run("write_tab", doc, "", parsed.get("content", ""), parsed.get("title", ""), "", "", "")
        elif a == "list_comments":
            result = run("list_comments", doc, "", "", "", "", "", "")
        elif a == "add_comment":
            result = run("add_comment", doc, "", parsed.get("content", ""), "", "", "", "")
        else:
            raise ValueError("Unknown action from parser: %s" % a)
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(result["error"])
        return {"ok": True, "interpreted": parsed, "result": result}

    raise ValueError("Unknown action: %s" % action)
