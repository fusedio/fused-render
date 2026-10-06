"""Channels: the surfaces a bot's one conversation reaches a person on
(docs/bots.md §10). `base` is the contract, `router` the policy, one module
per channel kind beside them. `registry.start()` builds the router with every
channel this platform has; nothing here starts on import.

The engines use three helpers from here, so a task thread never touches a
channel object: `caps_for(via)`, `prompt_for(bot)` and `login_text(bot, q)`."""
from __future__ import annotations

import sys

from fused_render.bots.channels import base
from fused_render.bots.channels.base import WEB_CAPS, Caps

KIND_LABELS = {"imessage": "iMessage", "botsend": "a local script (botsend)", "web": "the web page"}


def available() -> list:
    """The channel objects this platform can run (iMessage needs macOS)."""
    out = []
    if sys.platform == "darwin":
        from fused_render.bots.channels.imessage import ImessageChannel
        out.append(ImessageChannel())
    return out


def caps_for(via) -> Caps:
    """What the surface a task came from can show (web for anything unknown)."""
    kind = (via or {}).get("kind") or base.WEB_KIND
    if kind == "imessage":
        from fused_render.bots.channels.imessage import CAPS
        return CAPS
    return WEB_CAPS


def prompt_for(bot) -> str:
    """The CHANNEL paragraph for this task's prompt ('' for web and routine tasks)."""
    v = getattr(bot, "task_via", None)
    return base.prompt_section(v, caps_for(v), KIND_LABELS.get((v or {}).get("kind") or "", ""))


def origin_label(bot) -> str:
    """The 'task from …' phrase in the YOU line of both engines."""
    v = getattr(bot, "task_via", None) or {}
    kind = v.get("kind") or base.WEB_KIND
    if kind == base.ROUTINE_KIND or getattr(bot, "task_origin", "manual") == "routine":
        return "routine (user may be away)"
    if kind == base.WEB_KIND:
        return "chat"
    return f"{KIND_LABELS.get(kind, kind)} (user is on their phone, replies are texted back)"


def login_text(bot, q: str) -> str:
    """The question shown for a `login`: the web user clicks into the window;
    a phone user has to come to the Mac."""
    if caps_for(getattr(bot, "task_via", None)).can_login:
        return (f"{q} I've opened a real browser window for you — sign in there "
                "(your password manager and passkeys work normally), then reply 'done' or "
                "click Hand back when you're finished.")
    return (f"{q} I've opened a browser window on the Mac: come sign in there when you can "
            "(your password manager and passkeys work normally), then reply 'done'. I'll wait.")
