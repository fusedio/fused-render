"""Google Docs' tools, native: the Docs and Drive REST APIs from this process
(SPEC-connect-oauth §4, §11), the Gmail pack's shape.

A document comes back as text, not as the Docs API's structural tree: the
body's paragraphs joined, tables cell by cell, capped. That is what a model
reads, quotes and summarises; styling is not in scope here. Edits are the two
that need no structural knowledge: append at the end, and replace every
occurrence of a string. Any tool takes the document's URL in place of its id.

Scopes, on purpose: `documents` (any doc the user can open, by id or URL) and
`drive.file`, which lists only the docs made through Fused or opened through
it. Searching the user's whole Drive by title needs `drive.metadata.readonly`,
which Google puts in its restricted tier (the paid annual CASA assessment);
both scopes here are the sensitive tier, Google's free brand review only. So
`docs_search` is "the docs Fused made", and a bot asks for a link to anything
else.
"""
from __future__ import annotations

import re

from fused_render.connect.google_api import api, clamp, dispatch, obj
from fused_render.connect.tools import ToolError

DOCS = "https://docs.googleapis.com/v1/documents"
DRIVE = "https://www.googleapis.com/drive/v3/files"
TEXT_CAP = 60_000
_DOC_MIME = "application/vnd.google-apps.document"
_URL_ID = re.compile(r"/d/([A-Za-z0-9_-]{10,})")

TOOLS = [
    {"name": "docs_search",
     "description": "Find, by name, the Google Docs created through Fused (most recently changed first); "
                    "empty query lists them all. Any other doc is reached by its URL or id with docs_get.",
     "input_schema": obj({"query": {"type": "string", "description": "words of the title", "default": ""},
                          "max_results": {"type": "integer", "description": "1-100", "default": 20}}),
     "read_only": True, "destructive": False},
    {"name": "docs_get",
     "description": "A document's title and text (tables as tab-separated rows), from its URL or id.",
     "input_schema": obj({"document_id": {"type": "string", "description": "the id, or the document's URL"}},
                         ["document_id"]),
     "read_only": True, "destructive": False},
    {"name": "docs_create",
     "description": "Create a new Google Doc with a title and optional starting text.",
     "input_schema": obj({"title": {"type": "string"}, "text": {"type": "string", "default": ""}}, ["title"]),
     "read_only": False, "destructive": False},
    {"name": "docs_append_text",
     "description": "Add text at the end of a document.",
     "input_schema": obj({"document_id": {"type": "string"}, "text": {"type": "string"}},
                         ["document_id", "text"]),
     "read_only": False, "destructive": False},
    {"name": "docs_replace_text",
     "description": "Replace every occurrence of a string in a document.",
     "input_schema": obj({"document_id": {"type": "string"}, "find": {"type": "string"},
                          "replace": {"type": "string", "default": ""},
                          "match_case": {"type": "boolean", "default": True}},
                         ["document_id", "find"]),
     "read_only": False, "destructive": False},
]


def _id(document_id: str) -> str:
    v = (document_id or "").strip()
    m = _URL_ID.search(v)
    if m:
        return m.group(1)
    if not re.match(r"^[A-Za-z0-9_-]{10,}$", v):
        raise ToolError(f"not a document id or URL: {document_id!r}")
    return v


def _link(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


# -- text extraction -------------------------------------------------------------

def _para_text(p: dict) -> str:
    return "".join((el.get("textRun") or {}).get("content") or "" for el in p.get("elements") or [])


def _elements_text(elements: list) -> str:
    out = []
    for el in elements or []:
        if "paragraph" in el:
            out.append(_para_text(el["paragraph"]))
        elif "table" in el:
            for row in el["table"].get("tableRows") or []:
                cells = [_elements_text(c.get("content") or []).strip().replace("\n", " ")
                         for c in row.get("tableCells") or []]
                out.append("\t".join(cells) + "\n")
        elif "tableOfContents" in el:
            out.append(_elements_text(el["tableOfContents"].get("content") or []))
    return "".join(out)


def _end_index(doc: dict) -> int:
    content = (doc.get("body") or {}).get("content") or []
    return int(content[-1].get("endIndex") or 1) if content else 1


# -- tools ---------------------------------------------------------------------

def search(ctx, query="", max_results=20) -> dict:
    n = clamp(max_results, 1, 100, 20)
    q = f"mimeType = '{_DOC_MIME}' and trashed = false"
    words = (query or "").strip()
    if words:
        q += " and name contains '" + words.replace("\\", "\\\\").replace("'", "\\'") + "'"
    body = api(ctx, "GET", DRIVE, params={"q": q, "pageSize": n, "orderBy": "modifiedTime desc",
                                           "fields": "files(id,name,modifiedTime,webViewLink,owners(emailAddress))"})
    docs = [{"id": f.get("id"), "title": f.get("name"), "modified": f.get("modifiedTime"),
             "link": f.get("webViewLink") or _link(f.get("id") or ""),
             "owner": ((f.get("owners") or [{}])[0]).get("emailAddress")} for f in body.get("files") or []]
    return {"count": len(docs), "documents": docs}


def get(ctx, document_id: str) -> dict:
    doc_id = _id(document_id)
    doc = api(ctx, "GET", f"{DOCS}/{doc_id}")
    text = _elements_text((doc.get("body") or {}).get("content") or []).strip()
    return {"id": doc_id, "title": doc.get("title") or "", "link": _link(doc_id),
            "text": text[:TEXT_CAP], "truncated": len(text) > TEXT_CAP}


def create(ctx, title: str, text="") -> dict:
    doc = api(ctx, "POST", DOCS, json={"title": title})
    doc_id = doc.get("documentId") or ""
    if text:
        api(ctx, "POST", f"{DOCS}/{doc_id}:batchUpdate",
            json={"requests": [{"insertText": {"location": {"index": 1}, "text": text}}]})
    return {"ok": True, "id": doc_id, "title": doc.get("title") or title, "link": _link(doc_id)}


def append_text(ctx, document_id: str, text: str) -> dict:
    doc_id = _id(document_id)
    doc = api(ctx, "GET", f"{DOCS}/{doc_id}", params={"fields": "body(content(endIndex))"})
    # The body always ends with a newline the API refuses to write past: insert before it.
    index = max(1, _end_index(doc) - 1)
    api(ctx, "POST", f"{DOCS}/{doc_id}:batchUpdate",
        json={"requests": [{"insertText": {"location": {"index": index}, "text": text}}]})
    return {"ok": True, "id": doc_id, "link": _link(doc_id), "inserted_at": index}


def replace_text(ctx, document_id: str, find: str, replace="", match_case=True) -> dict:
    doc_id = _id(document_id)
    body = api(ctx, "POST", f"{DOCS}/{doc_id}:batchUpdate",
               json={"requests": [{"replaceAllText": {
                   "containsText": {"text": find, "matchCase": bool(match_case)},
                   "replaceText": replace or ""}}]})
    replies = body.get("replies") or [{}]
    changed = (replies[0].get("replaceAllText") or {}).get("occurrencesChanged", 0)
    return {"ok": True, "id": doc_id, "link": _link(doc_id), "replaced": int(changed or 0)}


_HANDLERS = {
    "docs_search": search,
    "docs_get": get,
    "docs_create": create,
    "docs_append_text": append_text,
    "docs_replace_text": replace_text,
}


def call(ctx, name: str, args: dict):
    return dispatch(TOOLS, _HANDLERS, ctx, name, args)
