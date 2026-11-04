"""Google Calendar's tools, native: the Calendar REST API from this process
(SPEC-connect-oauth §4, §11), the Gmail pack's shape.

Times go through as the model writes them: an RFC 3339 stamp
(`2026-11-05T14:00:00+01:00`, or `...Z`) is a timed event, a bare date
(`2026-11-05`) an all-day one, and a stamp without an offset is read in the
calendar's own time zone (`time_zone` names one explicitly). Results keep
what a model needs to answer or act: id, title, start, end, where, who,
status and the link; recurring events arrive as their single instances
ordered by start, which is what "what's on Tuesday" means.
"""
from __future__ import annotations

import datetime as _dt
import re
from urllib.parse import quote

from fused_render.connect.google_api import api, clamp, dispatch, obj
from fused_render.connect.tools import ToolError

API = "https://www.googleapis.com/calendar/v3/"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

TOOLS = [
    {"name": "calendar_list_calendars",
     "description": "The calendars on the signed-in user's list (id, name, which is primary).",
     "input_schema": obj({}),
     "read_only": True, "destructive": False},
    {"name": "calendar_list_events",
     "description": "Events in a time window (default: from now on), ordered by start; recurring events "
                    "as their instances. `query` matches title, description, location and attendees.",
     "input_schema": obj({"calendar": {"type": "string", "description": "calendar id; 'primary' is the user's own",
                                       "default": "primary"},
                          "time_min": {"type": "string", "description": "RFC 3339 or YYYY-MM-DD (inclusive)",
                                       "default": ""},
                          "time_max": {"type": "string", "description": "RFC 3339 or YYYY-MM-DD (exclusive)",
                                       "default": ""},
                          "query": {"type": "string", "default": ""},
                          "max_results": {"type": "integer", "description": "1-250", "default": 25}}),
     "read_only": True, "destructive": False},
    {"name": "calendar_get_event",
     "description": "One event in full, with its description and attendees' responses.",
     "input_schema": obj({"event_id": {"type": "string"},
                          "calendar": {"type": "string", "default": "primary"}}, ["event_id"]),
     "read_only": True, "destructive": False},
    {"name": "calendar_create_event",
     "description": "Create an event. `start`/`end` are RFC 3339 stamps, or YYYY-MM-DD for an all-day event "
                    "(end is the day after the last day). Attendees get Google's invitation email.",
     "input_schema": obj({"summary": {"type": "string", "description": "title"},
                          "start": {"type": "string"}, "end": {"type": "string"},
                          "calendar": {"type": "string", "default": "primary"},
                          "description": {"type": "string", "default": ""},
                          "location": {"type": "string", "default": ""},
                          "attendees": {"type": "array", "items": {"type": "string"},
                                        "description": "email addresses", "default": []},
                          "time_zone": {"type": "string", "description": "IANA zone for stamps without an offset",
                                        "default": ""}},
                         ["summary", "start", "end"]),
     "read_only": False, "destructive": False},
    {"name": "calendar_update_event",
     "description": "Change an event's title, times, description, location or attendees; "
                    "fields left out keep their value.",
     "input_schema": obj({"event_id": {"type": "string"},
                          "calendar": {"type": "string", "default": "primary"},
                          "summary": {"type": "string", "default": ""},
                          "start": {"type": "string", "default": ""}, "end": {"type": "string", "default": ""},
                          "description": {"type": "string", "default": ""},
                          "location": {"type": "string", "default": ""},
                          "attendees": {"type": "array", "items": {"type": "string"}, "default": []},
                          "time_zone": {"type": "string", "default": ""}},
                         ["event_id"]),
     "read_only": False, "destructive": False},
    {"name": "calendar_delete_event",
     "description": "Delete an event from a calendar (attendees are told it was cancelled).",
     "input_schema": obj({"event_id": {"type": "string"},
                          "calendar": {"type": "string", "default": "primary"}}, ["event_id"]),
     "read_only": False, "destructive": True},
]


def _when(value: str, time_zone: str = "") -> dict:
    v = (value or "").strip()
    if _DATE.match(v):
        return {"date": v}
    try:
        _dt.datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        raise ToolError(f"not a date or RFC 3339 time: {value!r}") from None
    out = {"dateTime": v}
    if time_zone:
        out["timeZone"] = time_zone
    return out


def _bound(value: str) -> str:
    """A list bound as RFC 3339: a bare date is midnight UTC of that day."""
    v = (value or "").strip()
    if _DATE.match(v):
        return v + "T00:00:00Z"
    _when(v)
    return v


def _side(x: dict | None) -> str:
    x = x or {}
    return x.get("dateTime") or x.get("date") or ""


def _event(e: dict, full: bool = False) -> dict:
    out = {"id": e.get("id"), "summary": e.get("summary") or "", "start": _side(e.get("start")),
           "end": _side(e.get("end")), "all_day": "date" in (e.get("start") or {}),
           "status": e.get("status"), "link": e.get("htmlLink")}
    if e.get("location"):
        out["location"] = e["location"]
    org = e.get("organizer") or {}
    if org.get("email"):
        out["organizer"] = org["email"]
    att = e.get("attendees") or []
    if att:
        out["attendees"] = [{"email": a.get("email"), "response": a.get("responseStatus")}
                            if full else a.get("email") for a in att]
    if full:
        out["description"] = e.get("description") or ""
        if e.get("recurringEventId"):
            out["recurring_event_id"] = e["recurringEventId"]
        if e.get("hangoutLink"):
            out["meet_link"] = e["hangoutLink"]
    return out


def _cal(calendar: str) -> str:
    return (calendar or "").strip() or "primary"


def _path(calendar: str, event_id: str | None = None) -> str:
    """`calendars/{id}/events[/{event}]` with the ids escaped (a calendar id is an email)."""
    out = API + "calendars/" + quote(_cal(calendar), safe="") + "/events"
    if event_id:
        out += "/" + quote(str(event_id), safe="")
    return out


def list_calendars(ctx) -> dict:
    body = api(ctx, "GET", API + "users/me/calendarList", params={"minAccessRole": "reader"})
    cals = [{"id": c.get("id"), "name": c.get("summaryOverride") or c.get("summary"),
             "primary": bool(c.get("primary")), "access_role": c.get("accessRole"),
             "time_zone": c.get("timeZone")} for c in body.get("items") or []]
    return {"count": len(cals), "calendars": cals}


def list_events(ctx, calendar="primary", time_min="", time_max="", query="", max_results=25) -> dict:
    n = clamp(max_results, 1, 250, 25)
    if not time_min and not time_max:
        time_min = _dt.datetime.now(_dt.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"singleEvents": "true", "orderBy": "startTime", "maxResults": n, "q": query,
              "timeMin": _bound(time_min) if time_min else "", "timeMax": _bound(time_max) if time_max else ""}
    body = api(ctx, "GET", _path(calendar), params=params)
    events = [_event(e) for e in body.get("items") or []]
    return {"calendar": _cal(calendar), "count": len(events), "time_zone": body.get("timeZone"),
            "has_more": bool(body.get("nextPageToken")), "events": events}


def get_event(ctx, event_id: str, calendar="primary") -> dict:
    return _event(api(ctx, "GET", _path(calendar, event_id)), full=True)


def _attendees(att) -> list[dict]:
    if isinstance(att, str):
        att = att.split(",")
    return [{"email": str(a).strip()} for a in att or [] if str(a).strip()]


def _patch(summary="", start="", end="", description="", location="", attendees=None, time_zone="") -> dict:
    body: dict = {}
    if summary:
        body["summary"] = summary
    if start:
        body["start"] = _when(start, time_zone)
    if end:
        body["end"] = _when(end, time_zone)
    if description:
        body["description"] = description
    if location:
        body["location"] = location
    if attendees:
        body["attendees"] = _attendees(attendees)
    return body


def create_event(ctx, summary: str, start: str, end: str, calendar="primary", description="", location="",
                 attendees=None, time_zone="") -> dict:
    body = _patch(summary, start, end, description, location, attendees, time_zone)
    if ("date" in body["start"]) != ("date" in body["end"]):
        raise ToolError("start and end must both be dates (all-day) or both be times")
    e = api(ctx, "POST", _path(calendar), json=body,
            params={"sendUpdates": "all" if attendees else "none"})
    return {"ok": True, **_event(e)}


def update_event(ctx, event_id: str, calendar="primary", summary="", start="", end="", description="",
                 location="", attendees=None, time_zone="") -> dict:
    body = _patch(summary, start, end, description, location, attendees, time_zone)
    if not body:
        raise ToolError("nothing to change")
    e = api(ctx, "PATCH", _path(calendar, event_id), json=body,
            params={"sendUpdates": "all"})
    return {"ok": True, **_event(e)}


def delete_event(ctx, event_id: str, calendar="primary") -> dict:
    api(ctx, "DELETE", _path(calendar, event_id), params={"sendUpdates": "all"})
    return {"ok": True, "id": event_id, "calendar": _cal(calendar)}


_HANDLERS = {
    "calendar_list_calendars": list_calendars,
    "calendar_list_events": list_events,
    "calendar_get_event": get_event,
    "calendar_create_event": create_event,
    "calendar_update_event": update_event,
    "calendar_delete_event": delete_event,
}


def call(ctx, name: str, args: dict):
    return dispatch(TOOLS, _HANDLERS, ctx, name, args)
