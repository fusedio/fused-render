"""The channel contract (docs/bots.md §10).

A bot has ONE conversation (its events.jsonl). A channel is a surface that
conversation reaches a person on: the web page (always), iMessage, and
whatever comes next. A channel is a small adapter: it pulls texts in
(`poll`) and pushes one message out (`send`); routing, bot resolution,
delivery policy, option numbering and prefixes live once, in `router.py`.

    Via      {"kind": "imessage", "addr": "+15551234567"}  where a user message came from;
             stamped on the `user` event and, through `bot.task_via`, on every event
             of the task it started. The web is {"kind": "web", "addr": ""}; a routine's
             task carries {"kind": "routine", "addr": ""} so policy can tell it apart; a
             task Super Bot handed to a bot carries {"kind": "handoff", "addr": <Super Bot id>}
             (docs §11).
    Inbound  one message a channel received, before any routing.
    Caps     what the surface can show; the engines and the router read it.
    Door     Super Bot is the one bot a channel reaches (`Channel.door`, docs §10).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import time

WEB_KIND = "web"
ROUTINE_KIND = "routine"
HANDOFF_KIND = "handoff"
WEB = {"kind": WEB_KIND, "addr": ""}
ROUTINE = {"kind": ROUTINE_KIND, "addr": ""}

# Events a channel may carry out of the conversation. Thoughts, actions and
# notes are the page's business, and so are approval cards: approvals are
# answered at the Mac only (docs §10), never by text.
OUT_ROLES = ("done", "question", "error")


LABELS = {"imessage": "iMessage", "botsend": "botsend", WEB_KIND: "the web page", ROUTINE_KIND: "a routine",
          HANDOFF_KIND: "Super Bot"}


def label(kind: str) -> str:
    """The user-facing name of a channel kind."""
    return LABELS.get(kind or WEB_KIND, kind or WEB_KIND)


def via(kind: str, addr: str = "") -> dict:
    return {"kind": str(kind or WEB_KIND), "addr": str(addr or "")}


def is_web(v) -> bool:
    return not v or (v.get("kind") or WEB_KIND) == WEB_KIND


class Texted(str):
    """A mid-task message that arrived over a phone channel: a plain str for
    every reader of `bot.inbox`, plus the `via` it came by. Approval and offer
    waits never take one as their verdict (approvals are answered at the Mac,
    docs §10); it still reaches the model as an instruction."""
    via: dict = {}

    def __new__(cls, text: str, v: dict | None = None):
        s = super().__new__(cls, text)
        s.via = dict(v or {})
        return s


def is_texted(s) -> bool:
    return isinstance(s, Texted)


@dataclass(frozen=True)
class Caps:
    """What a surface can render. The web has everything; a phone has text."""
    options: bool = True      # can show choice buttons (else the router numbers them)
    buttons: bool = True      # can show approval / offer cards
    max_len: int = 0          # 0 = unlimited; else the router shortens and points at the app
    media: bool = True        # can show images / app cards
    can_login: bool = True    # the user can reach the bot's browser window from here


WEB_CAPS = Caps()


@dataclass
class Inbound:
    addr: str                 # normalised sender (channel-specific form)
    text: str
    service: str = ""         # e.g. "iMessage" / "SMS" for Messages rows
    ts: float = field(default_factory=time.time)
    ext_id: str = ""          # the channel's own id for the message (chat.db rowid)


class Channel:
    """Base class. Subclasses set `kind` and `caps` and implement the four methods.
    Everything here is called from the router's threads, never from a task thread."""
    kind: str = ""
    caps: Caps = WEB_CAPS

    def poll(self) -> list[Inbound]:
        """New messages since the last call. Raise to report a broken channel
        (the router shows the error in Settings and retries)."""
        return []

    def send(self, addr: str, text: str, event: dict | None = None) -> None:
        """Deliver one message. Raise on failure."""
        raise NotImplementedError

    def status(self) -> dict:
        """What Settings shows: {running, error, last_in, last_out, …}."""
        return {}

    def identity(self) -> dict:
        """Who the bot speaks as on this surface: {"mode": "own"|"dedicated"|"", "label": str}."""
        return {"mode": "", "label": ""}

    def door(self) -> tuple[str | None, str]:
        """(Super Bot's id, its owner address on this surface) or (None, ""):
        Super Bot is the one bot a channel reaches (docs §10), and only from the
        address set on it. Resolved from disk on every call so a Settings save
        takes effect without a restart."""
        return None, ""

    def start(self) -> None:
        """Called once by the router before the first poll."""

    def stop(self) -> None:
        """Called once by the router on shutdown."""


_SUMMARY_LINE = re.compile(r"(?:^|\n)[ \t]*\**summary\**[ \t]*:\**[ \t]*(.+?)[ \t]*\Z", re.I | re.S)


def split_summary(text: str, summary: str = "") -> tuple[str, str]:
    """(message, summary). The model may hand the phone-sized version as a field
    (`summary`) or as a trailing `SUMMARY: …` line of the message (the agent
    engine's final message has no fields); the line is lifted out of the message
    either way so the web does not show it twice."""
    text = (text or "").rstrip()
    summary = " ".join((summary or "").split())
    m = _SUMMARY_LINE.search(text)
    if m:
        found = " ".join(m.group(1).split())
        text = text[:m.start()].rstrip()
        summary = summary or found
    return text, summary[:1000]


def prompt_section(v: dict | None, caps: Caps, kind_label: str = "") -> str:
    """The CHANNEL paragraph for the model when the task came from a surface
    other than the web. Empty for web and routine tasks; a hand-off gets its own
    short paragraph (docs §11)."""
    if is_web(v) or (v or {}).get("kind") == ROUTINE_KIND:
        return ""
    if (v or {}).get("kind") == HANDOFF_KIND:
        return ("HAND-OFF: Super Bot (the user's assistant on this Mac) gave you this task for the user. Your final "
                "message goes back to it as your result, so put the concrete findings in it. The user answers your "
                "`ask`, `login` and approval cards here at the Mac, in your own chat; that may take a while.\n\n")
    label = kind_label or (v or {}).get("kind") or "another channel"
    lines = [f"CHANNEL: the user sent this task from {label} and is reading your replies there, on a phone, not at the Mac."]
    if caps.max_len:
        lines.append(f"- Your `done` goes out as a text. Give it a `summary` (or end your final message with a line `SUMMARY: …`): "
                     f"the answer in one or two plain sentences, under {caps.max_len} characters, no Markdown. The full message "
                     "(details, lists, links) shows on the Mac; only the summary is texted. Same for `ask`: a short `summary` of the question.")
    if not caps.options:
        lines.append("- Every `ask` carries 2-5 short `options`; the user answers with a number or a word. Never ask an open question you could make a choice.")
    if not caps.buttons:
        lines.append("- No `offer` on this channel: build straight away when asked, otherwise finish the task.")
    if not caps.can_login:
        lines.append("- `login` still works, but the user must walk to the Mac to sign in; say so in its message and expect a wait.")
    if not caps.media:
        lines.append("- Screenshots and app cards do not reach this channel: describe in words, give links.")
    return "\n".join(lines) + "\n\n"
