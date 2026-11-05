"""The three Google packs' native tools (gmail, google-calendar, google-docs) against
mocked Google REST APIs, plus the shared `google_api` error mapping.

Each pack is copied into a temp providers folder with an api-key sign-in (the tools
only need a token in the store), and every HTTP request goes to an
`httpx.MockTransport`: nothing leaves the process.
"""
import base64
import json
import os
import shutil

import httpx
import pytest

from fused_render.connect import google_api, manager, store, tools
from fused_render.connect.tools import ToolAuthError, ToolError

PACKS = os.path.join(os.path.dirname(tools.__file__), "providers")


def _pack(root, key, name):
    d = root / key
    d.mkdir(parents=True)
    (d / "provider.json").write_text(json.dumps({
        "name": name, "tools": {"remote": None}, "privacy": "test",
        "auth": [{"kind": "api-key", "label": "Token",
                  "identity_url": "https://www.googleapis.com/oauth2/v3/userinfo"}]}))
    shutil.copy(os.path.join(PACKS, key, "tools.py"), d / "tools.py")


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("FUSED_RENDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FUSED_RENDER_CONNECT_STORE", "file")
    root = tmp_path / "packs"
    monkeypatch.setenv("FUSED_RENDER_CONNECT_PROVIDERS", str(root))
    for key, name in (("gmail", "Gmail"), ("google-calendar", "Google Calendar"), ("google-docs", "Google Docs")):
        _pack(root, key, name)
        store.put(key, {"kind": "api-key", "access": "ya29.x"})
    for d in (tools._cache, tools._clients, tools._native, manager._flows):
        d.clear()
    manager._reconnect.clear()
    yield
    for d in (tools._cache, tools._clients, tools._native, manager._flows):
        d.clear()


def _mock(monkeypatch, handler):
    monkeypatch.setattr(tools, "_http", lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def _call(key, name, args):
    out = tools.call_tool(key, name, args)
    return out, (json.loads(out["text"]) if out["ok"] else out["text"])


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


# -- google_api ------------------------------------------------------------------

class _Ctx:
    def __init__(self, handler):
        self.token = "tok"
        self.http = httpx.Client(transport=httpx.MockTransport(handler))
        self.record = {}


def _google_error(status_code, status, message):
    return httpx.Response(status_code, json={"error": {"code": status_code, "status": status, "message": message}})


def test_google_api_maps_googles_error_envelope():
    seen = []

    def handler(request):
        seen.append(request)
        path = request.url.path
        if path == "/ok":
            return httpx.Response(200, json={"a": 1})
        if path == "/empty":
            return httpx.Response(204)
        if path == "/401":
            return _google_error(401, "UNAUTHENTICATED", "Invalid Credentials")
        if path == "/403":
            return _google_error(403, "PERMISSION_DENIED", "Request had insufficient authentication scopes.")
        if path == "/429":
            return _google_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded")
        if path == "/404":
            return _google_error(404, "NOT_FOUND", "Requested entity was not found.")
        return httpx.Response(500, text="boom")
    ctx = _Ctx(handler)
    assert google_api.api(ctx, "GET", "https://g.example/ok", params={"a": "", "b": None, "c": 1}) == {"a": 1}
    assert seen[-1].url.params == httpx.QueryParams({"c": "1"})
    assert seen[-1].headers["authorization"] == "Bearer tok"
    assert google_api.api(ctx, "DELETE", "https://g.example/empty") == {}
    with pytest.raises(ToolAuthError, match="Invalid Credentials"):
        google_api.api(ctx, "GET", "https://g.example/401")
    with pytest.raises(ToolError, match="permission denied .*scopes"):
        google_api.api(ctx, "GET", "https://g.example/403")
    with pytest.raises(ToolError, match="ratelimited"):
        google_api.api(ctx, "GET", "https://g.example/429")
    with pytest.raises(ToolError, match="not found"):
        google_api.api(ctx, "GET", "https://g.example/404")
    with pytest.raises(ToolError, match="HTTP 500"):
        google_api.api(ctx, "GET", "https://g.example/500")


def test_dispatch_checks_names_and_required_arguments():
    spec = [{"name": "t", "input_schema": {"type": "object", "properties": {"a": {}, "b": {}}, "required": ["a"]}}]
    handlers = {"t": lambda ctx, a, b=None: (a, b)}
    assert google_api.dispatch(spec, handlers, None, "t", {"a": 1, "b": 2, "zzz": 3}) == (1, 2)
    with pytest.raises(ToolError, match="missing argument: a"):
        google_api.dispatch(spec, handlers, None, "t", {"b": 2})
    with pytest.raises(ToolError, match="unknown tool"):
        google_api.dispatch(spec, handlers, None, "nope", {})


# -- gmail ---------------------------------------------------------------------------

class Gmail:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        path = request.url.path.replace("/gmail/v1/users/me/", "")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params.multi_items()), body))
        if path == "labels" and request.method == "GET":
            return httpx.Response(200, json={"labels": [
                {"id": "INBOX", "name": "INBOX", "type": "system"},
                {"id": "Label_7", "name": "Work", "type": "user"}]})
        if path == "labels" and request.method == "POST":
            return httpx.Response(200, json={"id": "Label_8", "name": body["name"], "type": "user"})
        if path == "messages/send":
            return httpx.Response(200, json={"id": "s1", "threadId": body.get("threadId") or "tnew"})
        if path == "messages":
            return _google_error(403, "PERMISSION_DENIED", "Request had insufficient authentication scopes.")
        return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND", "message": path}})


@pytest.fixture()
def gmail(monkeypatch):
    srv = Gmail()
    _mock(monkeypatch, srv)
    return srv


def test_gmail_is_send_and_labels_only(gmail):
    """No tool reads mail: every mailbox-reading Gmail scope is Google's restricted (CASA) tier."""
    by = {s.name: s for s in tools.list_tools("gmail")}
    assert set(by) == {"gmail_send_message", "gmail_list_labels", "gmail_create_label"}
    assert by["gmail_list_labels"].read_only is True
    assert by["gmail_send_message"].read_only is False and by["gmail_send_message"].destructive is False
    assert not any(n for n in by if "search" in n or "get" in n or "trash" in n or "modify" in n)


def test_gmail_send_and_reply_by_supplied_ids(gmail):
    out, data = _call("gmail", "gmail_send_message", {"to": "bo@x.io", "subject": "Hey", "body": "yo", "cc": "c@x.io"})
    assert out["ok"] and data == {"ok": True, "id": "s1", "thread_id": "tnew", "to": "bo@x.io", "subject": "Hey"}
    sent = [c for c in gmail.calls if c[1] == "messages/send"][0][3]
    raw = base64.urlsafe_b64decode(sent["raw"] + "=" * (-len(sent["raw"]) % 4)).decode()
    assert "To: bo@x.io" in raw and "Cc: c@x.io" in raw and "Subject: Hey" in raw and raw.rstrip().endswith("yo")
    assert "threadId" not in sent and "In-Reply-To" not in raw
    # A reply: the user supplies the thread and the Message-ID, since the card cannot read them.
    _, data = _call("gmail", "gmail_send_message", {"to": "ana@x.io", "body": "ok", "subject": "Re: Budget",
                                                    "thread_id": "t1", "in_reply_to": "abc@x.io"})
    assert data["thread_id"] == "t1"
    sent = [c for c in gmail.calls if c[1] == "messages/send"][-1][3]
    raw = base64.urlsafe_b64decode(sent["raw"] + "=" * (-len(sent["raw"]) % 4)).decode()
    assert sent["threadId"] == "t1" and "In-Reply-To: <abc@x.io>" in raw and "References: <abc@x.io>" in raw
    out, text = _call("gmail", "gmail_send_message", {"to": "not an address", "body": "x"})
    assert not out["ok"] and text == "gmail: to is required (an email address)"
    out, text = _call("gmail", "gmail_send_message", {"to": "a@b.c"})
    assert text == "gmail: missing argument: body"


def test_gmail_labels(gmail):
    _, data = _call("gmail", "gmail_list_labels", {})
    assert [x["name"] for x in data["labels"]] == ["INBOX", "Work"] and data["count"] == 2
    _, data = _call("gmail", "gmail_create_label", {"name": "Clients/Acme"})
    assert data == {"ok": True, "id": "Label_8", "name": "Clients/Acme"}
    assert gmail.calls[-1][3]["name"] == "Clients/Acme"
    out, text = _call("gmail", "gmail_create_label", {})
    assert text == "gmail: missing argument: name"


def test_a_refused_google_token_is_refreshed_once_then_reconnect(monkeypatch):
    def handler(request):
        return _google_error(401, "UNAUTHENTICATED", "Invalid Credentials")
    _mock(monkeypatch, handler)
    refreshed = []
    monkeypatch.setattr(manager, "token", lambda key, force_refresh=False: refreshed.append(force_refresh) or "ya29.x")
    out = tools.call_tool("gmail", "gmail_list_labels", {})
    assert out["ok"] is False and out["error"] == "reconnect"
    assert refreshed == [False, True, False]
    assert manager.needs_reconnect("gmail")


# -- google calendar -------------------------------------------------------------------

class Calendar:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        path = request.url.path.replace("/calendar/v3/", "")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params.multi_items()), body))
        if path == "users/me/calendarList":
            return httpx.Response(200, json={"items": [
                {"id": "me@x.io", "summary": "me@x.io", "primary": True, "accessRole": "owner", "timeZone": "Europe/Paris"},
                {"id": "team@group", "summary": "Team", "summaryOverride": "Our team", "accessRole": "reader"}]})
        if path == "calendars/primary/events" and request.method == "GET":
            return httpx.Response(200, json={"timeZone": "Europe/Paris", "nextPageToken": "p2", "items": [
                {"id": "e1", "summary": "Standup", "status": "confirmed", "htmlLink": "https://cal/e1",
                 "start": {"dateTime": "2026-11-05T09:00:00+01:00"}, "end": {"dateTime": "2026-11-05T09:15:00+01:00"},
                 "organizer": {"email": "me@x.io"}, "attendees": [{"email": "bo@x.io", "responseStatus": "accepted"}],
                 "description": "long", "recurringEventId": "r1"},
                {"id": "e2", "summary": "Offsite", "start": {"date": "2026-11-06"}, "end": {"date": "2026-11-07"},
                 "location": "Lyon"}]})
        if path == "calendars/primary/events" and request.method == "POST":
            return httpx.Response(200, json={"id": "new", "summary": body["summary"], "start": body["start"],
                                             "end": body["end"], "htmlLink": "https://cal/new", "status": "confirmed"})
        if path == "calendars/primary/events/e1" and request.method == "GET":
            return httpx.Response(200, json={"id": "e1", "summary": "Standup", "description": "long",
                                             "start": {"dateTime": "2026-11-05T09:00:00+01:00"},
                                             "end": {"dateTime": "2026-11-05T09:15:00+01:00"},
                                             "attendees": [{"email": "bo@x.io", "responseStatus": "accepted"}],
                                             "hangoutLink": "https://meet/x", "recurringEventId": "r1"})
        if path == "calendars/team@group/events/e9" and request.method == "PATCH":
            return httpx.Response(200, json={"id": "e9", "summary": body.get("summary", "old"), "start": {"date": "2026-12-01"},
                                             "end": {"date": "2026-12-02"}})
        if path == "calendars/primary/events/e1" and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND", "message": path}})


@pytest.fixture()
def calendar(monkeypatch):
    srv = Calendar()
    _mock(monkeypatch, srv)
    return srv


def test_calendar_tools_listed_with_flags(calendar):
    by = {s.name: s for s in tools.list_tools("google-calendar")}
    assert set(by) == {"calendar_list_calendars", "calendar_list_events", "calendar_get_event",
                       "calendar_create_event", "calendar_update_event", "calendar_delete_event"}
    assert by["calendar_list_events"].read_only is True
    assert by["calendar_create_event"].destructive is False and by["calendar_delete_event"].destructive is True


def test_calendar_list_and_get(calendar):
    _, cals = _call("google-calendar", "calendar_list_calendars", {})
    assert cals["calendars"] == [
        {"id": "me@x.io", "name": "me@x.io", "primary": True, "access_role": "owner", "time_zone": "Europe/Paris"},
        {"id": "team@group", "name": "Our team", "primary": False, "access_role": "reader", "time_zone": None}]
    _, ev = _call("google-calendar", "calendar_list_events", {"time_min": "2026-11-05", "time_max": "2026-11-08", "query": "x"})
    params = calendar.calls[-1][2]
    assert params["timeMin"] == "2026-11-05T00:00:00Z" and params["timeMax"] == "2026-11-08T00:00:00Z"
    assert params["singleEvents"] == "true" and params["orderBy"] == "startTime" and params["q"] == "x"
    assert ev["has_more"] is True and ev["time_zone"] == "Europe/Paris"
    assert ev["events"][0] == {"id": "e1", "summary": "Standup", "start": "2026-11-05T09:00:00+01:00",
                               "end": "2026-11-05T09:15:00+01:00", "all_day": False, "status": "confirmed",
                               "link": "https://cal/e1", "organizer": "me@x.io", "attendees": ["bo@x.io"]}
    assert ev["events"][1]["all_day"] is True and ev["events"][1]["location"] == "Lyon"
    assert "description" not in ev["events"][0]      # the listing stays short
    # No window at all: from now on (an RFC 3339 UTC stamp), not the whole history.
    _call("google-calendar", "calendar_list_events", {})
    assert calendar.calls[-1][2]["timeMin"].endswith("Z") and "timeMax" not in calendar.calls[-1][2]
    _, one = _call("google-calendar", "calendar_get_event", {"event_id": "e1"})
    assert one["description"] == "long" and one["meet_link"] == "https://meet/x"
    assert one["attendees"] == [{"email": "bo@x.io", "response": "accepted"}] and one["recurring_event_id"] == "r1"


def test_calendar_create_update_delete(calendar):
    out, data = _call("google-calendar", "calendar_create_event", {
        "summary": "Lunch", "start": "2026-11-05T12:00:00", "end": "2026-11-05T13:00:00", "time_zone": "Europe/Paris",
        "attendees": ["bo@x.io", " cy@x.io "], "location": "Café"})
    assert out["ok"] and data["id"] == "new" and data["summary"] == "Lunch"
    m, path, params, body = calendar.calls[-1]
    assert (m, path, params) == ("POST", "calendars/primary/events", {"sendUpdates": "all"})
    assert body == {"summary": "Lunch", "start": {"dateTime": "2026-11-05T12:00:00", "timeZone": "Europe/Paris"},
                    "end": {"dateTime": "2026-11-05T13:00:00", "timeZone": "Europe/Paris"}, "location": "Café",
                    "attendees": [{"email": "bo@x.io"}, {"email": "cy@x.io"}]}
    _, data = _call("google-calendar", "calendar_create_event", {"summary": "Day", "start": "2026-11-06", "end": "2026-11-07"})
    assert calendar.calls[-1][3]["start"] == {"date": "2026-11-06"} and calendar.calls[-1][2] == {"sendUpdates": "none"}
    out, text = _call("google-calendar", "calendar_create_event", {"summary": "Bad", "start": "2026-11-06", "end": "2026-11-07T10:00:00Z"})
    assert text == "google-calendar: start and end must both be dates (all-day) or both be times"
    out, text = _call("google-calendar", "calendar_create_event", {"summary": "Bad", "start": "tomorrow", "end": "later"})
    assert text.startswith("google-calendar: not a date or RFC 3339 time: 'tomorrow'")
    _, data = _call("google-calendar", "calendar_update_event", {"event_id": "e9", "calendar": "team@group", "summary": "Renamed"})
    assert data["summary"] == "Renamed" and calendar.calls[-1][:2] == ("PATCH", "calendars/team@group/events/e9")
    assert calendar.calls[-1][3] == {"summary": "Renamed"}
    out, text = _call("google-calendar", "calendar_update_event", {"event_id": "e9"})
    assert text == "google-calendar: nothing to change"
    _, data = _call("google-calendar", "calendar_delete_event", {"event_id": "e1"})
    assert data == {"ok": True, "id": "e1", "calendar": "primary"}
    assert calendar.calls[-1][0] == "DELETE"


# -- google docs ---------------------------------------------------------------------

DOC = {"documentId": "d" * 20, "title": "Plan", "body": {"content": [
    {"endIndex": 1, "sectionBreak": {}},
    {"startIndex": 1, "endIndex": 7, "paragraph": {"elements": [{"textRun": {"content": "Hello "}},
                                                                {"textRun": {"content": "\n"}}]}},
    {"startIndex": 7, "endIndex": 20, "table": {"tableRows": [{"tableCells": [
        {"content": [{"paragraph": {"elements": [{"textRun": {"content": "a\n"}}]}}]},
        {"content": [{"paragraph": {"elements": [{"textRun": {"content": "b\n"}}]}}]}]}]}},
    {"startIndex": 20, "endIndex": 25, "paragraph": {"elements": [{"textRun": {"content": "End\n"}}]}}]}}


class Docs:
    def __init__(self):
        self.calls = []

    def __call__(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, dict(request.url.params.multi_items()), body))
        if path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {"id": "d" * 20, "name": "Plan", "modifiedTime": "2026-11-01T00:00:00Z",
                 "webViewLink": "https://docs.google.com/document/d/" + "d" * 20 + "/edit?usp=drivesdk",
                 "owners": [{"emailAddress": "me@x.io"}]}]})
        if path == "/v1/documents/" + "d" * 20 and request.method == "GET":
            return httpx.Response(200, json=DOC)
        if path == "/v1/documents" and request.method == "POST":
            return httpx.Response(200, json={"documentId": "n" * 20, "title": body["title"]})
        if path.endswith(":batchUpdate"):
            req = body["requests"][0]
            if "replaceAllText" in req:
                return httpx.Response(200, json={"replies": [{"replaceAllText": {"occurrencesChanged": 3}}]})
            return httpx.Response(200, json={"replies": [{}]})
        return httpx.Response(404, json={"error": {"code": 404, "status": "NOT_FOUND", "message": path}})


@pytest.fixture()
def docs(monkeypatch):
    srv = Docs()
    _mock(monkeypatch, srv)
    return srv


def test_docs_tools_listed_with_flags(docs):
    by = {s.name: s for s in tools.list_tools("google-docs")}
    assert set(by) == {"docs_search", "docs_get", "docs_create", "docs_append_text", "docs_replace_text"}
    assert by["docs_search"].read_only and by["docs_get"].read_only
    assert not by["docs_replace_text"].read_only and by["docs_replace_text"].destructive is False


def test_docs_search_and_get(docs):
    _, data = _call("google-docs", "docs_search", {"query": "Q3 o'plan", "max_results": 5})
    params = docs.calls[-1][2]
    assert params["q"] == "mimeType = 'application/vnd.google-apps.document' and trashed = false and name contains 'Q3 o\\'plan'"
    assert params["pageSize"] == "5" and params["orderBy"] == "modifiedTime desc"
    assert data["documents"] == [{"id": "d" * 20, "title": "Plan", "modified": "2026-11-01T00:00:00Z",
                                  "link": "https://docs.google.com/document/d/" + "d" * 20 + "/edit?usp=drivesdk",
                                  "owner": "me@x.io"}]
    _call("google-docs", "docs_search", {})
    assert "name contains" not in docs.calls[-1][2]["q"]
    # The doc's URL works in place of its id; tables come out as tab-separated rows.
    _, doc = _call("google-docs", "docs_get", {"document_id": "https://docs.google.com/document/d/" + "d" * 20 + "/edit#x"})
    assert doc["title"] == "Plan" and doc["text"] == "Hello \na\tb\nEnd" and doc["truncated"] is False
    assert doc["link"] == "https://docs.google.com/document/d/" + "d" * 20 + "/edit"
    out, text = _call("google-docs", "docs_get", {"document_id": "nope!"})
    assert text == "google-docs: not a document id or URL: 'nope!'"


def test_docs_create_append_replace(docs):
    _, data = _call("google-docs", "docs_create", {"title": "Notes", "text": "First line\n"})
    assert data == {"ok": True, "id": "n" * 20, "title": "Notes", "link": "https://docs.google.com/document/d/" + "n" * 20 + "/edit"}
    assert docs.calls[-2][1:] == ("/v1/documents", {}, {"title": "Notes"})
    assert docs.calls[-1][3] == {"requests": [{"insertText": {"location": {"index": 1}, "text": "First line\n"}}]}
    _call("google-docs", "docs_create", {"title": "Empty"})
    assert docs.calls[-1][1] == "/v1/documents"       # no batchUpdate without text
    _, data = _call("google-docs", "docs_append_text", {"document_id": "d" * 20, "text": "\nMore"})
    assert data["inserted_at"] == 24      # endIndex 25, before the body's final newline
    assert docs.calls[-1][3] == {"requests": [{"insertText": {"location": {"index": 24}, "text": "\nMore"}}]}
    _, data = _call("google-docs", "docs_replace_text", {"document_id": "d" * 20, "find": "Hello", "replace": "Bye", "match_case": False})
    assert data["replaced"] == 3
    assert docs.calls[-1][3] == {"requests": [{"replaceAllText": {"containsText": {"text": "Hello", "matchCase": False},
                                                                   "replaceText": "Bye"}}]}
    out, text = _call("google-docs", "docs_replace_text", {"document_id": "d" * 20})
    assert text == "google-docs: missing argument: find"
