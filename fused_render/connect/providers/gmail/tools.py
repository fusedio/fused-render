"""Gmail's tools, native: the Gmail REST API called from this process (SPEC-connect-oauth §4, §11).

Google hosts no MCP server for Gmail, so the pack ships its tools here, the
Slack shape: the user token from the PKCE sign-in as Bearer over `ctx.http`,
loaded by path by the core `tools.py`; the token never reaches the caller.

Results are trimmed on purpose: a Gmail message is a MIME tree of base64
parts, headers by the dozen and tracking HTML that cost a model thousands of
tokens. What is kept: id, thread id, labels, From/To/Cc/Subject/Date, the
snippet, and for one message or thread the text body (text/plain, else the
HTML with its tags stripped), capped, with the attachments listed by name.

Google answers errors as `{error: {code, status, message}}`; `google_api.api`
turns a 401 into `ToolAuthError` (refreshed once by the core) and the rest
into `ToolError` with Google's own message.
"""
from __future__ import annotations

import base64
import html as _html
import re
from email.message import EmailMessage
from email.utils import parseaddr

from fused_render.connect.google_api import api, clamp, dispatch, obj
from fused_render.connect.tools import ToolError

API = "https://gmail.googleapis.com/gmail/v1/users/me/"
#: What one body may cost a model; the rest is `truncated: true`.
BODY_CAP = 20_000
THREAD_BODY_CAP = 8_000
_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID", "References", "Reply-To"]

TOOLS = [
    {"name": "gmail_search_messages",
     "description": "Search the mailbox with Gmail's search syntax (e.g. 'from:ana newer_than:7d "
                    "has:attachment', 'is:unread label:work'). Returns headers and snippets, newest first.",
     "input_schema": obj({"query": {"type": "string", "description": "Gmail search query", "default": ""},
                          "max_results": {"type": "integer", "description": "1-50", "default": 10},
                          "label": {"type": "string", "description": "only this label (name or id), e.g. INBOX",
                                    "default": ""}}),
     "read_only": True, "destructive": False},
    {"name": "gmail_get_message",
     "description": "One message in full: headers, the text body and the attachments' names.",
     "input_schema": obj({"id": {"type": "string", "description": "message id"}}, ["id"]),
     "read_only": True, "destructive": False},
    {"name": "gmail_get_thread",
     "description": "Every message of a conversation, oldest first, with their text bodies.",
     "input_schema": obj({"id": {"type": "string", "description": "thread id"}}, ["id"]),
     "read_only": True, "destructive": False},
    {"name": "gmail_list_labels",
     "description": "The mailbox's labels (system ones like INBOX, UNREAD, STARRED and the user's own).",
     "input_schema": obj({}),
     "read_only": True, "destructive": False},
    {"name": "gmail_send_message",
     "description": "Send an email as the signed-in user. With reply_to_message_id it is a reply in that "
                    "message's thread (to its sender unless `to` is given, same subject unless given).",
     "input_schema": obj({"to": {"type": "string", "description": "comma-separated addresses", "default": ""},
                          "subject": {"type": "string", "default": ""},
                          "body": {"type": "string", "description": "plain text"},
                          "cc": {"type": "string", "default": ""},
                          "bcc": {"type": "string", "default": ""},
                          "reply_to_message_id": {"type": "string", "default": ""}}, ["body"]),
     "read_only": False, "destructive": False},
    {"name": "gmail_modify_labels",
     "description": "Add and/or remove labels on a message (names or ids). Remove INBOX to archive, "
                    "remove UNREAD to mark read, add STARRED to star.",
     "input_schema": obj({"id": {"type": "string", "description": "message id"},
                          "add": {"type": "array", "items": {"type": "string"}, "default": []},
                          "remove": {"type": "array", "items": {"type": "string"}, "default": []}}, ["id"]),
     "read_only": False, "destructive": False},
    {"name": "gmail_trash_message",
     "description": "Move a message to the trash (Gmail deletes it for good after 30 days).",
     "input_schema": obj({"id": {"type": "string", "description": "message id"}}, ["id"]),
     "read_only": False, "destructive": True},
]


# -- decoding -------------------------------------------------------------------

def _b64(data: str) -> bytes:
    data = (data or "").strip()
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _headers(payload: dict) -> dict:
    out = {}
    for h in payload.get("headers") or []:
        name = str(h.get("name") or "")
        if name.lower() in {x.lower() for x in _HEADERS}:
            out[name.lower()] = h.get("value") or ""
    return out


_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>|<br\s*/?>|</p>|</div>|</tr>|<[^>]+>", re.I | re.S)


def _strip_html(s: str) -> str:
    def sub(m):
        t = m.group(0).lower()
        return "\n" if t.startswith(("<br", "</p", "</div", "</tr")) else ""
    text = _html.unescape(_TAG.sub(sub, s))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _walk(part: dict, found: dict, attachments: list) -> None:
    mime = str(part.get("mimeType") or "")
    body = part.get("body") or {}
    filename = part.get("filename") or ""
    if filename and body.get("attachmentId"):
        attachments.append({"filename": filename, "mime_type": mime, "size": body.get("size"),
                            "attachment_id": body.get("attachmentId")})
    elif body.get("data") and mime in ("text/plain", "text/html") and mime not in found:
        try:
            found[mime] = _b64(body["data"]).decode("utf-8", "replace")
        except (ValueError, TypeError):
            pass
    for p in part.get("parts") or []:
        _walk(p, found, attachments)


def _body_text(payload: dict) -> tuple[str, list]:
    found: dict = {}
    attachments: list = []
    _walk(payload or {}, found, attachments)
    if found.get("text/plain", "").strip():
        return found["text/plain"].strip(), attachments
    if found.get("text/html"):
        return _strip_html(found["text/html"]), attachments
    return "", attachments


def _summary(m: dict) -> dict:
    h = _headers(m.get("payload") or {})
    return {"id": m.get("id"), "thread_id": m.get("threadId"), "from": h.get("from", ""),
            "to": h.get("to", ""), "subject": h.get("subject", ""), "date": h.get("date", ""),
            "snippet": _html.unescape(m.get("snippet") or ""), "labels": m.get("labelIds") or []}


def _full(m: dict, cap: int) -> dict:
    out = _summary(m)
    h = _headers(m.get("payload") or {})
    if h.get("cc"):
        out["cc"] = h["cc"]
    text, attachments = _body_text(m.get("payload") or {})
    out["body"] = text[:cap]
    out["truncated"] = len(text) > cap
    if attachments:
        out["attachments"] = attachments
    return out


# -- labels ---------------------------------------------------------------------

def _labels(ctx) -> list[dict]:
    body = api(ctx, "GET", API + "labels")
    return [{"id": x.get("id"), "name": x.get("name"), "type": x.get("type")}
            for x in body.get("labels") or []]


def _label_ids(ctx, names, labels=None) -> list[str]:
    if isinstance(names, str):
        names = [n for n in names.split(",")]
    wanted = [str(n).strip() for n in names or [] if str(n).strip()]
    if not wanted:
        return []
    labels = labels if labels is not None else _labels(ctx)
    by_id = {x["id"] for x in labels}
    by_name = {str(x["name"]).lower(): x["id"] for x in labels}
    out = []
    for n in wanted:
        if n in by_id:
            out.append(n)
        elif n.lower() in by_name:
            out.append(by_name[n.lower()])
        elif n.upper() in by_id:
            out.append(n.upper())
        else:
            raise ToolError(f"no label called {n!r}")
    return out


# -- tools ----------------------------------------------------------------------

def search_messages(ctx, query="", max_results=10, label="") -> dict:
    n = clamp(max_results, 1, 50, 10)
    params = {"q": query, "maxResults": n}
    if label:
        params["labelIds"] = _label_ids(ctx, [label])[0]
    body = api(ctx, "GET", API + "messages", params=params)
    out = []
    for ref in (body.get("messages") or [])[:n]:
        m = api(ctx, "GET", API + f"messages/{ref['id']}",
                params={"format": "metadata", "metadataHeaders": _HEADERS})
        out.append(_summary(m))
    return {"count": len(out), "estimated_total": body.get("resultSizeEstimate"),
            "messages": out}


def get_message(ctx, id: str) -> dict:
    return _full(api(ctx, "GET", API + f"messages/{id}", params={"format": "full"}), BODY_CAP)


def get_thread(ctx, id: str) -> dict:
    body = api(ctx, "GET", API + f"threads/{id}", params={"format": "full"})
    msgs = [_full(m, THREAD_BODY_CAP) for m in body.get("messages") or []]
    return {"thread_id": body.get("id"), "count": len(msgs), "messages": msgs}


def list_labels(ctx) -> dict:
    labels = _labels(ctx)
    return {"count": len(labels), "labels": labels}


def send_message(ctx, body: str, to="", subject="", cc="", bcc="", reply_to_message_id="") -> dict:
    msg = EmailMessage()
    thread_id = None
    if reply_to_message_id:
        orig = api(ctx, "GET", API + f"messages/{reply_to_message_id}",
                   params={"format": "metadata", "metadataHeaders": _HEADERS})
        h = _headers(orig.get("payload") or {})
        thread_id = orig.get("threadId")
        if not to:
            to = h.get("reply-to") or h.get("from") or ""
        if not subject:
            subject = h.get("subject", "")
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
        if h.get("message-id"):
            msg["In-Reply-To"] = h["message-id"]
            msg["References"] = f"{h['references']} {h['message-id']}".strip() if h.get("references") \
                else h["message-id"]
    if not to or not parseaddr(to.split(",")[0])[1]:
        raise ToolError("to is required (an email address)")
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    sent = api(ctx, "POST", API + "messages/send", json=payload)
    return {"ok": True, "id": sent.get("id"), "thread_id": sent.get("threadId"), "to": to,
            "subject": subject}


def modify_labels(ctx, id: str, add=None, remove=None) -> dict:
    labels = _labels(ctx)
    payload = {}
    add_ids, remove_ids = _label_ids(ctx, add, labels), _label_ids(ctx, remove, labels)
    if add_ids:
        payload["addLabelIds"] = add_ids
    if remove_ids:
        payload["removeLabelIds"] = remove_ids
    if not payload:
        raise ToolError("nothing to change: give add and/or remove")
    m = api(ctx, "POST", API + f"messages/{id}/modify", json=payload)
    return {"ok": True, "id": m.get("id") or id, "labels": m.get("labelIds") or []}


def trash_message(ctx, id: str) -> dict:
    m = api(ctx, "POST", API + f"messages/{id}/trash")
    return {"ok": True, "id": m.get("id") or id, "labels": m.get("labelIds") or []}


_HANDLERS = {
    "gmail_search_messages": search_messages,
    "gmail_get_message": get_message,
    "gmail_get_thread": get_thread,
    "gmail_list_labels": list_labels,
    "gmail_send_message": send_message,
    "gmail_modify_labels": modify_labels,
    "gmail_trash_message": trash_message,
}


def call(ctx, name: str, args: dict):
    return dispatch(TOOLS, _HANDLERS, ctx, name, args)
