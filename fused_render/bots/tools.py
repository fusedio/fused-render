"""The bot's tool table: ONE definition of what a bot can do, shared by both
engines (docs/bots.md §6).

The steps engine (`steps_engine.py`, OpenBot's JSON-action loop) and the agent
engine (`agent_engine.py`, Claude Code driving native tools over MCP) both
route a decision through here:

    spec     = TOOL_SPECS[name]                      what the model sees (schema, description)
    label    = describe(bot, name, args, obs)        the transcript line / approval preview
    why      = risk(bot, name, args, obs)            "" or why it needs the user's yes
    label, result = execute(bot, name, args, obs)    run it; result is plain text ("ok, now at …", "error: …",
                                                     "RESULT:\\n…", "TEXT:\\n…")

plus the observation formatters (`element_lines`, `format_observation`,
`change_report`) that turn `browser.observe()` into the text the model reads,
and `ui_summary` / `ui_detail`, which turn the same result into what the USER
reads on the action chip. Two audiences: the model's text carries
instructions ("Now `done` …") and whole pages; the chip carries one plain
line, plus the raw result behind a disclosure when it adds something.

`describe`, `risk` and `execute` resolve a `tool` / `py` decision through the
SAME helpers (`apptools.tool_ref`, `bot.py_ref`) so a call can never reach
`execute` on a different target than the gate judged (OpenBot's rule).

What this module expects of `bot` (class Bot in bot.py):
  bot.browser                         Browser (browser.py): goto/click/type/hover/scroll/press/select/read/
                                      wait_for/upload/tab_new/tab_switch/tab_close/back/screenshot/observe
  bot.meta                            the bot.json dict (approval, build_access, builds, imessage, imessage_to …)
  bot.contacts()                      [(label, handle)]           (imessage.parse_contacts)
  bot.contact(args)                   (label, handle) | None      (imessage.resolve_contact over `to`/`ref`/`name`)
  bot.py_ref(args)                    (app_dir | None, file, args)
  bot.resolve_file(name)              absolute path for `upload`
  bot.save_file(name, text)           -> absolute path (Inbox)
  bot.remember(note)                  -> result sentence
  bot.skill_save(title, trigger, body) -> name  (raises ValueError)
  bot.build(name, spec, fresh)        -> (label, result)
  bot.run_tool(app, name, args)       -> (label, result)
  bot.run_py(args)                    -> (label, result)
  bot.show_app(name, obs)             -> (label, result)
  bot.stop_flag, bot.pause_flag, bot.wake   threading.Event
  bot.all_files()                     [{name, size, kind, …}]  (attached + downloaded)
Everything else (ask/login/offer waits, approvals, pause/stop) is the
engine's job: those are control flow, not tools, and each engine owns its
loop.
"""
from __future__ import annotations

import json
import os
import re
import time

from fused_render.bots import apptools

# Element refs the model may use look like sb12 (browser.py stamps them).
REF_RE = re.compile(r"^sb\d+$")

# Button labels that usually commit something you cannot take back. Kept
# narrow on purpose: cookie "Accept", "Submit" on a search form and "Apply"
# on a filter are everyday clicks and must not nag. (OpenBot `_RISKY_BTN`.)
RISKY_BTN = re.compile(
    r"^\W*(buy( now)?|pay( now)?|purchase|place( your)? order|complete (order|purchase|booking)|checkout|check out|"
    r"send( message| email| now)?|post( comment| now)?|publish|reply|tweet|share now|comment|"
    r"delete( account| all)?|remove( all)?|unsubscribe|deactivate|cancel (subscription|order|account)|"
    r"transfer|withdraw|book( now)?|reserve|confirm (order|payment|purchase|booking|transfer|delete|deletion)|"
    r"connect|follow|like|apply now|easy apply|submit application|sign up|create account|"
    r"change password|update (email|password)|save changes)\W*$", re.I)

# Actions that change the page (and so earn a step thumbnail + a change report).
BROWSER_ACTIONS = frozenset({"goto", "click", "type", "press", "select", "hover", "scroll", "wait", "back",
                             "tab", "upload", "read", "observe", "screenshot"})
# Actions whose result the model should treat as data it is now looking at
# (OpenBot's CURRENT RESULT / one-run ledger).
RESULT_ACTIONS = frozenset({"tool", "py"})
# Actions whose result is file content the model is now looking at (long, kept whole in the transcript).
FILE_ACTIONS = frozenset({"readfile"})

COMPACT_ELEMENTS = 40       # an action result's element list
COMPACT_TEXT = 1200         # … and its text excerpt
FULL_ELEMENTS = 160         # `observe`
FULL_TEXT = 6000
READ_CAP = 6000


def _s(**props):
    """A JSON-schema object with these properties (strings unless given)."""
    out = {"type": "object", "properties": {}, "additionalProperties": False}
    required = []
    for name, spec in props.items():
        if isinstance(spec, tuple):
            schema, req = spec
        else:
            schema, req = spec, False
        if isinstance(schema, str):
            schema = {"type": schema}
        out["properties"][name] = schema
        if req:
            required.append(name)
    if required:
        out["required"] = required
    return out


STR = "string"
REQ = ({"type": "string"}, True)
REF = {"type": "string", "description": "an element ref from the element list, e.g. sb12"}
REF_REQ = (REF, True)

#: name -> {description, inputSchema}. Descriptions are the model-facing
#: contract; keep them short and exact (they are sent on every task).
TOOL_SPECS: dict[str, dict] = {
    "observe": {
        "description": "Read the current page in full: url, title, every interactive element with its ref, the visible "
                       "text (6000 chars), open tabs, downloads, and whether a popup/dialog is open. Action results already "
                       "carry a compact view; call this when you need the whole page.",
        "inputSchema": _s()},
    "screenshot": {
        "description": "A screenshot of the current page (what a user would see). Use it when the element list is "
                       "unclear or thin (canvas apps, image-heavy pages, layout questions).",
        "inputSchema": _s()},
    "goto": {"description": "Navigate the current tab to a URL (https:// is added when missing).",
             "inputSchema": _s(url=REQ)},
    "click": {"description": "Real mouse click on an element by ref.",
              "inputSchema": _s(ref=REF_REQ, risky={"type": "boolean", "description": "true when the click is irreversible (send, buy, delete, post…)"})},
    "type": {"description": "Focus a text field by ref, clear it and enter text; submit=true presses Enter after. "
                            "Never click a field first: this focuses it.",
             "inputSchema": _s(ref=REF_REQ, text=REQ, submit="boolean",
                               risky={"type": "boolean", "description": "true when submitting is irreversible"})},
    "press": {"description": "One keystroke, optionally with modifiers: Escape, Enter, Tab, ArrowDown, Control+a, Meta+Enter. "
                             "With ref, focus that element first.",
              "inputSchema": _s(key=REQ, ref=REF, risky="boolean")},
    "select": {"description": "Choose an option in a <select> by label or value. Custom dropdowns are not <select>: "
                              "click them, then click the option.",
               "inputSchema": _s(ref=REF_REQ, value=REQ, risky="boolean")},
    "hover": {"description": "Move the mouse over an element (opens hover menus) without clicking.",
              "inputSchema": _s(ref=REF_REQ)},
    "scroll": {"description": "Scroll the page up or down, or bring one element (ref) into view.",
               "inputSchema": _s(direction={"type": "string", "enum": ["up", "down"]}, ref=REF)},
    "wait": {"description": "Wait up to 10 s for `text` to appear on the page (a page still loading, a result list); "
                            "with no text, wait 2 s.",
             "inputSchema": _s(text=STR)},
    "read": {"description": "The full text of one element by ref (an article, a post, a table), or of the whole page "
                            "when no ref. Use it before summarising content.",
             "inputSchema": _s(ref=REF)},
    "back": {"description": "Browser back.", "inputSchema": _s()},
    "tab": {"description": "Tabs: new (with url), switch (index), close (index; current when omitted). Links that open a "
                           "new tab show up under TABS; switch to work there.",
            "inputSchema": _s(tab=({"type": "string", "enum": ["new", "switch", "close"]}, True), url=STR, index="integer")},
    "readfile": {"description": "Read a file from FILES (attached, downloaded or saved earlier) by name: text/CSV/JSON/HTML "
                                "come back as text (long files in parts, `page` picks one), images are attached so you see "
                                "them, PDFs one page at a time (`page`, text or the page as an image when it is a scan), "
                                "RTF/DOC/DOCX as text. Use it instead of guessing what a file contains.",
                 "inputSchema": _s(file=REQ, page={"type": "integer", "description": "part/page number, default 1"})},
    "upload": {"description": "Put a local file into the page's file input (FILE INPUT elements; a styled Upload button often "
                              "reveals one). `file` is a name from FILES or a path the user gave. Always needs the user's approval.",
               "inputSchema": _s(file=REQ, ref=REF)},
    "save": {"description": "Write text (Markdown, CSV, JSON) to this task's folder in the user's Inbox (~/Fused/bots/<you>/<task>/). "
                            "Use it when the task asks to save/export or the result is longer than a chat message. "
                            "ARTIFACTS lists what this task saved already: never save the same thing twice.",
             "inputSchema": _s(name=REQ, text=REQ)},
    "remember": {"description": "Save one short durable note to your MEMORY (site quirks, user preferences, where things live). "
                                "Never secrets. One note per fact; do not repeat what MEMORY already says.",
                 "inputSchema": _s(text=REQ)},
    "learn": {"description": "Save a PLAYBOOK for this kind of task: a short title, comma-separated trigger words a future task "
                             "would contain, and 5-15 numbered steps with exact URLs, what to click, what to skip. Use it right "
                             "before finishing when a task took real exploration; not when a matching PLAYBOOK already worked.",
              "inputSchema": _s(title=REQ, trigger=REQ, steps=REQ)},
    "ask": {"description": "Ask the user something and wait for the answer (a decision, a choice). Never for passwords, codes or "
                           "sign-ins (use login). With 2-5 short `options` the user can pick one with a click; they may still type.",
            "inputSchema": _s(message=REQ, options={"type": "array", "items": {"type": "string"}, "maxItems": 5},
                              summary={"type": "string", "description": "When CHANNEL is present: the question in one short plain sentence, for the text message."})},
    "login": {"description": "The page needs a sign-in, 2FA or captcha: opens a real Chrome window on the user's desktop where "
                             "they sign in with their own keyboard (password manager and passkeys work), waits until they reply "
                             "'done' or click Hand back, then returns. `message` says why, in one sentence.",
              "inputSchema": _s(message=REQ)},
    "text": {"description": "Send an iMessage from this Mac to someone in CONTACTS (by name or handle). Only listed contacts. "
                            "Cannot be unsent, so it always goes through the approval gate. Short, plain, no Markdown.",
             "inputSchema": _s(to=REQ, text=REQ)},
    "texts": {"description": "Read the recent iMessage thread with a CONTACTS person. With `seconds` (max 300) first wait up to "
                             "that long for a NEW message from them (use right after `text` when you need their answer).",
              "inputSchema": _s(to=REQ, seconds="integer")},
    "tool": {"description": "Call one of the APP TOOLS (functions local apps on this Mac expose). `app` is the app folder, `name` "
                            "the tool, `args` exactly the parameters shown. Prefer a tool over browsing when it does the job. "
                            "Tools marked [approval] pause for the user's yes. The same call with the same args is refused until "
                            "the user speaks again. Never put passwords or keys in args.",
             "inputSchema": _s(app=REQ, name=REQ, args={"type": "object"}, risky="boolean")},
    "py": {"description": "Run one .py of a local fused app directly (its main(**args)), the way the app's own page would. Apps "
                          "marked [py] in APPS ship a SKILL.md; `py` with `app` and NO `file` loads it under APP SKILLS, then "
                          "`py` app file args runs one of its `## file.py` sections. The value comes back as RESULT: use it, never "
                          "repeat the same call. Your own builds run at once; other apps' files ask the user first.",
           "inputSchema": _s(app=REQ, file=STR, args={"type": "object"}, risky="boolean")},
    "build": {"description": "Have Claude Code create (or, when an app of that exact name exists, UPDATE) a small local fused-render "
                             "app for the user: `name` short, `spec` what it shows, what the user does, where the data comes from "
                             "(paste what you gathered). Runs for minutes on its own; you get a link at once: report it and "
                             "finish, the user hears again when it is ready. At most one build per task. When the user asked for "
                             "an app in so many words, build straight away (the approval card confirms it). new=true forces a "
                             "separate copy.",
              "inputSchema": _s(name=REQ, spec=REQ, new="boolean")},
    "show": {"description": "Put a built app INTO THE CHAT as a card the user opens right there. `name` is an app's name or "
                            "folder from APPS, a build link, or a pasted /render link with state; omit it for the page you are on. "
                            "Use it whenever the user asks to see/show/open an app here; never read the app aloud instead. The card "
                            "is the answer: finish in one line after it.",
             "inputSchema": _s(name=STR)},
    "offer": {"description": "PROPOSE an app and wait for the user's one-click answer: an existing app from APPS (`name`; they get "
                             "Use it / Not now) or a new one worth building (`name` + `spec` as for build; they get Build it / Not "
                             "now, and a yes starts the build). `message`: one or two plain sentences on what it does for them, plus "
                             "your findings when the task is otherwise complete. One offer per task, never on routines, never for a "
                             "quick fact. Returns what the user decided.",
              "inputSchema": _s(name=REQ, message=REQ, spec=STR)},
}

ALL_TOOLS = tuple(TOOL_SPECS)

# Super Bot's permission bridge (agent_engine._permission): the tool Claude Code
# calls INSTEAD of a terminal prompt (`--permission-prompt-tool`), so a Bash /
# Edit / Write the CLI would ask about becomes an approval card. Listed only on
# Super Bot's roster and only because the CLI needs to find it; it is not a tool
# for the model (the system prompt says so) and it is not in TOOL_SPECS, so
# the handler never counts it as a step.
PERMISSION_TOOL = "permission"
PERMISSION_SPEC = {
    "description": "Internal: Claude Code's permission prompt. Never call this yourself.",
    "inputSchema": {"type": "object", "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                                                     "tool_use_id": {"type": "string"}}},
}


def is_super(bot) -> bool:
    return (getattr(bot, "meta", None) or {}).get("kind") == "super"


def roster(bot) -> list[dict]:
    """The tools THIS bot gets this task (docs §6): `tool` only when app
    tools are available, `text`/`texts` only with contacts, `upload` only
    when there is something to upload. MCP's `tools/list` shape."""
    names = list(ALL_TOOLS)
    if not apptools.available():
        names.remove("tool")
    if not bot.contacts():
        names.remove("text")
        names.remove("texts")
    try:
        has_files = bool(bot.all_files())
    except Exception:  # noqa: BLE001
        has_files = False
    if not has_files:
        names.remove("upload")
        names.remove("readfile")
    out = [{"name": n, "description": TOOL_SPECS[n]["description"], "inputSchema": TOOL_SPECS[n]["inputSchema"]}
           for n in names]
    if is_super(bot):
        out.append({"name": PERMISSION_TOOL, **PERMISSION_SPEC})
    return out


# ------------------------------------------------------------ observation ---
def element_lines(elements: list[dict]) -> list[str]:
    """OpenBot's `_prompt` element format: `sb12 button type=… name=… "text" …`."""
    out = []
    for e in elements:
        bits = [e.get("ref", ""), e.get("tag", "")]
        if e.get("role") and e.get("role") != e.get("tag"):
            bits.append(f"role={e['role']}")
        if e.get("type"):
            bits.append(f"type={e['type']}")
        if e.get("name"):
            bits.append(f"name={e['name']}")
        if e.get("text"):
            bits.append(json.dumps(e["text"]))
        if e.get("href") and not e.get("text"):
            bits.append(e["href"])
        if e.get("options"):
            bits.append("options=[" + " | ".join(e["options"]) + "]")
        if e.get("value") and e.get("tag") == "select":
            bits.append(f"selected={json.dumps(e['value'])}")
        if "checked" in e:
            bits.append("checked" if e["checked"] else "unchecked")
        if e.get("expanded") is not None:
            bits.append("expanded" if e["expanded"] else "collapsed")
        if e.get("selected"):
            bits.append("selected")
        if e.get("disabled"):
            bits.append("disabled")
        if e.get("empty"):
            bits.append("(EMPTY text field" + (", focused" if e.get("focused") else "") + ": use `type`, not click)")
        elif "empty" in e:
            bits.append(f"(text field holds {json.dumps(e.get('value') or '')}" + (", focused" if e.get("focused") else "") + ")")
        if e.get("upload"):
            bits.append("(FILE INPUT: use `upload`" + (f", accepts {e['accept']}" if e.get("accept") else "") + ")")
        if e.get("menu"):
            bits.append("(in dropdown menu, clickable)")
        if e.get("dialog"):
            bits.append("(IN POPUP DIALOG)")
        if e.get("offscreen"):
            bits.append("(below the fold)")
        out.append(" ".join(b for b in bits if b))
    return out


def format_observation(obs: dict, compact: bool = False, text: bool = True) -> str:
    """The page as the model reads it. `compact` is what rides on an action
    result (docs §6: 40 elements, 1200 chars); the full form is `observe`.
    `text=False` drops the VISIBLE TEXT block when the change report says the
    text is byte-identical to the view the model already holds (context is the
    budget; 1200 repeated chars buy nothing)."""
    els = list(obs.get("elements") or [])
    n_all = len(els)
    cap_els = COMPACT_ELEMENTS if compact else FULL_ELEMENTS
    cap_txt = COMPACT_TEXT if compact else FULL_TEXT
    if compact and len(els) > cap_els:
        # Viewport first: elements the user can see now, then the rest in page order.
        on = [e for e in els if not e.get("offscreen")]
        off = [e for e in els if e.get("offscreen")]
        els = (on + off)[:cap_els]
    popup = ""
    if any(e.get("dialog") for e in els):
        popup = ("\nPOPUP OPEN: a dialog/modal is showing (elements marked IN POPUP DIALOG). "
                 "Deal with it first (read its text below, then click one of its buttons or close it).")
    if obs.get("dialog"):
        popup += f"\nA browser alert/confirm popped up and was auto-accepted: {obs['dialog']}"
    tabs = obs.get("tabs") or []
    tabs_s = ""
    if len(tabs) > 1:
        tabs_s = "\nTABS (use `tab switch index` to move):\n" + "\n".join(
            f"- [{t['i']}]{' *current*' if t['active'] else ''} {t['url'][:100]}" + (f"  ({t['title'][:50]})" if t.get("title") else "")
            for t in tabs)
    dls = obs.get("downloads") or []
    dls_s = ""
    if dls:
        dls_s = "\nDOWNLOADS (arrived in this bot's folder):\n" + "\n".join(f"- {d['name']} ({d['size']} bytes)" for d in dls[:8])
    body = (obs.get("text") or "")[:cap_txt]
    more = f" (showing {len(els)} of {n_all}; call observe for all)" if len(els) < n_all else ""
    lines = element_lines(els)
    return (f"CURRENT PAGE\nurl: {obs.get('url')}\ntitle: {obs.get('title')}{popup}{tabs_s}{dls_s}\n\n"
            f"INTERACTIVE ELEMENTS ({n_all}{more}):\n" + ("\n".join(lines) or "(none)")
            + (f"\n\nVISIBLE TEXT:\n{body or '(none)'}" if text else "\n\nVISIBLE TEXT: (unchanged since your last view)"))


def _sig(e: dict) -> tuple:
    return (e.get("role") or e.get("tag") or "", (e.get("text") or e.get("name") or "")[:60])


def change_report(before: dict | None, after: dict) -> str:
    """One or two lines on what an action changed (docs §6): url/title/dialog
    diffs and the controls that appeared or vanished, else 'nothing visible
    changed'. This is what breaks open/close loops: the model is told when a
    click did nothing."""
    if not before:
        return ""
    parts = []
    bu, au = before.get("url"), after.get("url")
    bt, at = before.get("title") or "", after.get("title") or ""
    moved = bu != au
    if moved:
        parts.append(f"url changed to {au}" + (f" (title {at!r})" if at else ""))
    elif bt != at:
        parts.append(f"title is now {at!r}")
    if after.get("dialog"):
        parts.append(f"a dialog popped up and was accepted: {after['dialog']}")
    b_el = {_sig(e) for e in before.get("elements") or []}
    a_el = {_sig(e) for e in after.get("elements") or []}
    # On a new page every control is "new": the diff is noise there (the
    # compact view that follows lists the page); it is the signal on the SAME page.
    new = [] if moved else [s for s in a_el - b_el if s[1]]
    gone = [] if moved else [s for s in b_el - a_el if s[1]]
    popup_now = any(e.get("dialog") for e in after.get("elements") or [])
    popup_before = any(e.get("dialog") for e in before.get("elements") or [])
    if popup_now and not popup_before:
        parts.append("a popup/dialog opened")
    elif popup_before and not popup_now:
        parts.append("the popup/dialog closed")
    if new:
        shown = ", ".join(f"{r} {json.dumps(t)}" for r, t in sorted(new)[:8])
        parts.append(f"{len(new)} new control{'s' if len(new) != 1 else ''} appeared: {shown}" + (" …" if len(new) > 8 else ""))
    if gone:
        shown = ", ".join(f"{r} {json.dumps(t)}" for r, t in sorted(gone)[:5])
        parts.append(f"{len(gone)} control{'s' if len(gone) != 1 else ''} gone: {shown}" + (" …" if len(gone) > 5 else ""))
    # Field values: innerText and the signatures both ignore what a text field
    # or <select> holds, so `type` and `select` read as "nothing visible
    # changed". Compare the values of the fields present in both views.
    changed = [] if moved else _field_changes(before, after)  # a same-named field on a new page was not typed into
    if changed:
        shown = "; ".join(f"{json.dumps(label or 'a field')} now holds {json.dumps(v)}" for label, v in changed[:3])
        parts.append(shown)
    btxt, atxt = (before.get("text") or "")[:FULL_TEXT], (after.get("text") or "")[:FULL_TEXT]
    if not parts:
        if btxt != atxt:
            parts.append("the page text changed; the controls did not")
        elif _on_screen(before) != _on_screen(after):
            # Same url, controls and (whole-page) text, but a different slice of
            # them is in the viewport: a scroll worked. Without this a scroll
            # reads as "nothing visible changed" and the model is told to change
            # course on a route that is making progress.
            parts.append(SCROLLED)
        else:
            parts.append(UNCHANGED + " (same url, controls and text)")
    return "CHANGE: " + "; ".join(parts) + "."


UNCHANGED = "nothing visible changed"
SCROLLED = "the page scrolled; a different part is on screen"


def _fields(obs: dict) -> dict:
    """{signature: value} for every text field / <select> in a view. Keyed by
    signature, not ref: refs are renumbered on every snapshot."""
    out = {}
    for e in obs.get("elements") or []:
        if "empty" in e or e.get("tag") == "select":
            out.setdefault(_sig(e), str(e.get("value") or ""))
    return out


def _field_changes(before: dict, after: dict) -> list:
    """[(label, new value)] for the fields that hold something different now."""
    b, a = _fields(before), _fields(after)
    return [(sig[1], a[sig][:40]) for sig in a if sig in b and a[sig] != b[sig]]


def _on_screen(obs: dict) -> tuple:
    """The on-screen controls in page order. Signatures, not refs: refs are
    renumbered on every snapshot, and the ordered tuple (not a set) still sees
    a scroll through a list of identical "Add to cart" buttons."""
    return tuple(_sig(e) for e in obs.get("elements") or [] if not e.get("offscreen"))


def unchanged(change: str) -> bool:
    """The change report says the action changed nothing at all."""
    return (change or "").startswith("CHANGE: " + UNCHANGED)


def text_unchanged(change: str) -> bool:
    """The page text is byte-identical to the previous view (nothing changed,
    or only the viewport moved): the VISIBLE TEXT block can be left out."""
    return unchanged(change) or (change or "") == f"CHANGE: {SCROLLED}."


# ------------------------------------------------------------- UI summary ---
UI_CAP = 160        # the action chip's one line
DETAIL_CAP = 1500   # the raw result behind the chip's disclosure
# Tools whose raw result is the point (a text, a value, a page): it always rides as detail.
_DETAIL_ALWAYS = frozenset({"read", "readfile", "tool", "py", "observe", "texts"})
_NOW_DONE = re.compile(r"\s*Now `done`.*\Z", re.S)


def _clip(s: str, cap: int = UI_CAP) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= cap else s[:cap - 1].rstrip() + "…"


def _short_url(u: str, cap: int = 70) -> str:
    """A url as a person reads it: no scheme, no www., trailing slash dropped, clipped."""
    u = re.sub(r"^[a-z][a-z0-9+.-]*://", "", u or "", flags=re.I)
    u = re.sub(r"^www\.", "", u).rstrip("/")
    return u if len(u) <= cap else u[:cap - 1] + "…"


def _first_line(s: str) -> str:
    return next((ln for ln in (s or "").strip().splitlines() if ln.strip()), "")


def _change_text(change: str) -> str:
    c = (change or "").strip()
    if c.startswith("CHANGE: "):
        c = c[len("CHANGE: "):]
    c = c.rstrip(".").strip()
    return c.replace(UNCHANGED + " (same url, controls and text)", UNCHANGED)


def ui_summary(name: str, result: str, change: str = "", obs: dict | None = None, label: str = "") -> str:
    """What the user reads on an action chip: ONE line, at most 160 chars,
    written for them. Never the model's instructions (build/show results end
    in "Now `done` …"), never a page dump. Shared by both engines; `label`
    (the action's transcript line) only helps `show` name its app."""
    raw = (result or "").strip()
    if raw.startswith("error:"):
        return _clip(_first_line(raw))
    if name == "observe":
        o = obs or {}
        n, k = len(o.get("elements") or []), len(o.get("tabs") or [])
        return f"{n} elements" + (f" · {k} tabs" if k > 1 else "")
    if name in ("read", "readfile"):
        body = raw[len("TEXT:"):].strip() if raw.startswith("TEXT:") else raw
        return f"{len(body):,} chars"
    if name == "screenshot":
        return "captured"
    if name == "scroll" and change and raw.startswith("ok"):
        # A scroll's controls diff is just what came into the DOM-scan window;
        # the user wants to know whether it moved.
        if unchanged(change):
            return "nothing more to scroll to"
        m = re.search(r"(\d+) new controls? appeared", change)
        return "scrolled" + (f" · {m.group(1)} more controls in view" if m else "")
    if name in BROWSER_ACTIONS:
        # "ok, now at <url>" says less than the change report; a non-ok status
        # ("not found after 10 s") is itself the news.
        if change and raw.startswith("ok"):
            c = _change_text(change)
            nav = re.match(r"url changed to (\S+)", c)
            if nav:
                # A navigation: where we are now, by host and title, not the diff.
                title = " ".join(str((obs or {}).get("title") or "").split())
                return _clip(f"now at {_short_url(nav.group(1))}" + (f' · "{title[:60]}"' if title else ""))
            m = re.match(r'ok, chose "(.*)"$', raw)
            return _clip(f"chose \"{m.group(1)}\" · {c}" if m else c)
        return _clip(_first_line(raw))
    if name in ("tool", "py") and raw.startswith("RESULT:"):
        body = raw[len("RESULT:"):].strip()
        head = f"result · {len(body):,} chars"
        first = _first_line(body)
        return _clip(f"{head} · {first}") if first else head
    if name == "show":
        m = re.search(r'"([^"]+)"', label or "")
        if m:
            app = m.group(1)
        else:
            m = re.search(r"\(([^()]+)\)", raw)
            app = os.path.basename(m.group(1).rstrip("/")) if m else "app"
        return _clip(f"{app} card posted")
    if name == "build":
        doing = "updating" if "updating" in _NOW_DONE.sub("", raw) else "building"
        return f"Claude is {doing} it now · link posted"
    if name == "save":
        m = re.search(r"([\d,]+) chars\)", raw)
        return f"saved to Inbox · {int(m.group(1).replace(',', '')):,} chars" if m else _clip(_first_line(raw))
    if name == "texts" and raw.startswith("TEXTS with"):
        n = sum(1 for ln in raw.splitlines()[1:] if ln.startswith("["))
        return f"{n} message{'s' if n != 1 else ''}"
    return _clip(_first_line(raw))


def ui_detail(name: str, result: str, summary: str, change: str = "") -> str | None:
    """The raw result behind the chip (capped at 1500 chars), or None when it
    would only repeat the summary. `show` never gets one and `build` loses its
    "Now `done` …" tail: both are written to the model, not the user."""
    raw = (result or "").strip()
    if name == "show":
        return None
    if name == "build":
        raw = _NOW_DONE.sub("", raw).strip()
    elif name in BROWSER_ACTIONS and name not in _DETAIL_ALWAYS and raw.startswith("ok"):
        # The full change report, when the chip had to cut it. A plain
        # navigation ("url changed to … (title …)") is wholly on the chip as
        # "now at …"; only a navigation that also reports a popup/dialog has more.
        full = _change_text(change)
        if full.startswith("url changed to ") and ";" not in full:
            return None
        return full[:DETAIL_CAP] if full and full != summary else None
    if not raw or raw == summary or " ".join(raw.split()) == summary:
        return None
    return raw[:DETAIL_CAP]


# --------------------------------------------------------------- resolve ---
def element(args: dict, obs: dict) -> dict:
    ref = (args or {}).get("ref") or ""
    return next((e for e in (obs or {}).get("elements", []) if e.get("ref") == ref), None) or {}


def target(args: dict, obs: dict) -> tuple[str, str, dict]:
    """(ref, label, kwargs): the model's ref, the element's text for the
    transcript line, and the ref/text/x/y quartet every browser.py element
    method takes (plus `backend` when the AX snapshot knows the node)."""
    ref, el = (args or {}).get("ref") or "", element(args, obs)
    kw = {"ref": ref, "text": el.get("text") or "", "x": el.get("x"), "y": el.get("y")}
    if el.get("backend") is not None:
        kw["backend"] = el["backend"]
    return ref, what_is(el, ref), kw


def what_is(el: dict, ref: str = "") -> str:
    """How the transcript names an element: its text, else its form name, else
    what kind of field it is. A bare `sb162` on a chip means nothing to the
    user (an unlabeled search box has no text)."""
    if el.get("text"):
        return el["text"]
    if el.get("name"):
        return el["name"]
    tag = el.get("tag") or ""
    if tag in ("input", "textarea") or el.get("role") == "textbox" or "empty" in el:
        return "search box" if el.get("type") == "search" else "text field"
    if tag == "select":
        return "dropdown"
    return ref


def describe(bot, act: str, d: dict, obs: dict) -> str:
    """The approval preview / transcript label (OpenBot `_describe`)."""
    d = d or {}
    el = element(d, obs)
    what = what_is(el, d.get("ref") or "")
    if act == "click":
        return f"click \"{what}\""
    if act == "type":
        return f"type \"{(d.get('text') or '')[:80]}\" into \"{what}\"" + (" and press Enter" if d.get("submit") else "")
    if act == "press":
        return f"press {d.get('key')}" + (f" in \"{what}\"" if what else "")
    if act == "select":
        return f"select \"{d.get('value')}\" in \"{what}\""
    if act == "upload":
        return f"upload \"{d.get('file') or d.get('text') or ''}\"" + (f" into \"{what}\"" if what else "")
    if act == "readfile":
        pg = d.get("page") or d.get("index")
        return f"read file \"{d.get('file') or d.get('name') or d.get('text') or ''}\"" + (f" page {pg}" if pg and pg != 1 else "")
    if act == "text":
        c = bot.contact(d)
        who = f"{c[0]} ({c[1]})" if c else (d.get("to") or "?")
        return f"text {who}: \"{(d.get('text') or d.get('message') or '')[:200]}\""
    if act == "build":
        from fused_render.bots import paths as bpaths
        tgt = os.path.join(bpaths.apps_root(), bpaths.slug(d.get("name")))
        verb = "update the app" if os.path.isfile(os.path.join(tgt, "index.html")) and d.get("new") is not True else "build the app"
        spec = " ".join((d.get("spec") or d.get("text") or d.get("message") or "").split())
        return f"{verb} \"{(d.get('name') or 'app')[:60]}\" in {tgt}" + (f" — {spec[:160]}{'…' if len(spec) > 160 else ''}" if spec else "")
    if act == "tool":
        app, name, args = apptools.tool_ref(d)
        rec = apptools.find(apptools.registry(), app, name)
        who = f"{rec.app} › {rec.name}" if rec else f"{app or '?'} › {name or '?'}"
        return f"call {who} with {json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)[:200]}"
    if act == "py":
        app_dir, file, args = bot.py_ref(d)
        where = os.path.basename(app_dir) if app_dir else (d.get("app") or "?")
        if not file:
            return f"load the SKILL.md of {where}"
        return f"run {where} › {file} with {json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False)[:200]}"
    if act == "goto":
        return f"goto {d.get('url') or ''}".strip()
    return f"{act} {what}".strip()


def risk(bot, act: str, d: dict, obs: dict) -> str:
    """Why this action needs approval, or '' when it does not (OpenBot `_risk`)."""
    d = d or {}
    if act == "upload":
        return f"It sends the local file \"{d.get('file') or d.get('text') or ''}\" to this site."
    if act == "text":
        return "" if not bot.contact(d) else "An iMessage cannot be unsent."  # unknown contact fails in execute instead
    if act == "build":
        return ("It starts a Claude Code session that writes files and spends model credit"
                + (", unattended (Builds: full access)." if bot.meta.get("build_access") == "full" else "."))
    if act == "tool":
        app, name, _ = apptools.tool_ref(d)
        rec = apptools.find(apptools.registry(), app, name)
        if rec is None:
            return ""
        if d.get("risky") is True:
            return f"It calls {rec.app} › {rec.name}, which changes something outside this chat."
        if apptools.is_trusted(bot.meta, rec.app):
            return ""  # the user whitelisted this app for this bot (bot.json trusted_apps)
        if apptools.is_write(rec):
            return f"It calls {rec.app} › {rec.name}, which changes something outside this chat."
        return ""
    if act == "py":
        app_dir, file, _ = bot.py_ref(d)
        if not app_dir or not file:
            return ""
        skill = apptools.read_skill(app_dir)
        name = apptools.skill_file(skill, file)
        if name is None:
            return ""
        says = f" Its SKILL.md: \"{skill['files'][name][:200]}\"" if skill["files"].get(name) else ""
        stem = os.path.basename(app_dir)
        if d.get("risky") is not True and apptools.is_trusted(bot.meta, stem):
            return ""  # whitelisted app: its documented scripts run without a card
        if apptools.is_owned(app_dir, bot.meta.get("builds")):
            if name in skill["approve"]:
                return f"It runs {stem} › {name}, which the app's SKILL.md marks as needing approval.{says}"
            if d.get("risky") is True:
                return f"It runs {stem} › {name}; the bot flagged it as irreversible.{says}"
            return ""
        return f"It runs {stem} › {name} on this Mac, an app this bot did not build.{says}"
    if act not in ("click", "type", "press", "select"):
        return ""
    if d.get("risky") is True:
        return "The bot flagged this as irreversible."
    label = (element(d, obs).get("text") or "").strip()
    if act == "click" and label and RISKY_BTN.search(label):
        return f"The button says \"{label[:40]}\", which usually cannot be undone."
    return ""


def call_key(bot, act: str, d: dict):
    """Identity of a `py`/`tool` call for the one-instruction-one-run rule;
    None for every other action (OpenBot `_call_key`)."""
    d = d or {}
    if act == "py":
        app_dir, file, args = bot.py_ref(d)
        if not app_dir or not file:
            return None
        ref = [app_dir, file, args]
    elif act == "tool":
        app, name, args = apptools.tool_ref(d)
        ref = [app, name, args]
    else:
        return None
    return json.dumps([act, ref], sort_keys=True, default=str, ensure_ascii=False)


# --------------------------------------------------------------- execute ---
def execute(bot, act: str, d: dict, obs: dict) -> tuple[str, str]:
    """Run one tool (OpenBot `_execute`); never raises. Control-flow tools
    (ask, login, offer) are NOT here: each engine owns them."""
    d = d or {}
    b = bot.browser
    try:
        if act == "observe":
            return "observe", format_observation(b.observe(), compact=False)
        if act == "screenshot":
            b.screenshot()
            return "screenshot", "ok, screenshot taken"
        if act == "goto":
            url = d.get("url") or ""
            if not url:
                return "goto", "error: no url"
            info = b.goto(url)
            return f"goto {url}", f"ok, now at {info.get('url')}"
        if act == "click":
            ref, what, at = target(d, obs)
            info = b.click(**at)
            return f"click \"{what}\"", f"ok, now at {info.get('url')}" + (f"; POPUP appeared and was accepted: {info['dialog']}" if info.get("dialog") else "")
        if act == "type":
            text, submit = d.get("text") or "", bool(d.get("submit"))
            ref, what, at = target(d, obs)
            info = b.type(text, ref=ref, submit=submit, x=at["x"], y=at["y"], backend=at.get("backend"))
            return f"type \"{text}\" into {what}" + (" + Enter" if submit else ""), f"ok, now at {info.get('url')}"
        if act == "hover":
            ref, what, at = target(d, obs)
            b.hover(**at)
            return f"hover \"{what}\"", "ok, menu (if any) is open; see the new element list"
        if act == "scroll":
            direction = d.get("direction") or "down"
            if d.get("ref"):
                _, what, at = target(d, obs)
                b.scroll(**at)
                return f"scroll to \"{what}\"", "ok, element is in view"
            b.scroll(direction)
            return f"scroll {direction}", "ok"
        if act == "press":
            key = d.get("key") or d.get("text") or ""
            if not key:
                return "press", "error: no key"
            ref, what, at = target(d, obs)
            info = b.press(key, **at)
            where = f" in \"{what}\"" if ref else ""
            return f"press {key}{where}", f"ok, now at {info.get('url')}"
        if act == "select":
            value = d.get("value") or d.get("text") or ""
            _, what, at = target(d, obs)
            info = b.select(value, **at)
            return f"select \"{value}\" in {what}", f"ok, chose \"{info.get('chosen')}\""
        if act == "read":
            ref, what, at = target(d, obs)
            info = b.read(limit=READ_CAP, **at)
            what = f"\"{what}\"" if ref else "page"
            return f"read {what}", "TEXT:\n" + (info.get("text") or "(empty)")
        if act == "text":
            from fused_render.bots import imessage
            c = bot.contact(d)
            body = (d.get("text") or d.get("message") or "").strip()
            if not c:
                names = ", ".join(l for l, _ in bot.contacts()) or "none"
                return f"text {d.get('to') or '?'}", f"error: not in CONTACTS ({names}); only the user can add contacts (Settings > Advanced > iMessage)"
            if not body:
                return f"text {c[0]}", "error: empty message"
            imessage.send_text(c[1], body)
            return f"text {c[0]} \"{body[:80]}\"", f"sent to {c[0]} ({c[1]})"
        if act == "texts":
            from fused_render.bots import imessage
            c = bot.contact(d)
            if not c:
                names = ", ".join(l for l, _ in bot.contacts()) or "none"
                return f"texts {d.get('to') or '?'}", f"error: not in CONTACTS ({names}); only the user can add contacts (Settings > Advanced > iMessage)"
            try:
                wait_s = min(300, max(0, int(d.get("seconds") or 0)))
            except (TypeError, ValueError):
                wait_s = 0
            rows = imessage.recent_texts(c[1])
            if wait_s:
                last = max([r["rowid"] for r in rows] or [0])
                end = time.time() + wait_s
                got = False
                while time.time() < end and not bot.stop_flag.is_set() and not bot.pause_flag.is_set():
                    new = [r for r in imessage.recent_texts(c[1], after_rowid=last) if not r["me"]]
                    if new:
                        got = True
                        break
                    bot.wake.wait(3)
                rows = imessage.recent_texts(c[1])
                note = "" if got else f"\n(no new reply from {c[0]} in {wait_s} s)"
                return f"texts {c[0]} wait {wait_s}s", imessage.format_texts(c[0], rows[-12:]) + note
            return f"texts {c[0]}", imessage.format_texts(c[0], rows[-12:])
        if act == "tool":
            return bot.run_tool(*apptools.tool_ref(d))
        if act == "py":
            return bot.run_py(d)
        if act == "show":
            return bot.show_app(d.get("name") or d.get("value") or d.get("url") or d.get("text") or "", obs)
        if act == "build":
            return bot.build(d.get("name") or d.get("value") or "", d.get("spec") or d.get("text") or d.get("message") or "",
                             fresh=d.get("new") is True)
        if act == "readfile":
            from fused_render.bots import filereader
            name = d.get("file") or d.get("name") or d.get("text") or ""
            path = bot.resolve_file(name)
            text, img = filereader.read_file(path, d.get("page") or d.get("index") or 1)
            if img is not None:
                # The steps engine (local model) is text-only; the agent engine
                # bypasses this branch (_readfile) and attaches the image.
                text += " (This engine cannot view images; the user can see the file, and you can still `upload` it.)"
            return describe(bot, act, d, obs), text
        if act == "upload":
            name = d.get("file") or d.get("text") or ""
            path = bot.resolve_file(name)
            _, _, at = target(d, obs)
            info = b.upload(path, **at)
            return f"upload {os.path.basename(path)}", f"ok, file attached; check the page for a preview or a Submit button (now at {info.get('url')})"
        if act == "tab":
            sub = (d.get("tab") or d.get("value") or "").lower()
            idx = d.get("index")
            if sub == "new":
                info = b.tab_new(d.get("url") or "about:blank")
                return f"tab new {d.get('url') or ''}".rstrip(), f"ok, now in a new tab at {info.get('url')}"
            if sub == "switch":
                info = b.tab_switch(idx if idx is not None else 0)
                return f"tab switch {idx}", f"ok, now at {info.get('url')}"
            if sub == "close":
                info = b.tab_close(idx)
                return f"tab close {idx if idx is not None else ''}".rstrip(), f"ok, now at {info.get('url')}"
            return "tab", "error: tab needs \"tab\": new|switch|close"
        if act == "back":
            info = b.back()
            return "back", f"ok, now at {info.get('url')}"
        if act == "wait":
            text = d.get("text") or ""
            if text:
                info = b.wait_for(text, 10)
                return f"wait for \"{text}\"", "ok, it appeared" if info.get("found") else "not found after 10 s"
            time.sleep(2)
            b.screenshot()
            return "wait", "ok"
        if act == "save":
            name = d.get("name") or d.get("value") or "result.md"
            body = d.get("text") or d.get("message") or ""
            saved = bot.save_file(name, body)
            return f"save {name}", f"saved {os.path.basename(saved)} to the user's Inbox ({os.path.dirname(saved)}, {len(body)} chars)"
        if act == "remember":
            note = d.get("text") or d.get("message") or ""
            return f"remember \"{note[:120]}\"", bot.remember(note)
        if act == "learn":
            try:
                nm = bot.skill_save(d.get("title") or d.get("name"), d.get("trigger") or d.get("value"),
                                    d.get("steps") or d.get("text") or d.get("message"))
                res = f"saved playbook {nm}"
            except ValueError as e:
                res = f"error: {e}"
            return f"learn \"{(d.get('title') or d.get('name') or '')[:80]}\"", res
        return act or "(none)", "error: unknown action"
    except Exception as e:  # noqa: BLE001 — the model gets the sentence, never a traceback
        return act, f"error: {e}"
