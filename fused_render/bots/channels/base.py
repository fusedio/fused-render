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
             task Super Bot handed to a bot carries {"kind": "handoff", "addr": "<Super Bot id>:<hand-off id>"}
             (docs §11; handoff_via). A task an app page sent through `fused.bots`
             carries {"kind": "app", "addr": "<app key>:<call id>"} (docs §13; app_via):
             the key names the app folder, the call id is unique per send, so the page
             reads only its own turn's events. The Fused Render iOS app's Bots screens
             carry {"kind": "phone", "addr": "<paired device id>"} (docs §14; phone_via):
             the user's own paired device, outside like a text, but it may settle the
             approval card it shows (the route stamps `answers`). Policy reads only the kind.
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
APP_KIND = "app"
PHONE_KIND = "phone"  # the iOS app's Bots screens over the LAN listener (docs §14); "imessage" is a text
WEB = {"kind": WEB_KIND, "addr": ""}
ROUTINE = {"kind": ROUTINE_KIND, "addr": ""}

# Events a channel may carry out of the conversation. Thoughts, actions and
# notes are the page's business. An approval card goes out too, but only to the
# sender of the phone-started task it belongs to (the origin rule in
# Router.targets), as a numbered text that sender may answer; the answer counts
# for that one card only (`answers_card`, docs §10). Mac-started tasks never text.
OUT_ROLES = ("done", "question", "approval", "error")

# The two choices a texted approval card is numbered with (Router.render).
APPROVAL_OPTIONS = ("Approve", "Deny")


def relay_of(ev) -> dict | None:
    """The card another bot waits on, when `ev` is Super Bot's relay of it (docs §11
    relay): {"bot": <target id>, "seq": <that card's seq>, "kind": "approval" |
    "question", "options": [...]}; None for every other event. A reply to the
    relayed card answers the TARGET's card, through the router, never Super Bot."""
    r = (ev or {}).get("relay")
    return r if isinstance(r, dict) and r.get("bot") and r.get("seq") is not None else None


def approvals_by_text(meta) -> bool:
    """Super Bot's "Approve by text" switch (Settings > Phone, `imessage_approvals`):
    a phone-started task's approval cards are texted and the reply may settle
    them. A bot.json without the key reads as on."""
    m = meta or {}
    return "imessage_approvals" not in m or bool(m.get("imessage_approvals"))


LABELS = {"imessage": "iMessage", "botsend": "botsend", WEB_KIND: "the web page", ROUTINE_KIND: "a routine",
          HANDOFF_KIND: "Super Bot", APP_KIND: "an app", PHONE_KIND: "the phone"}


def label(kind: str) -> str:
    """The user-facing name of a channel kind."""
    return LABELS.get(kind or WEB_KIND, kind or WEB_KIND)


def via(kind: str, addr: str = "") -> dict:
    return {"kind": str(kind or WEB_KIND), "addr": str(addr or "")}


def is_web(v) -> bool:
    return not v or (v.get("kind") or WEB_KIND) == WEB_KIND


def is_local(v) -> bool:
    """A task the user set up at the Mac: typed in the chat (web) or scheduled
    there (a routine). The one predicate behind Super Bot's "started here" tests
    (bot.start_task, tools.phone_super, agent_engine.super_mode): a routine is the
    user's own task, so it runs with Super Bot's own settings; only an outside
    sender (a text) is forced to ask."""
    return is_web(v) or (v or {}).get("kind") == ROUTINE_KIND


def is_outside(v) -> bool:
    """A message from a sender that is not the user at the Mac: a text, a botsend
    file, an app page, the iOS app (PHONE_KIND). Such a message is never the verdict of an app offer and,
    unless the router stamped it for the one card it answers (`answers_card`),
    not of an approval card either (docs §10); it still reaches the model as an
    instruction or as the answer to its own `ask`. The negation of is_local,
    minus the hand-off (Super Bot's own task, judged by its own rules, docs §11)."""
    return not is_local(v) and (v or {}).get("kind") != HANDOFF_KIND


def app_via(app_key: str, call_id: str) -> dict:
    """The via of ONE app send: addr "<app key>:<call id>" (docs §13). The key is
    the app folder's normalised name (apptools.norm_app); the call id is minted
    per send, so a turn's watcher reads only its own task's events and a later
    send never lends its events to an earlier result (the hand-off rule)."""
    return via(APP_KIND, f"{app_key}:{call_id}")


def app_key_of(v) -> str:
    """The app key an app via carries ("" for any other via)."""
    v = v or {}
    if v.get("kind") != APP_KIND:
        return ""
    return str(v.get("addr") or "").split(":", 1)[0]


def phone_via(device_id: str) -> dict:
    """The via of a message sent from the iOS app's Bots screens (docs §14): addr
    is the paired device's id (lan.py stamps X-Fused-Device from the pairing
    record; "" on loopback). Outside like a text (is_outside), so Super Bot runs
    it under the phone posture."""
    return via(PHONE_KIND, device_id)


def handoff_via(super_id: str, handoff_id: str) -> dict:
    """The via of ONE hand-off's task: addr "<Super Bot id>:<hand-off id>", unique per
    hand-off so a watcher reads only its own task's events (docs §11). Policy reads the kind."""
    return via(HANDOFF_KIND, f"{super_id}:{handoff_id}")



class Outside(str):
    """A mid-task message from an outside sender (is_outside: a phone channel, a
    botsend file, an app page): a plain str for every reader of `bot.inbox`,
    plus the `via` it came by and, from an app, the `images` sent with it (each
    {"media_type", "data": base64}; the agent engine pushes them into the model
    turn beside the text, docs §13). Offer waits never take one as their verdict;
    an approval wait only when the router stamped `via["answers"]` with that
    card's seq (`answers_card`: the card was texted to this sender and the bot
    still waited on it, docs §10). Otherwise it reaches the model as an
    instruction or as the answer to its own `ask`."""
    via: dict = {}
    images: list = []

    def __new__(cls, text: str, v: dict | None = None, images=None):
        s = super().__new__(cls, text)
        s.via = dict(v or {})
        s.images = list(images or [])
        return s


Texted = Outside  # the older name: a text from the phone was the first outside sender


def is_outside_msg(s) -> bool:
    return isinstance(s, Outside)


is_texted = is_outside_msg


def answers_card(s, seq) -> bool:
    """True when an outside message was stamped as the answer to the approval card
    `seq`: by the router (Router.dispatch: the card was texted to this sender and
    the bot still waits on it), or by the phone route (bots/phone.py: the iOS app
    showed this very card, docs §14). The one way an outside yes or no is a verdict."""
    return seq is not None and (getattr(s, "via", None) or {}).get("answers") == seq


def images_of(s) -> list:
    """The image blocks an inbox message carries ([] for a plain str)."""
    return list(getattr(s, "images", None) or [])


class Forever(str):
    """An approval answered with the card's "don't ask again" button (the web
    page's /send with `forever: true`): a plain yes for every reader of
    `bot.inbox`, plus the flag the gate reads to write an allow rule
    (tools.add_allow_rule). Never set from a phone channel: the flag lives on
    the message, not in its words, so a typed "approve forever" stays a plain yes."""

    def __new__(cls, text: str):
        return super().__new__(cls, text)


def is_forever(s) -> bool:
    return isinstance(s, Forever)


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


def prompt_section(v: dict | None, caps: Caps, kind_label: str = "", approvals: bool = False) -> str:
    """The CHANNEL paragraph for the model when the task came from a surface
    other than the web. Empty for web and routine tasks; a hand-off gets its own
    short paragraph (docs §11). `approvals`: the user may answer an approval card
    from that surface (the "Approve by text" switch, docs §10)."""
    if is_web(v) or (v or {}).get("kind") == ROUTINE_KIND:
        return ""
    if (v or {}).get("kind") == APP_KIND:
        return (f"APP: the app {app_key_of(v)!r} on this Mac sent this task from its own page and reads your final "
                "message as its result (data read by code, so put the concrete findings in it, plainly). Any images it "
                "sent are in this message. The user is at the Mac: `ask`, `login` and approval cards are answered in "
                "your chat here (the app may answer an `ask` too); that may take a while.\n\n")
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
        if approvals:
            lines.append("- Approval cards reach the user as a text they answer with a number: never tell them to come to the Mac "
                         "for one; take the step and wait for the answer.")
        else:
            lines.append("- Approval cards are answered at the Mac only: say so when a step needs one and expect a wait.")
    if not caps.can_login:
        lines.append("- `login` still works, but the user must walk to the Mac to sign in; say so in its message and expect a wait.")
    if not caps.media:
        lines.append("- Screenshots and app cards do not reach this channel: describe in words, give links.")
    return "\n".join(lines) + "\n\n"
