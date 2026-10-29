"""The collaboration timeline (docs/CONTRIB_LOOP.md §16.16): Activity lines
for every step of working on an app together — not only the moves fused
makes in this folder (§16.9), but what the OTHER people did, as soon as
this machine learns it.

STILL ONE MACHINE'S HISTORY. Lines live on this copy's record
(`store.add_activity`) like every other Activity line, so the author, a
contributor and someone who installed from the Community each see their
own side of the same events — but now every side names who did what:

  author        Bob proposed "X" · Bob sent newer edits to "X" · Claude read
                Bob's proposal · Tried it · Asked Bob to update · Accepted /
                Declined (here or on GitHub) · Bob withdrew "X" · Published 3
                edits to GitHub · Got 2 changes from GitHub (Bob: "…") · Sent
                to the Community for review · The review turned it down /
                needs your OK · It is live · It is out of the Community
  contributor   Installed alice's app · alice commented / asked you to
                update · Your proposal no longer fits · New edits were added
                to your proposal on GitHub · alice accepted your proposal
                (when the pull request merged, before the change arrives —
                a Community copy waits for the next release) · alice closed
                it without taking it · it is open again
  Community     Installed from the Community · alice took the app out of the
  copy          Community · it is back

NO NEW POLLING. Every line comes from a read fused already makes: the
proposal refresh (`proposals._ingest`), the author's inbox listing
(`refresh._list_inbox` — background only; the one extra GitHub call is a
`get_pr` for a proposal that LEFT the list without fused closing it), the
Community listing read (`listing._save`), the catalog read
(`market.refresh_approved`), the install (`classify.record_install`) and
the own copy's sync (`ownsync`, `git_upstream`).

DATED WHEN IT HAPPENED, WRITTEN ONCE. A line carries GitHub's own time
(`merged_at`, `created_at`, a comment's time) when there is one, so a
proposal merged an hour before the app was opened sits an hour back in the
list. Every line about a pull request carries a `key` (`pr:12:merged`,
`inbox:12:opened`, …): the store writes a key once, so a refresh that runs
twice, or an Accept here that the next listing sees again, never says it
twice.

READ-ONLY, NEVER RAISES. Activity offers no action on any line, and these
lines move nothing (no backup); a failure to write one is logged and
dropped — a timeline line must never fail the read or the move it
describes.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime

logger = logging.getLogger(__name__)

#: Back-to-back automatic publishes of an own app within this long are one
#: line ("Published 7 edits to GitHub"), not seven (§16.16).
PUBLISH_MERGE_S = 2 * 3600.0
#: How much of a comment or a summary a line quotes.
QUOTE_MAX = 160


def _store():
    from fused_render.contrib import store
    return store


# ------------------------------------------------------------------ words


def iso(value) -> float | None:
    """Epoch seconds for GitHub's ISO time ("2026-10-28T09:00:00Z"), None
    when absent or unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def quoted(text, limit: int = 80) -> str:
    """`“text”`, one line, cut at `limit` — the author's or sender's words."""
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    return f"“{t[:limit - 1]}…”" if len(t) > limit else f"“{t}”"


def _owner(record) -> str:
    repo = (record or {}).get("repo") or ""
    return repo.split("/")[0] if "/" in repo else "the author"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# ------------------------------------------------------------------ writing


def note(root, kind: str, text: str, *, at=None, key=None, actor=None, link=None):
    """One line (`store.add_activity`); the id, or None (no record, already
    written under `key`, or the write failed). Never raises."""
    try:
        return _store().add_activity(root, kind, text, at=at, key=key, actor=actor, link=link)
    except Exception:  # noqa: BLE001 — a timeline line never fails what it describes
        logger.exception("timeline: line %r failed for %s", kind, root)
        return None


def line(kind: str, text: str, *, at=None, key=None, actor=None, link=None) -> dict:
    """A line for a caller writing it inside its own `store.update`
    (`store.append_activity`)."""
    return _store().new_activity(kind, text, at=at, key=key, actor=actor, link=link)


def write(root, lines) -> int:
    """Write `lines` (`line(...)`) in one store update; how many were new."""
    lines = [x for x in lines or [] if isinstance(x, dict)]
    if not lines:
        return 0
    store = _store()
    n = []

    def fn(rec):
        n.extend(1 for x in lines if store.append_activity(rec, x))

    try:
        store.update(root, fn)
    except Exception:  # noqa: BLE001
        logger.exception("timeline: lines failed for %s", root)
    return len(n)


# ------------------------------------------- the contributor's proposal (§16.5)


def proposal_lines(record: dict, before: dict | None, after: dict | None, pr: dict,
                   comments: list | None, *, me: str | None,
                   local_head: str | None) -> list[dict]:
    """What GitHub's answer about the contributor's open proposal says
    happened since the last read (`proposals._ingest`): `before`/`after`
    are the record's proposal around `_apply_pr`, `pr` GitHub's
    (`gh.get_pr`), `comments` its comments when they were read,
    `local_head` what this copy last pushed (`PR_HEAD_REF`). Pure."""
    if not isinstance(after, dict) or not after.get("number"):
        return []
    from fused_render.contrib import gh

    n = after["number"]
    owner = _owner(record)
    title = quoted(after.get("title"))
    url = after.get("url") or pr.get("url") or None
    b = (before or {}).get("cache") or {}
    a = after.get("cache") or {}
    was, now = b.get("state") or "open", a.get("state") or "open"
    out: list[dict] = []
    since = after.get("sent_at") or after.get("opened_at")
    for c in comments or []:
        who = c.get("author") or ""
        at = iso(c.get("at"))
        if not who or gh.same_login(who, me or after.get("user")) or who.lower().endswith(
                "[bot]") or (since is not None and at is not None and at <= since):
            continue
        body = c.get("body") or ""
        asked = gh.ASK_UPDATE_MARKER in body
        words = quoted(body.replace(gh.ASK_UPDATE_MARKER, ""), QUOTE_MAX)
        if asked:
            text = f"{who} asked you to update your proposal" + (f": {words}" if words else ".")
        elif words:
            text = f"{who} commented on your proposal: {words}"
        else:
            continue
        out.append(line("proposal-asked" if asked else "proposal-comment", text, at=at,
                        key=f"pr:{n}:comment:{who.lower()}:{c.get('at') or ''}", actor=who,
                        link=url))
    if was != "merged" and now == "merged":
        who = pr.get("merged_by") or owner
        if me and gh.same_login(who, me):
            text = f"You merged your proposal {title} into {owner}'s app on GitHub."
        else:
            text = f"{who} accepted your proposal {title}."
        kind = (record or {}).get("kind")
        if kind == "marketplace":
            text += f" It reaches you with {owner}'s next Community release."
        elif kind == "github":
            text += " It comes into your copy with the next update."
        mine = bool(me and gh.same_login(who, me))
        out.append(line("proposal-accepted", text, at=iso(pr.get("merged_at") or
                                                          pr.get("closed_at")),
                        key=f"pr:{n}:merged", actor=None if mine else who, link=url))
    elif was != "closed" and now == "closed":
        out.append(line("proposal-declined",
                        f"{owner} closed your proposal {title} without taking it.",
                        at=iso(pr.get("closed_at")),
                        key=f"pr:{n}:closed:{pr.get('closed_at') or ''}", actor=owner,
                        link=url))
    elif was == "closed" and now == "open":
        out.append(line("proposal-reopened", f"Your proposal {title} is open again.",
                        link=url))
    if now == "open" and b.get("mergeable") is not False and a.get("mergeable") is False:
        out.append(line("proposal-no-fit",
                        f"Your proposal no longer fits {owner}'s latest version. "
                        "Update it to fix that.",
                        key=f"pr:{n}:nofit:{a.get('head_sha') or ''}", link=url))
    head = a.get("head_sha")
    if now == "open" and head and b.get("head_sha") and head != b.get("head_sha") \
            and head != local_head:
        out.append(line("proposal-edited",
                        "New edits were added to your proposal on GitHub. "
                        "Update brings them into your copy.",
                        key=f"pr:{n}:head:{head}", link=url))
    return out


# ------------------------------------------------ the author's inbox (§16.6)


def _seen(rec) -> dict | None:
    got = (rec or {}).get("inbox_seen")
    return got if isinstance(got, dict) else None


def inbox_listed(app_dir: str, items: list, *, repo: str | None) -> int:
    """The author's inbox listing just reached GitHub (`refresh._list_inbox`,
    a background worker — network allowed): one line per proposal that is
    new since the last listing (dated when it was opened; the first listing
    ever names every open one), one when a known proposal's head moved
    (the sender sent newer edits), and for one that LEFT the list without
    fused closing it here, what GitHub says became of it (merged on GitHub,
    withdrawn, closed). Remembers the list on the record (`inbox_seen`).
    Answers how many lines were written."""
    store = _store()
    root = store.key(app_dir)
    rec = store.get(root)
    if not isinstance(rec, dict) or not isinstance(items, list):
        return 0
    seen = dict(_seen(rec) or {})
    out: list[dict] = []
    now: dict = {}
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("number"), int):
            continue
        n = it["number"]
        who = it.get("author") or "Someone"
        title = quoted(it.get("title"))
        url = it.get("url") or None
        head = it.get("head_sha")
        was = seen.get(str(n)) if isinstance(seen.get(str(n)), dict) else None
        if was is None:
            out.append(line("inbox-opened", f"{who} proposed {title}.",
                            at=iso(it.get("created_at")), key=f"inbox:{n}:opened",
                            actor=who, link=url))
        elif head and was.get("head") and head != was.get("head") and head != was.get("ours"):
            out.append(line("inbox-updated", f"{who} sent newer edits to {title}.",
                            key=f"inbox:{n}:head:{head}", actor=who, link=url))
        now[str(n)] = {"head": head, "title": it.get("title") or "", "author": who,
                       "url": url, "ours": (was or {}).get("ours")}
    for k, was in seen.items():
        if k in now or not isinstance(was, dict):
            continue
        gone = _left(rec, int(k) if k.isdigit() else None, was, repo)
        if gone is None:
            now[k] = was     # GitHub could not say: ask again next listing
            continue
        out.extend(gone)

    wrote: list = []

    def fn(r):
        # Merge with Claude may have recorded `ours` while this listing ran
        # (`inbox_event("combined")`): keep it, and never credit the sender
        # with that head.
        cur = _seen(r) or {}
        for k, v in now.items():
            got = cur.get(k).get("ours") if isinstance(cur.get(k), dict) else None
            if got:
                v["ours"] = got
        skip = {f"inbox:{k}:head:{v['ours']}" for k, v in now.items() if v.get("ours")}
        wrote.extend(1 for x in out if x.get("key") not in skip
                     and store.append_activity(r, x))
        r["inbox_seen"] = now

    try:
        store.update(root, fn)
    except Exception:  # noqa: BLE001
        logger.exception("timeline: inbox lines failed for %s", root)
        return 0
    return len(wrote)


#: The note `proposals.withdraw` leaves on the pull request it closes.
_WITHDREW = re.compile(r"withdrew this proposal", re.I)


def _left(rec: dict, n: int | None, was: dict, repo: str | None) -> list[dict] | None:
    """What became of proposal `n`, gone from the open list: `[]` when a
    line here already said (Accept, Decline), None when GitHub could not
    be asked."""
    store = _store()
    if n is None or not repo:
        return []
    if store.has_activity(rec, f"inbox:{n}:merged") or store.has_activity(
            rec, f"inbox:{n}:closed"):
        return []
    from fused_render.contrib import gh
    try:
        pr = gh.get_pr(repo, n)
    except Exception:  # noqa: BLE001 — offline, signed out: the next listing asks again
        return None
    who = pr.get("user") or was.get("author") or "Someone"
    title = quoted(pr.get("title") or was.get("title"))
    url = pr.get("url") or was.get("url") or None
    if pr.get("state") == "open":
        return []            # still open, only not for this app's branch any more
    if pr.get("merged"):
        by = pr.get("merged_by")
        me = (gh.identity() or {}).get("login")
        mine = bool(by and me and gh.same_login(by, me))
        if mine:
            text = f"You merged {who}'s proposal {title} on GitHub."
        elif by:
            text = f"{by} merged {who}'s proposal {title} on GitHub."
        else:
            text = f"{who}'s proposal {title} was merged on GitHub."
        return [line("inbox-accepted", text, at=iso(pr.get("merged_at") or pr.get("closed_at")),
                     key=f"inbox:{n}:merged", actor=None if mine else by, link=url)]
    withdrew = False
    try:
        withdrew = any(gh.same_login(c.get("author"), who) and _WITHDREW.search(
            c.get("body") or "") for c in gh.pr_comments(repo, n))
    except Exception:  # noqa: BLE001 — "closed" says enough without the note
        pass
    text = (f"{who} withdrew their proposal {title}." if withdrew
            else f"{who}'s proposal {title} was closed without merging.")
    return [line("inbox-withdrawn" if withdrew else "inbox-closed", text,
                 at=iso(pr.get("closed_at")), key=f"inbox:{n}:closed", actor=who, link=url)]


def _item(app_dir: str, number: int) -> dict:
    """The inbox's cached row for `number`, else what the last listing
    remembered (`inbox_seen`), else {}."""
    try:
        from fused_render.contrib import inbox
        for it in inbox.cached(app_dir) or []:
            if isinstance(it, dict) and it.get("number") == number:
                return it
    except Exception:  # noqa: BLE001
        pass
    was = (_seen(_store().get(app_dir)) or {}).get(str(number))
    return {"author": was.get("author"), "title": was.get("title"), "url": was.get("url")} \
        if isinstance(was, dict) else {}


def inbox_event(app_dir: str, number: int, what: str, *, author: str | None = None,
                head: str | None = None, extra: str = "") -> None:
    """One line for something the author did to proposal `number` here:
    `accepted`, `declined`, `tried` (a Try copy of `head`), `asked`
    (Ask to update), `combined` (Merge with Claude kept, pushed onto the
    proposal as `head` — remembered, so the next listing does not credit
    the sender with it)."""
    it = _item(app_dir, number)
    who = author or it.get("author") or "the sender"
    title = quoted(it.get("title"))
    of = f"{who}'s proposal" + (f" {title}" if title else "")
    url = it.get("url") or None
    texts = {
        "accepted": ("inbox-accepted", f"Accepted {of}.", f"inbox:{number}:merged"),
        "declined": ("inbox-declined", f"Declined {of}." + (f" Your note: {extra}" if extra
                                                             else ""),
                     f"inbox:{number}:closed"),
        "tried": ("inbox-tried", f"Tried {of} in a separate copy.",
                  f"inbox:{number}:try:{head or ''}"),
        "asked": ("inbox-asked", f"Asked {who} to update {of}.", None),
        "combined": ("inbox-merged", f"Combined {of} with your latest version.", None),
    }
    if what not in texts:
        return
    kind, text, key = texts[what]
    # The author's own step: no `actor` (the page marks only someone else's).
    note(app_dir, kind, text, key=key, link=url)
    if what == "combined" and head:
        store = _store()

        def ours(r):
            seen = _seen(r)
            if seen and isinstance(seen.get(str(number)), dict):
                seen[str(number)]["ours"] = head
        store.update(app_dir, ours)


def reviewed(app_dir: str, number: int, pr: dict, result: dict) -> None:
    """Claude finished reading proposal `number` at `result["head_sha"]`."""
    if not isinstance(result, dict) or not result.get("verdict"):
        return
    who = pr.get("user") or "the sender"
    title = quoted(pr.get("title"))
    summary = quoted(result.get("summary"), QUOTE_MAX)
    if result.get("verdict") == "fine":
        text = f"Claude read {who}'s proposal {title}: nothing risky in it."
    else:
        text = f"Claude read {who}'s proposal {title} and thinks it needs a look."
    note(app_dir, "inbox-reviewed", text + (f" {summary}" if summary else ""),
         key=f"inbox:{number}:review:{result.get('head_sha') or ''}",
         link=pr.get("url") or None)


# --------------------------------------------- the author's listing (§18.6)


def listing_lines(old: dict | None, new: dict | None) -> list[dict]:
    """What a Community listing read says happened since the last one
    (`listing._save`, inside its store update): a request sent (or sent
    again), the review's verdict, the listing live, a new version live,
    the listing gone. Pure; nothing for a read that failed."""
    old = old if isinstance(old, dict) else {}
    if not isinstance(new, dict) or new.get("error"):
        return []
    name = new.get("name") or old.get("name") or "Your app"
    out: list[dict] = []
    opr = old.get("pr") if isinstance(old.get("pr"), dict) else {}
    npr = new.get("pr") if isinstance(new.get("pr"), dict) else {}
    num, url = npr.get("number"), npr.get("url") or None
    if num and npr.get("state") == "open":
        if opr.get("number") != num:
            kind = npr.get("kind")
            text = {"remove": f"Asked to take {name} out of the Community.",
                    "update": f"Sent the new version of {name} to the Community for review."
                    }.get(kind, f"Sent {name} to the Community for review.")
            out.append(line("listing-sent", text, key=f"listing:{num}:sent", link=url))
        elif npr.get("head_sha") and opr.get("head_sha") and npr["head_sha"] != opr["head_sha"]:
            out.append(line("listing-sent", f"Sent a newer version of {name} for review.",
                            key=f"listing:{num}:sent:{npr['head_sha']}", link=url))
    orv = old.get("review") if isinstance(old.get("review"), dict) else {}
    nrv = new.get("review") if isinstance(new.get("review"), dict) else {}
    sha = nrv.get("sha") or ""
    moved = orv.get("sha") != nrv.get("sha")
    if num and nrv.get("status") == "failed" and (moved or orv.get("status") != "failed"):
        why = (nrv.get("reasons") or [""])[0]
        out.append(line("listing-failed", f"The Community review turned down {name}."
                        + (f" {why}" if why else ""),
                        key=f"listing:{num}:failed:{sha}", link=url))
    if num and nrv.get("status") == "needs-confirmation" and (
            moved or orv.get("status") != "needs-confirmation"):
        powers = [p.get("detail") for p in nrv.get("new_powers") or [] if isinstance(p, dict)]
        out.append(line("listing-confirm",
                        f"The Community review needs your OK: {name} asks for new powers"
                        + (f" ({'; '.join(powers[:2])})." if powers else "."),
                        key=f"listing:{num}:confirm:{sha}", link=url))
    if num and nrv.get("confirmed") and not (orv.get("confirmed") and not moved):
        out.append(line("listing-confirmed",
                        f"You confirmed {name}'s new powers. A reviewer takes it from here.",
                        key=f"listing:{num}:confirmed:{sha}", link=url))
    orow = old.get("row") if isinstance(old.get("row"), dict) else None
    nrow = new.get("row") if isinstance(new.get("row"), dict) else None
    approved = new.get("approved") if isinstance(new.get("approved"), dict) else {}
    if nrow and nrow.get("commit") and (orow or {}).get("commit") != nrow.get("commit"):
        whats = quoted(approved.get("whats_new"), QUOTE_MAX)
        text = (f"The new version of {name} is live in the Community." if orow
                else f"{name} is live in the Community.")
        out.append(line("listing-live", text + (f" What's new: {whats}" if whats else ""),
                        at=iso(approved.get("at")), key=f"listing:live:{nrow['commit']}"))
    elif orow and nrow is None and old.get("checked_at"):
        out.append(line("listing-out", f"{name} is out of the Community now. Copies people "
                        "installed keep their last reviewed version.",
                        key=f"listing:out:{orow.get('commit') or ''}"))
    return out


# --------------------------------------------- a Community copy (§18.4)


def install_line(record: dict, *, url: str | None = None) -> dict | None:
    """The first line of a copy `classify.record_install` just recorded."""
    kind = (record or {}).get("kind")
    owner = _owner(record)
    repo = (record or {}).get("repo") or ""
    link = f"https://github.com/{repo}" if repo else None
    if kind == "marketplace":
        from fused_render.contrib import market
        ap = record.get("approved") if isinstance(record.get("approved"), dict) else {}
        when = market.release_date(ap.get("at"))
        ver = ap.get("version")
        text = (f"Installed {owner}'s app from the Community"
                + (f", version {ver}" if ver else "")
                + (f" (released {when})" if when else "") + ".")
        return line("installed", text, actor=owner, link=link)
    if kind == "github":
        return line("installed", f"Installed {owner}'s app from GitHub ({repo}). "
                    f"You'll get {owner}'s changes as they publish them.", actor=owner,
                    link=link)
    if kind == "own" and repo:
        return line("installed", f"Installed your app from GitHub ({repo}).", link=link)
    return None


def delisted_line(record: dict) -> dict:
    return line("delisted", f"{_owner(record)} took this app out of the Community. You keep "
                "the version you have; there will be no new releases.",
                actor=_owner(record))


def relisted_line(record: dict) -> dict:
    return line("relisted", f"{_owner(record)} put this app back in the Community.",
                actor=_owner(record))


# --------------------------------------------- an own app's GitHub traffic


def commits(root, a: str | None, b: str | None) -> list[tuple[str, str]]:
    """`[(author name, subject)]` of `a..b`, oldest first, merges left out;
    the "Claude: " prefix of fused's own commits dropped."""
    if not b:
        return []
    from fused_render import git_upstream as gu
    from fused_render.contrib import replay
    r = replay.git(root, "log", "--reverse", "--no-merges", "--format=%an%x00%s",
                   f"{a}..{b}" if a else b)
    if not gu._ok(r):
        return []
    out = []
    for row in gu._out(r).splitlines():
        if "\x00" in row:
            who, subject = row.split("\x00", 1)
            out.append((who.strip() or "Someone", re.sub(r"^Claude: ", "", subject.strip())))
    return out


def commits_words(rows: list[tuple[str, str]], *, by: bool = True) -> str:
    """"Bob: “Fix chart”, “Add legend”; Alice: “Tweak”" (up to three
    subjects, "and N more"); without `by` the subjects alone."""
    shown, groups = rows[-3:] if len(rows) > 3 else rows, {}
    for who, s in shown:
        groups.setdefault(who if by else "", []).append(quoted(s))
    text = "; ".join((f"{w}: " if w else "") + ", ".join(ss) for w, ss in groups.items())
    more = len(rows) - len(shown)
    return text + (f" and {more} more" if more > 0 else "")


def published(root, before: str | None, after: str | None) -> None:
    """The author's own copy just pushed `before..after` to GitHub (an
    automatic publish): one line, folded into the previous publish line when
    that one is recent (PUBLISH_MERGE_S) and continues from where it ended,
    so a working session reads as one line."""
    if not before or not after or before == after:
        return              # no known start: never list the whole history
    store = _store()
    rec = store.get(root) or {}
    lines = [x for x in rec.get("activity") or [] if isinstance(x, dict)]
    last = max(lines, key=lambda x: x.get("at") or 0, default=None)
    fold = None
    if last is not None and last.get("kind") == "published" \
            and isinstance(last.get("span"), list) and len(last["span"]) == 2 \
            and last["span"][1] == before \
            and time.time() - (last.get("at") or 0) < PUBLISH_MERGE_S:
        fold = last
    start = fold["span"][0] if fold else before
    rows = commits(root, start, after)       # git: outside the store lock
    if not rows:
        return
    x = store.new_activity("published", f"Published {_plural(len(rows), 'edit')} to GitHub: "
                           + commits_words(rows, by=False) + ".")
    x["span"] = [start, after]

    def fn(r):
        if fold is not None:
            r["activity"] = [e for e in r.get("activity") or []
                             if not (isinstance(e, dict) and e.get("id") == fold.get("id"))]
        store.append_activity(r, x)

    try:
        store.update(root, fn)
    except Exception:  # noqa: BLE001
        logger.exception("timeline: publish line failed for %s", root)


def pulled(root, before: str | None, after: str | None) -> None:
    """The author's own copy just took `before..after` from GitHub — work
    pushed there by a collaborator with push rights, a merged proposal, or
    the author on another machine: who changed what."""
    if not before or not after or before == after:
        return
    rows = commits(root, before, after)
    if rows:
        note(root, "pulled", f"Got {_plural(len(rows), 'change')} from GitHub: "
             + commits_words(rows) + ".", key=f"pulled:{after}")
