"""Who made this app (contract §10): the people in the history of the
version this copy follows, for the "contributors" line on the app's page.

LOCAL HISTORY ONLY. No network: the list is `git log` of the follow tip (a
GitHub-link copy: the author's latest as last fetched) or, for an app of
your own, of what is published (`origin`'s branch, else HEAD for an app that
was never published). Bob's own unproposed edits are therefore not on it —
he is a contributor once the author's version carries his work, which is
exactly what handoff A4 promises ("Bob's name appears as a contributor").

WHO COUNTS. Commit AUTHORS, never committers (GitHub commits every squash and
rebase merge as `GitHub <noreply@github.com>`). Fused's own identity
(`Fused <apps@fused.io>`, the one app_git and Claude's turns commit with) is
not a person and is left out; so are bots. A GitHub noreply address
(`<id>+<login>@users.noreply.github.com`, or the older `<login>@…`) names
its account exactly, so it becomes a `login` and every commit under that
login counts as one person whatever name was on it. Anyone else is keyed by
email, with the name from their most recent commit.

Cached per (folder, tip): history below a tip never changes, so the same tip
always gives the same answer, and a page polling the app costs one
`rev-parse` once the list is known.
"""
from __future__ import annotations

import logging
import re
import threading

logger = logging.getLogger(__name__)

_FUSED_EMAIL = "apps@fused.io"
_NOREPLY_RE = re.compile(r"^(?:\d+\+)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))@users\.noreply\.github\.com$",
                         re.I)
#: A long-lived app can have tens of thousands of commits; the line on the
#: page names a handful of people. Bounded so one `git log` stays cheap.
_MAX_COMMITS = 20000

_cache: dict[tuple[str, str], list] = {}
_lock = threading.Lock()


def _gu():
    from fused_render import git_upstream
    return git_upstream


def _tip(root: str, record: dict | None) -> str | None:
    """The tip whose history names the contributors (module docstring)."""
    gu = _gu()

    def rev(spec):
        r = gu._run_safe(root, "rev-parse", "--verify", "--quiet", f"{spec}^{{commit}}")
        return gu._out(r) if gu._ok(r) else None

    kind = (record or {}).get("kind")
    if kind in ("github", "marketplace"):
        try:
            from fused_render.contrib import follow as follow_mod
            f = follow_mod.for_record(record)
            tip = follow_mod.tip(root, f) if f is not None else None
        except Exception:  # noqa: BLE001 — a missing follow ref is "unknown yet"
            logger.debug("contributors: no follow tip for %s", root, exc_info=True)
            tip = None
        branch = (record or {}).get("branch")
        return tip or (rev(f"refs/remotes/origin/{branch}") if branch else None)
    branch = (record or {}).get("branch") or gu._current_branch(root)
    return (rev(f"refs/remotes/origin/{branch}") if branch else None) or rev("HEAD")


def parse(log_text: str) -> list[dict]:
    """`[{login, name, commits}]` from `git log --format=%an%x09%ae` output,
    most commits first (ties by name). Pure, so it is testable without git."""
    people: dict[str, dict] = {}
    for line in (log_text or "").splitlines():
        name, _, email = line.partition("\t")
        name, email = name.strip(), email.strip()
        if not email or email.lower() == _FUSED_EMAIL:
            continue
        m = _NOREPLY_RE.match(email)
        login = m.group(1) if m else None
        if (login or "").lower().endswith("[bot]") or "[bot]" in name.lower():
            continue
        key = f"login:{login.lower()}" if login else f"email:{email.lower()}"
        row = people.get(key)
        if row is None:
            # git log is newest first: the first name seen is the latest one.
            people[key] = {"login": login, "name": name or login or email, "commits": 1}
        else:
            row["commits"] += 1
    return sorted(people.values(), key=lambda r: (-r["commits"], r["name"].lower()))


def list(app_dir: str) -> list[dict]:  # noqa: A001 — the contract's name
    """The app's contributors, `[{"login": str|None, "name", "commits"}]`,
    most commits first. Empty when the folder is not a repo or has no tip
    yet. Never raises, never touches the network."""
    try:
        from fused_render.contrib import store
        root = store.key(app_dir)
        record = store.get(root)
        tip = _tip(root, record)
        if not tip:
            return []
        with _lock:
            hit = _cache.get((root, tip))
        if hit is not None:
            return [dict(r) for r in hit]
        gu = _gu()
        r = gu._run_safe(root, "log", f"--max-count={_MAX_COMMITS}", "--no-merges",
                    "--format=%an%x09%ae", tip)
        if not gu._ok(r):
            return []
        rows = parse(gu._out(r))
        with _lock:
            _cache[(root, tip)] = rows
            if len(_cache) > 256:
                _cache.pop(next(iter(_cache)))
        return [dict(x) for x in rows]
    except Exception:  # noqa: BLE001 — a contributors line is decoration
        logger.exception("contributors: listing failed for %s", app_dir)
        return []
