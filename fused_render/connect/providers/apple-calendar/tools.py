"""Apple Calendar's tools, native: Calendar.app on this Mac through `osascript`
(JavaScript for Automation), the `local-app` kind (SPEC-connect-oauth §2, §11).

No token and no network: `ctx.token` is `""`, and every call is one JXA
program handed to `connect/applescript.run_jxa` with the request as argv (user
text is never interpolated into the script). Calendar.app already syncs every
account the user added to it (iCloud, Google, Exchange...), so one pack
reaches them all, and only what the app has synced.

Five tools: calendars, events (a window), search (a phrase in a window),
event (one, by uid), create_event. Nothing deletes or moves an event.

Times go in and out as ISO 8601 strings. A bare date (`2026-11-04`) with no
time is taken as that day's start, in this Mac's time zone, the way a person
would mean it; the app's own `startDate` is what comes back, as ISO with its
offset. Recurring events are returned once with `recurring: true` (the rule,
not its instances): Calendar.app's scripting dictionary knows no instances,
which `mac-automation`'s calendar action says too.

The window walk is `calendar.events.whose({startDate between})` per calendar,
the only query the dictionary offers; on a calendar with years of history it
takes a few seconds, which is why `days` is capped and the tool timeout is
55 s. A withdrawn Automation grant (-1743) is `ToolAuthError`: the core marks
the card Reconnect and the message says where in System Settings to allow it.
"""
from __future__ import annotations

import datetime
import json
import re

from fused_render.connect import applescript
from fused_render.connect.tools import ToolAuthError, ToolError

APP = "Calendar"
MAX_DAYS = 366
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DATE_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")

# One JXA program: `calendar(p)` takes the request object and returns the result object.
JXA = r"""
function calendar(p) {
  const app = Application("Calendar");
  app.includeStandardAdditions = false;
  const iso = d => { try { return d ? localIso(new Date(d)) : ""; } catch (e) { return ""; } };
  // ISO with this Mac's offset, so a 9:00 meeting reads as 09:00, not as UTC.
  const localIso = d => { const pad = n => String(n).padStart(2, "0"), o = -d.getTimezoneOffset(), s = o >= 0 ? "+" : "-";
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds()) + s + pad(Math.floor(Math.abs(o) / 60)) + ":" + pad(Math.abs(o) % 60); };
  // "2026-11-04" is that day's start here; anything else is what Date parses (ISO with an offset, or local).
  const when = s => { if (!s) return null; const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s); const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s); return isNaN(d) ? null : d; };
  const snip = (t, n) => { t = (t || "").replace(/\s+/g, " ").trim(); return t.length > n ? t.slice(0, n) + "…" : t; };
  const row = (e, cal, full) => {
    const r = { uid: e.uid(), summary: e.summary() || "", calendar: cal, start: iso(e.startDate()), end: iso(e.endDate()),
      all_day: !!e.alldayEvent(), location: e.location() || "" };
    let rec = ""; try { rec = e.recurrence() || ""; } catch (x) {}
    if (rec) { r.recurring = true; if (full) r.recurrence = rec; }
    let st = ""; try { st = String(e.status() || ""); } catch (x) {}
    if (st && st !== "none") r.status = st;
    const d = e.description() || "";
    if (full) { r.description = d; try { r.url = e.url() || ""; } catch (x) { r.url = ""; } }
    else if (d) r.description = snip(d, 160);
    return r; };
  const cals = () => { const all = app.calendars(); if (!p.calendar) return all;
    const hit = all.filter(c => c.name() === p.calendar); return hit; };
  const MAX_DAYS = p.max_days || 366;
  const limit = Math.max(1, Math.min(p.limit || 50, 500));
  const byStart = (a, b) => (a.start < b.start ? -1 : a.start > b.start ? 1 : 0);
  const window = () => {
    const from = when(p.from) || new Date(), to = when(p.to) || new Date(from.getTime() + (p.days || 7) * 86400000);
    if (to <= from) return { error: "`to` must be after `from`" };
    if (to - from > MAX_DAYS * 86400000) return { error: "window longer than " + MAX_DAYS + " days; narrow `from` and `to`" };
    const out = [];
    for (const cal of cals()) {
      const name = cal.name();
      const hits = cal.events.whose({ _and: [{ startDate: { _greaterThanEquals: from } }, { startDate: { _lessThan: to } }] })();
      for (const e of hits) { const r = row(e, name, false); r._text = (r.summary + " " + r.location + " " + (e.description() || "")).toLowerCase(); out.push(r); }
    }
    return { from: localIso(from), to: localIso(to), rows: out.sort(byStart) }; };
  // `_text` is the full title + location + notes, for search; the row shows a snippet.
  const strip = rows => rows.map(r => { delete r._text; return r; });
  let out;
  switch (p.action) {
    case "calendars": {
      out = { calendars: app.calendars().map(c => { let w = true; try { w = !!c.writable(); } catch (x) {}
        return { name: c.name(), description: c.description() || "", writable: w, color: String(c.color() || "") }; }) };
      break; }
    case "events": {
      if (p.calendar && !cals().length) { out = { error: "no calendar named " + p.calendar }; break; }
      const w = window(); if (w.error) { out = w; break; }
      out = { from: w.from, to: w.to, calendar: p.calendar || "", count: w.rows.length, events: strip(w.rows.slice(0, limit)) };
      break; }
    case "search": {
      if (p.calendar && !cals().length) { out = { error: "no calendar named " + p.calendar }; break; }
      const w = window(); if (w.error) { out = w; break; }
      const q = String(p.query || "").toLowerCase();
      const rows = w.rows.filter(r => r._text.indexOf(q) >= 0);
      out = { query: p.query, from: w.from, to: w.to, calendar: p.calendar || "", count: rows.length, events: strip(rows.slice(0, limit)) };
      break; }
    case "event": {
      let found = null;
      for (const cal of cals()) { const m = cal.events.whose({ uid: p.uid })(); if (m.length) { found = row(m[0], cal.name(), true); break; } }
      out = found ? { event: found } : { error: "no event with uid " + p.uid };
      break; }
    case "create": {
      const start = when(p.start), end = when(p.end) || (start && new Date(start.getTime() + (p.all_day ? 86400000 : 3600000)));
      if (!start) { out = { error: "start is not a date: " + p.start }; break; }
      if (end <= start) { out = { error: "end must be after start" }; break; }
      let cal = null;
      if (p.calendar) { const hit = cals(); if (!hit.length) { out = { error: "no calendar named " + p.calendar }; break; }
        // Two accounts can each have a "Work": a write must not guess which.
        if (hit.length > 1) { out = { error: hit.length + " calendars are named " + p.calendar + " (in different accounts); give one a unique name in Calendar" }; break; }
        cal = hit[0]; }
      else { const all = app.calendars(); cal = all.find(c => { try { return c.writable(); } catch (x) { return true; } }) || all[0];
        if (!cal) { out = { error: "Calendar has no calendars; add an account in the app first" }; break; } }
      const props = { summary: p.summary, startDate: start, endDate: end, alldayEvent: !!p.all_day };
      if (p.location) props.location = p.location;
      if (p.description) props.description = p.description;
      const ev = app.Event(props); cal.events.push(ev);
      out = { ok: true, event: row(ev, cal.name(), true) };
      break; }
    default: out = { error: "unknown action " + p.action };
  }
  return out;
}
function run(argv) { return JSON.stringify(calendar(JSON.parse(argv[0] || "{}"))); }
"""


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_CAL = {"type": "string", "description": "calendar name; every calendar when empty", "default": ""}
_WINDOW = {
    "from": {"type": "string", "description": "ISO date or date-time; now when empty", "default": ""},
    "to": {"type": "string", "description": "ISO date or date-time; `from` + days when empty", "default": ""},
    "days": {"type": "integer", "description": "window length when `to` is empty (1-366)", "default": 7},
    "calendar": _CAL,
    "limit": {"type": "integer", "description": "max events (1-500)", "default": 50},
}

TOOLS = [
    {"name": "calendar_list",
     "description": "List the calendars in Calendar on this Mac (every synced account), with whether each is writable.",
     "input_schema": _obj({}, []),
     "read_only": True, "destructive": False},
    {"name": "calendar_events",
     "description": "Events starting in a window (default: the next 7 days), in time order. "
                    "Recurring events appear once, as the rule, with recurring=true.",
     "input_schema": _obj(dict(_WINDOW), []),
     "read_only": True, "destructive": False},
    {"name": "calendar_search",
     "description": "Events in a window (default: the next 30 days) whose title, location or notes contain a phrase.",
     "input_schema": _obj({"query": {"type": "string"}, **{k: (dict(v, default=30) if k == "days" else v)
                                                           for k, v in _WINDOW.items()}}, ["query"]),
     "read_only": True, "destructive": False},
    {"name": "calendar_event",
     "description": "One event in full, by uid (from a listing).",
     "input_schema": _obj({"uid": {"type": "string"}, "calendar": _CAL}, ["uid"]),
     "read_only": True, "destructive": False},
    {"name": "calendar_create_event",
     "description": "Create an event. `start`/`end` are ISO date-times in this Mac's time zone (or dates, for an "
                    "all-day event); `end` defaults to an hour after `start`. Goes on the named calendar, "
                    "else the first writable one. No invitations are sent.",
     "input_schema": _obj({"summary": {"type": "string"}, "start": {"type": "string"},
                           "end": {"type": "string", "default": ""}, "calendar": _CAL,
                           "location": {"type": "string", "default": ""},
                           "description": {"type": "string", "default": ""},
                           "all_day": {"type": "boolean", "default": False}}, ["summary", "start"]),
     "read_only": False, "destructive": False},
]
_ACTIONS = {"calendar_list": "calendars", "calendar_events": "events", "calendar_search": "search",
            "calendar_event": "event", "calendar_create_event": "create"}
_PARAMS = {t["name"]: set(t["input_schema"]["properties"]) for t in TOOLS}
_REQUIRED = {t["name"]: t["input_schema"]["required"] for t in TOOLS}


def _run(req: dict) -> dict:
    try:
        return applescript.run_jxa(JXA, [json.dumps(req)], app=APP)
    except applescript.AutomationDenied as e:
        raise ToolAuthError(str(e)) from None
    except applescript.AutomationError as e:
        raise ToolError(str(e)) from None


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(v if v not in (None, "") else default)))
    except (TypeError, ValueError):
        return default


def _date(v) -> str:
    """A date argument as the script takes it: ISO, or a bare date. Anything else is refused here
    rather than by `new Date`, whose parse of free text is a guess, and a date that does not
    exist (2026-02-30) is refused too: JXA's `new Date(y, m, d)` would roll it into March."""
    s = str(v or "").strip()
    if not s:
        return s
    if not (_DATE_ONLY.match(s) or _DATE_TIME.match(s)):
        raise ToolError(f"not an ISO date or date-time: {s!r}")
    s = s.replace(" ", "T", 1) if " " in s else s
    try:
        datetime.datetime.fromisoformat(s)
    except ValueError:
        raise ToolError(f"not a real date: {s!r}") from None
    return s


def _bool(v, name: str) -> bool:
    """A JSON boolean, or the words a model types for one; never `bool("false")`."""
    if isinstance(v, bool) or v is None:
        return bool(v)
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("", "true", "false", "yes", "no", "1", "0"):
        return v.strip().lower() in ("true", "yes", "1")
    raise ToolError(f"{name} must be true or false, got {v!r}")


def call(ctx, name: str, args: dict):
    action = _ACTIONS.get(name)
    if action is None:
        raise ToolError(f"unknown tool {name}")
    a = {k: v for k, v in (args or {}).items() if k in _PARAMS[name]}
    for k in _REQUIRED[name]:
        if not str(a.get(k) or "").strip():
            raise ToolError(f"missing argument: {k}")
    days_default = 30 if action == "search" else 7
    req = {"action": action, "calendar": str(a.get("calendar") or ""), "query": str(a.get("query") or ""),
           "uid": str(a.get("uid") or ""), "summary": str(a.get("summary") or "").strip(),
           "location": str(a.get("location") or ""), "description": str(a.get("description") or ""),
           "all_day": _bool(a.get("all_day"), "all_day"), "max_days": MAX_DAYS,
           "from": _date(a.get("from")), "to": _date(a.get("to")),
           "start": _date(a.get("start")), "end": _date(a.get("end")),
           "days": _int(a.get("days"), days_default, 1, MAX_DAYS), "limit": _int(a.get("limit"), 50, 1, 500)}
    if req["from"] and req["to"]:
        span = datetime.datetime.fromisoformat(req["to"]) - datetime.datetime.fromisoformat(req["from"])
        if span.days > MAX_DAYS:
            raise ToolError(f"window longer than {MAX_DAYS} days; narrow `from` and `to`")
    return _run(req)
