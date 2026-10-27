"""The Marketplace listing's buttons and sentence (docs/CONTRIB_LOOP.md §18.6)
for an own app with a GitHub repo — the listing half of `actions.py`'s one
place that decides which buttons exist.

`decide(listing, *, signed_in, refreshing)` is pure — `Listing` dict in,
`{state, sentence, actions}` out — and is what BOTH callers draw from:
`buttons(f, b)` adds its listing ids to the Source panel's `_Buttons` inside
`actions._own` (one line there, WS-ENGINE's), and `routers/listing.py`
answers it whole for the page's ListingStrip and re-derives it on every
POST to refuse an id the state no longer offers. Local only: the listing is
`listing.view` (the cached read + local git), never a GitHub call, because
both callers serve page GETs.

NO MAGIC (§18.0 M6): every state has a sentence and at least one button —
In review (Check again), Live (Publish update when the app moved on, Remove,
Check again), Needs your confirmation (Confirm), Failed (Fix with Claude,
Send the new version / Send again, Check again), not listed (Publish to
Marketplace). A publish the gates would refuse is not offered; the gate's
own fix is, in its place: Publish now (the local tip isn't on GitHub) or
Open App Doctor (a sharing row fails) — and Sign in to GitHub when signed
out. Those three are route-only ids: the own panel already has its own
Publish now / Sign in, so `buttons(f, b)` adds only `LISTING_IDS`.

NEVER RAISES. `buttons`/`sentence` run inside every own app's panel read;
a broken listing must cost the listing buttons, never the panel.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

#: The ids the frontend posts to `/api/contrib/listing-action` (never to
#: `/api/contrib/action`, which does not know them).
LISTING_IDS = ("publish-market", "publish-update", "confirm-powers", "fix-with-claude",
               "check-listing", "remove-market")
#: Ids the listing route also offers in place of a gated publish.
ROUTE_IDS = ("publish-now", "open-doctor", "sign-in")

REMOVE_CONFIRM = ("{name} leaves the Community. People who installed it keep their copy "
                  "on its last reviewed version.")
#: Said while the index's check runs without an Anthropic key (WS-IDX: the
#: block carries `"claude": "skipped"`).
CLAUDE_OFF = "Claude's review is off; only the fixed rules ran."
CONFIRM_POWERS = ("{name} will be allowed to: {powers}. Everyone who installs this version "
                  "gets it.")


def _btn(id_: str, label: str, *, primary=False, confirm=None, danger=False,
         dialog=None) -> dict:
    a = {"id": id_, "label": label, "primary": bool(primary)}
    if confirm:
        a["confirm"] = confirm
    if danger:
        a["danger"] = True
    if dialog:
        a["dialog"] = dialog
    return a


def _join(items: list[str]) -> str:
    items = [x.rstrip(".") for x in items if isinstance(x, str) and x.strip()]
    return "; ".join(items)


def decide(lst: dict, *, signed_in: bool = True, refreshing: bool = False) -> dict:
    """`{state, sentence, actions}` for `listing.view`'s answer. Pure."""
    from fused_render.contrib import listing as L

    name = lst.get("name") or "This app"
    acts: list[dict] = []
    extra: list[str] = []
    if not signed_in:
        return {"state": "signed-out",
                "sentence": f"Sign in to GitHub to publish {name} to the Community.",
                "actions": [_btn("sign-in", "Sign in to GitHub", primary=True,
                                 dialog="sign-in")]}
    if not lst.get("checked_at"):
        if refreshing:
            return {"state": "pending", "sentence": f"Checking the Community for {name}…",
                    "actions": []}
        return {"state": "unknown",
                "sentence": f"fused hasn't checked the Community for {name} yet.",
                "actions": [_btn("check-listing", "Check the Community", primary=True)]}

    def gates_ok() -> bool:
        """Publish is offered only when it would not be refused; the
        refusal's own fix is offered in its place."""
        if lst.get("unpushed"):
            extra.append("Your latest changes aren't on GitHub yet; the Community lists "
                         "what is on GitHub.")
            acts.append(_btn("publish-now", "Publish now", primary=True))
            return False
        if not lst.get("doctor_clear", True):
            extra.append(L._doctor_sentence(lst.get("doctor_blockers") or []))
            acts.append(_btn("open-doctor", "Open App Doctor", primary=True, dialog="doctor"))
            return False
        return True

    review = lst.get("review") or {}
    status = review.get("status")
    pr = lst.get("pr") or {}
    listed = bool(lst.get("listed"))
    removing = pr.get("kind") == "remove" and pr.get("state") == "open"
    if status == "needs-confirmation":
        state = "needs-confirmation"
        powers = _join([p.get("detail") for p in review.get("new_powers") or []
                        if isinstance(p, dict)]) or "do new things"
        sentence = (f"{name} passed the review, and this version adds new abilities: "
                    f"{powers}. It goes live when you confirm.")
        acts.append(_btn("confirm-powers", "Confirm", primary=True,
                         confirm=CONFIRM_POWERS.format(name=name, powers=powers)))
        acts.append(_btn("check-listing", "Check again"))
    elif status == "failed":
        state = "failed"
        reasons = _join(review.get("reasons") or [])
        closed = pr.get("state") == "closed"
        verb = "remove" if pr.get("kind") == "remove" else "publish"
        sentence = (f"The request to {verb} {name} was closed "
                    "on GitHub before it went live." if closed
                    else f"The review turned down {name}: {reasons}.")
        moved = bool(lst.get("pushed_tip") and pr.get("commit")
                     and lst["pushed_tip"] != pr["commit"])
        if not closed:
            acts.append(_btn("fix-with-claude", "Fix with Claude", primary=not moved))
        if pr.get("kind") == "remove":
            if listed:
                acts.append(_btn("remove-market", "Remove from Community", danger=True,
                                 confirm=REMOVE_CONFIRM.format(name=name)))
        elif listed or pr.get("commit"):
            if gates_ok():
                acts.append(_btn("publish-update",
                                 "Send the new version" if moved else "Send again",
                                 primary=moved or closed))
        elif gates_ok():
            acts.append(_btn("publish-market", "Publish to Community", primary=True,
                             dialog="listing"))
        acts.append(_btn("check-listing", "Check again"))
    elif status == "in-review":
        state = "removing" if removing else "in-review"
        sentence = (f"Removing {name} from the Community. About 30 minutes." if removing
                    else f"Fused is reviewing {name}. About 30 minutes.")
        if review.get("confirmed"):
            sentence += " You confirmed its new abilities."
        acts.append(_btn("check-listing", "Check again", primary=True))
    elif listed:
        state = "live"
        when = L.day((lst.get("approved") or {}).get("at"))
        sentence = (f"{name} is in the Community, release of {when}." if when
                    else f"{name} is in the Community.")
        if lst.get("update_available"):
            sentence += " Your app has changed since."
            if gates_ok():
                acts.append(_btn("publish-update", "Publish update", primary=True))
        acts.append(_btn("remove-market", "Remove from Community", danger=True,
                         confirm=REMOVE_CONFIRM.format(name=name)))
        acts.append(_btn("check-listing", "Check again"))
    else:
        state = "not-listed"
        sentence = f"{name} isn't in the Community yet."
        if gates_ok():
            acts.append(_btn("publish-market", "Publish to Community", primary=True,
                             dialog="listing"))
        acts.append(_btn("check-listing", "Check again"))
    if review.get("claude_skipped") and state in ("in-review", "needs-confirmation", "failed"):
        extra.append(CLAUDE_OFF)
    if lst.get("error"):
        extra.append(lst["error"])
    seen, out, primary = set(), [], False
    for a in acts:  # one per id, at most one primary, primary first
        if a["id"] in seen:
            continue
        seen.add(a["id"])
        if a["primary"] and primary:
            a = {**a, "primary": False}
        primary = primary or a["primary"]
        out.append(a)
    out.sort(key=lambda a: not a["primary"])
    return {"state": state, "sentence": " ".join([sentence, *extra]).strip(), "actions": out}


# -- the Source panel's half ---------------------------------------------------------


def _enabled() -> bool:
    from fused_render.contrib import listing
    return listing._enabled()


def _root(f: dict) -> str | None:
    """The app folder the facts are about: `f["app_dir"]`, else the own
    record naming `f["repo"]` whose folder exists."""
    root = f.get("app_dir")
    if isinstance(root, str) and root:
        return root
    from fused_render.contrib import store
    for k, rec in store.find_by_repo(f.get("repo") or ""):
        if isinstance(rec, dict) and rec.get("kind") == "own" and os.path.isdir(k):
            return k
    return None


def _applies(f: dict) -> str | None:
    if not isinstance(f, dict) or f.get("kind") != "own" or not f.get("repo"):
        return None
    if not (f.get("github") or {}).get("signed_in") or not _enabled():
        return None
    return _root(f)


def _decided(f: dict) -> dict | None:
    root = _applies(f)
    if root is None:
        return None
    from fused_render.contrib import listing
    return decide(listing.view(root), signed_in=True)


def buttons(f: dict, b) -> None:
    """Add the listing's buttons (`LISTING_IDS` only) to `actions._own`'s
    `_Buttons` `b`. Only for an own app with a repo, signed in, with the
    Marketplace switch on. Never raises."""
    try:
        decided = _decided(f)
        for a in (decided or {}).get("actions") or []:
            if a["id"] not in LISTING_IDS:
                continue
            b.add(a["id"], a["label"], primary=a.get("primary", False),
                  confirm=a.get("confirm"), danger=a.get("danger", False),
                  dialog=a.get("dialog"))
    except Exception:  # noqa: BLE001 — the listing must never blank the panel
        logger.warning("contrib actions: listing buttons failed", exc_info=True)


def sentence(f: dict) -> str | None:
    """The listing's status sentence for the own panel, or None when the
    listing does not apply. Never raises."""
    try:
        decided = _decided(f)
        return decided.get("sentence") if decided else None
    except Exception:  # noqa: BLE001
        logger.warning("contrib actions: listing sentence failed", exc_info=True)
        return None
