"""The specialists the user keeps: one home folder each under `paths.root()` (PLAN B.1).

The manifest `specialist.json` is the record (id, name, subject, face, model, effort, connections, created,
shipped); `triggers.json` beside it holds "when to bring me in" and is tracked in git because the nightly job edits
it too. Nothing here caches a Specialist: every read is a file read, so several server processes and the nightly
job never disagree.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from fused_render.specialists import home as hm
from fused_render.specialists import paths

logger = logging.getLogger(__name__)

GENERAL = "general"
NAME_MAX = 80
SUBJECT_MAX = 300
MODELS = ("", "sonnet", "opus", "haiku")
EFFORTS = ("", "low", "medium", "high", "xhigh", "max")
_CREATE_LOCK = threading.Lock()


@dataclass
class Specialist:
    id: str
    name: str
    subject: str = ""
    face: dict | str = ""
    model: str = ""
    effort: str = ""
    triggers: dict = field(default_factory=lambda: {"folders": [], "keywords": []})
    connections: list = field(default_factory=list)
    created: float = 0.0
    shipped: str = ""
    home: str = ""

    def manifest(self) -> dict:
        d = asdict(self)
        d.pop("home", None)
        d.pop("triggers", None)
        return d


class Busy(RuntimeError):
    """The specialist cannot be changed that way right now; its message is the sentence the route answers."""


class Invalid(ValueError):
    """A create/update argument the user must fix; its message is the sentence the route answers."""


def _line(v, cap: int) -> str:
    return " ".join(str(v or "").split())[:cap]


def clean_triggers(t) -> dict:
    if t is None:
        return {"folders": [], "keywords": []}
    if not isinstance(t, dict):
        raise Invalid("'triggers' must be an object with 'folders' and 'keywords'")
    folders, keywords = t.get("folders") or [], t.get("keywords") or []
    if not isinstance(folders, list) or not isinstance(keywords, list):
        raise Invalid("'triggers.folders' and 'triggers.keywords' must be lists")
    fo = []
    for f in folders:
        f = os.path.expanduser(str(f or "").strip())
        if not f:
            continue
        if not os.path.isabs(f):
            raise Invalid(f"trigger folder {f!r} must be an absolute path")
        f = os.path.normpath(f)
        if f not in fo:
            fo.append(f)
    kw = []
    for k in keywords:
        k = _line(k, 60)
        if k and k.lower() not in [x.lower() for x in kw]:
            kw.append(k)
    return {"folders": fo[:50], "keywords": kw[:50]}


def _from_home(folder: str) -> Specialist | None:
    m = hm.read_json(os.path.join(folder, paths.MANIFEST), None)
    if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"]:
        return None
    trig = hm.read_json(os.path.join(folder, "triggers.json"), {})
    try:
        trig = clean_triggers(trig if isinstance(trig, dict) else {})
    except Invalid:
        trig = {"folders": [], "keywords": []}
    conns = m.get("connections")
    return Specialist(id=m["id"], name=str(m.get("name") or m["id"]), subject=str(m.get("subject") or ""),
                      face=m.get("face") or "", model=str(m.get("model") or ""), effort=str(m.get("effort") or ""),
                      triggers=trig, connections=[str(c) for c in conns] if isinstance(conns, list) else [],
                      created=float(m.get("created") or 0.0), shipped=str(m.get("shipped") or ""), home=folder)


def all() -> list[Specialist]:  # noqa: A001 — the B.8 name
    found = paths._scan(paths.root())
    out = [s for s in (_from_home(f) for f in found.values()) if s]
    return sorted(out, key=lambda s: (s.created, s.name.lower()))


def any_exist() -> bool:
    """At least one specialist exists. Cheap and never raises (agent.py asks on every task start)."""
    try:
        r = paths.root()
        if not os.path.isdir(r):
            return False
        return any(not n.startswith(".") and os.path.isfile(os.path.join(r, n, paths.MANIFEST))
                   for n in os.listdir(r))
    except OSError:
        return False


def get(spec_id: str) -> Specialist | None:
    if not spec_id or not isinstance(spec_id, str) or spec_id == GENERAL:
        return None
    folder = paths.home(spec_id)
    s = _from_home(folder) if os.path.isdir(folder) else None
    return s if s and s.id == spec_id else None


def find(key: str) -> Specialist | None:
    """By id, then by name (case-insensitive, whitespace folded)."""
    key = (key or "").strip()
    if not key:
        return None
    s = get(key)
    if s:
        return s
    want = " ".join(key.split()).lower()
    return next((s for s in all() if " ".join(s.name.split()).lower() == want), None)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48].strip("-") or "specialist"


def _free_slug(name: str) -> str:
    base = slugify(name)
    r = paths.root()
    slug, n = base, 2
    while os.path.exists(os.path.join(r, slug)):
        slug, n = f"{base}-{n}", n + 1
    return slug


def create(name: str, subject: str, triggers: dict | None = None, shipped: str = "", *,
           charter: str | None = None, face=None) -> Specialist:
    """Make a new home (layout, charter, git history with its first commit) and return the specialist.
    `charter`: the CLAUDE.md text (the seed passes the Canvas one); default the B.7 CHARTER."""
    name = _line(name, NAME_MAX)
    subject = _line(subject, SUBJECT_MAX)
    if not name:
        raise Invalid("A specialist needs a name")
    if name.lower() == GENERAL:
        raise Invalid("'general' is reserved for the general inbox; pick another name")
    trig = clean_triggers(triggers)
    with _CREATE_LOCK:
        if find(name):
            raise Invalid(f"A specialist named {name!r} already exists")
        sid = uuid.uuid4().hex[:8]
        folder = os.path.join(paths.root(), _free_slug(name))
        spec = Specialist(id=sid, name=name, subject=subject, face=face or "", triggers=trig,
                          created=time.time(), shipped=shipped or "", home=folder)
        os.makedirs(folder, exist_ok=False)
        hm.ensure_layout(folder, spec=spec.manifest(),
                         charter=charter if charter is not None else hm.render_charter(name, subject))
        hm.write_json(os.path.join(folder, "triggers.json"), trig)
        paths._reset()
    from fused_render.specialists import history

    history.init(folder, f"create the {name} specialist")
    return spec


def update(spec_id: str, **fields) -> Specialist:
    """Change any of name, subject, face, model, effort, triggers, connections. Triggers are committed (they are
    tracked); manifest fields are committed too, so history shows every Settings save."""
    s = get(spec_id)
    if s is None:
        raise KeyError(spec_id)
    unknown = set(fields) - {"name", "subject", "face", "model", "effort", "triggers", "connections"}
    if unknown:
        raise Invalid(f"unknown field(s): {', '.join(sorted(unknown))}")
    if "name" in fields:
        name = _line(fields["name"], NAME_MAX)
        if not name:
            raise Invalid("A specialist needs a name")
        other = find(name)
        if other and other.id != s.id:
            raise Invalid(f"A specialist named {name!r} already exists")
        if name.lower() == GENERAL:
            raise Invalid("'general' is reserved for the general inbox; pick another name")
        s.name = name
    if "subject" in fields:
        s.subject = _line(fields["subject"], SUBJECT_MAX)
    if "face" in fields:
        f = fields["face"]
        if f is not None and not isinstance(f, (dict, str)):
            raise Invalid("'face' must be an object or a string")
        s.face = f or ""
    if "model" in fields:
        m = str(fields["model"] or "")
        if m not in MODELS and not re.match(r"^claude-[a-z0-9.\-\[\]]+$", m):
            raise Invalid(f"unknown model {m!r}")
        s.model = m
    if "effort" in fields:
        e = str(fields["effort"] or "")
        if e not in EFFORTS:
            raise Invalid(f"effort must be one of {', '.join(x for x in EFFORTS if x)}")
        s.effort = e
    if "connections" in fields:
        c = fields["connections"]
        if not isinstance(c, list):
            raise Invalid("'connections' must be a list")
        s.connections = list(dict.fromkeys(str(x) for x in c if str(x or "").strip()))
    if "triggers" in fields:
        s.triggers = clean_triggers(fields["triggers"])
        hm.write_json(os.path.join(s.home, "triggers.json"), s.triggers)
    hm.write_json(os.path.join(s.home, paths.MANIFEST), s.manifest())
    from fused_render.specialists import history

    history.commit(s.id, "settings: " + ", ".join(sorted(fields)) if fields else "settings",
                   [paths.MANIFEST, "triggers.json"])
    return s


def live_runs(home: str) -> list[dict]:
    """Agent runs with a turn open anywhere in this home, its breakouts' workrooms included:
    `[{run_id, backend, sessions}]` (agent._live_runs_under). [] when the agent module is not importable or
    anything fails: callers use it to refuse or skip, never to crash."""
    try:
        from fused_render.claude_agent import agent_module

        agent = agent_module()
        scan = getattr(agent, "_live_runs_under", None) or agent._live_runs_in
        return list(scan(home))
    except Exception:  # noqa: BLE001
        return []


def main_live_runs(home: str) -> list[dict]:
    """Agent runs with a turn open in the home's own folder: the main chat (breakouts run in their workrooms)."""
    try:
        from fused_render.claude_agent import agent_module

        return list(agent_module()._live_runs_in(home))
    except Exception:  # noqa: BLE001
        return []


def live_run_ids(home: str) -> list[str]:
    return [r["run_id"] for r in live_runs(home) if r.get("run_id")]


def _cancel_live_runs(home: str) -> None:
    """Best effort: stop any agent run whose target is inside this home (main chat, breakouts)."""
    try:
        for run_id in live_run_ids(home):
            try:
                from fused_render.claude_agent import agent_module

                agent_module()._cancel(run_id)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        logger.warning("specialists: could not cancel live runs in %s", home, exc_info=True)


def delete(spec_id: str) -> None:
    """Refused (`Busy`) while its nightly job runs. Move the home to `<root>/.state/trash/<id>-<ts>/` (recoverable by hand, never rm), after cancelling its
    live runs and marking its open breakouts `deleted` so a task that holds one gets a clear answer."""
    s = get(spec_id)
    if s is None:
        raise KeyError(spec_id)
    try:
        from fused_render.specialists import nightly

        busy = bool(nightly.running(spec_id))
    except Exception:  # noqa: BLE001 — stream 3 missing: nothing of it can be running
        busy = False
    if busy:
        raise Busy("The nightly update is running for this specialist; delete it when the update has finished.")
    _cancel_live_runs(s.home)
    bdir = os.path.join(s.home, paths.APP_DIR, "breakouts")
    try:
        names = os.listdir(bdir)
    except OSError:
        names = []
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(bdir, n)
        rec = hm.read_json(p, None)
        if isinstance(rec, dict) and rec.get("status") == "open":
            rec["status"] = "deleted"
            hm.write_json(p, rec)
    trash = os.path.join(paths.state_root(), "trash")
    os.makedirs(trash, exist_ok=True)
    shutil.move(s.home, os.path.join(trash, f"{s.id}-{int(time.time())}"))
    paths._reset()


__all__ = ["Busy", "Specialist", "Invalid", "GENERAL", "all", "any_exist", "get", "find", "create", "update", "delete",
           "clean_triggers", "slugify"]
