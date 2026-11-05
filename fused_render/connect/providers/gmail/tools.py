"""Gmail's tools, native: the Gmail REST API called from this process (SPEC-connect-oauth §4, §11).

Google hosts no MCP server for Gmail, so the pack ships its tools here, the
Slack shape: the user token from the PKCE sign-in as Bearer over `ctx.http`,
loaded by path by the core `tools.py`; the token never reaches the caller.

**Send and labels only, on purpose.** Every Gmail scope that reads a mailbox
(`gmail.readonly`, `gmail.modify`, `gmail.metadata`, `mail.google.com`) is in
Google's restricted tier, which means a paid annual CASA security assessment
for the project that ships the client id. `gmail.send` and `gmail.labels` are
the sensitive tier: Google's free brand review and nothing more. So this card
composes and sends, and lists or creates label definitions; it cannot search,
read, label or trash messages (applying a label is `messages.modify`, a
restricted scope too). A deployment that wants the mailbox registers its own
Google client with `gmail.modify` and adds the read tools; nothing here stops
that, the scope string is one line in `provider.json`.

Google answers errors as `{error: {code, status, message}}`; `google_api.api`
turns a 401 into `ToolAuthError` (refreshed once by the core) and the rest
into `ToolError` with Google's own message.
"""
from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import parseaddr

from fused_render.connect.google_api import api, dispatch, obj
from fused_render.connect.tools import ToolError

API = "https://gmail.googleapis.com/gmail/v1/users/me/"

TOOLS = [
    {"name": "gmail_send_message",
     "description": "Send a plain-text email as the signed-in user. To reply in an existing thread, give "
                    "thread_id and the original's Message-ID header as in_reply_to (this card cannot read "
                    "mail, so the user supplies them).",
     "input_schema": obj({"to": {"type": "string", "description": "comma-separated addresses"},
                          "subject": {"type": "string", "default": ""},
                          "body": {"type": "string", "description": "plain text"},
                          "cc": {"type": "string", "default": ""},
                          "bcc": {"type": "string", "default": ""},
                          "thread_id": {"type": "string", "default": ""},
                          "in_reply_to": {"type": "string", "description": "the Message-ID being answered",
                                          "default": ""}},
                         ["to", "body"]),
     "read_only": False, "destructive": False},
    {"name": "gmail_list_labels",
     "description": "The mailbox's labels (system ones like INBOX, UNREAD, STARRED and the user's own).",
     "input_schema": obj({}),
     "read_only": True, "destructive": False},
    {"name": "gmail_create_label",
     "description": "Create a label (nest with '/', e.g. 'Clients/Acme').",
     "input_schema": obj({"name": {"type": "string"}}, ["name"]),
     "read_only": False, "destructive": False},
]


def send_message(ctx, to: str, body: str, subject="", cc="", bcc="", thread_id="", in_reply_to="") -> dict:
    if "@" not in parseaddr((to or "").split(",")[0])[1]:
        raise ToolError("to is required (an email address)")
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg["Subject"] = subject
    if in_reply_to:
        ref = in_reply_to if in_reply_to.startswith("<") else f"<{in_reply_to}>"
        msg["In-Reply-To"] = ref
        msg["References"] = ref
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii").rstrip("=")
    payload = {"raw": raw}
    if thread_id:
        payload["threadId"] = thread_id
    sent = api(ctx, "POST", API + "messages/send", json=payload)
    return {"ok": True, "id": sent.get("id"), "thread_id": sent.get("threadId"), "to": to, "subject": subject}


def list_labels(ctx) -> dict:
    body = api(ctx, "GET", API + "labels")
    labels = [{"id": x.get("id"), "name": x.get("name"), "type": x.get("type")}
              for x in body.get("labels") or []]
    return {"count": len(labels), "labels": labels}


def create_label(ctx, name: str) -> dict:
    made = api(ctx, "POST", API + "labels",
               json={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})
    return {"ok": True, "id": made.get("id"), "name": made.get("name") or name}


_HANDLERS = {
    "gmail_send_message": send_message,
    "gmail_list_labels": list_labels,
    "gmail_create_label": create_label,
}


def call(ctx, name: str, args: dict):
    return dispatch(TOOLS, _HANDLERS, ctx, name, args)
