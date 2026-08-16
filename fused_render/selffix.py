"""Self-fix — a Claude session on the app's OWN installation, and the mark it leaves.

When something fails on an end user's machine (a download that errors, a job
that dies with a traceback), the download manager offers one more option beside
Dismiss: **try to fix it here**. That opens a Claude Code session whose working
directory is this installed copy of fused-render, with the failure written down
beside it, and lets the user watch it work in the explorer's chat sidebar.

Everything in this module exists because of what that leaves behind: **an
installation that is no longer the one we shipped.** Three facts follow, and
they are the whole design.

**1. The mark belongs to the INSTALL, not to the user.** A per-user file in
``~/.fused-render`` would be a lie in both directions: a second account on the
same machine runs the same modified bytes and would see a clean badge, and a
user who reinstalls would keep the badge for an install that no longer has the
change in it. So the state dir is ``<install root>/.fused-render-selffix`` —
inside the tree a reinstall replaces. That placement is not bookkeeping, it is
the mechanism: **replacing the installation removes the mark, with no uninstall
hook anyone has to remember to run.**

**2. …but "the tree was replaced" is not something we may merely assume**, so
two independent checks back it up, each covering what the other cannot:

  * a **version stamp** in the marker. `pip uninstall` only removes the files
    its RECORD lists, so a marker this app wrote can outlive an upgrade that
    replaced every file around it. A marker whose ``version`` is not the running
    ``__version__`` describes an installation that is gone: it is deleted on
    sight (`status`), with no digest and no I/O beyond the read.
  * a **content digest**, reconciled once per process start (`reconcile`). That
    is what catches the case the version stamp cannot see — a *same-version*
    reinstall, the repair install someone does precisely because the app is
    behaving oddly. If the tree now hashes back to the pristine baseline the
    marker recorded, the modification is gone and so is the marker.

**3. Only a self-fix session ever sets the mark.** There is deliberately no
continuous verification, for a reason and a cost. The reason: what is being
recorded is *"Claude changed this installation while fixing something, here is
its report"* — a provenance claim — and not *"these bytes differ from the
release"*, which is an integrity claim we cannot honestly make without shipping
a signed per-file manifest. The cost: a modification made some other way (a
developer's own edit, a half-finished `pip install`) is not marked, which is the
right silence — a source checkout rebuilds `static/shell-dist` on every watch
tick, and a badge that lit up for that would mean nothing anywhere else.

No import of anything under ``fused_render.server`` — the router imports this;
keep it acyclic.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sys
import threading
import time

import fused_render
from fused_render import __version__

logger = logging.getLogger("fused_render.selffix")

# The state dir, INSIDE the install tree (see the module docstring). Dot-led so
# it is never importable as a package and never shows in an ordinary listing.
STATE_DIR_NAME = ".fused-render-selffix"
MARKER_NAME = "modified.json"
BASELINE_NAME = "baseline.json"
REPORTS_DIR = "reports"
INCIDENTS_DIR = "incidents"

SCHEMA = 1

# How many fix entries the marker keeps. One session per incident and an
# incident is rare, so this is a runaway guard rather than a budget; the oldest
# entries drop first, and their report FILES stay on disk either way (the panel
# lists the directory, not just the marker).
MAX_FIXES = 20

# Files and dirs the digest ignores. Byte-caches are written by the act of
# importing, so a tree that has merely been RUN must hash the same as one that
# has not — otherwise every install is "modified" the moment it starts. The
# state dir excludes itself for a sharper version of the same reason: writing
# the incident file that a fix session reads would otherwise be a modification
# of the installation, recorded by the very run that made it.
_SKIP_DIRS = {"__pycache__", STATE_DIR_NAME}
_SKIP_SUFFIXES = (".pyc", ".pyo")
_SKIP_NAMES = {".DS_Store"}

# Where a user goes to get a clean copy. The download page is the one door that
# is right for every platform; the releases page is where a wheel's URL lives.
DOWNLOAD_URL = "https://render.fused.io"
RELEASES_URL = "https://github.com/fusedio/fused-render/releases/latest"
ISSUES_URL = "https://github.com/fusedio/fused-render/issues/new"

_lock = threading.Lock()


# ------------------------------------------------------------------- locations


def install_root() -> str:
    """The folder a fix session opens on: this installed `fused_render` package.

    The package dir and NOT its parent, which is `site-packages` (or, in the
    packaged mac app, `Contents/Resources/lib/python3.12`) — a folder holding
    every third-party dependency the app ships. Handing an agent that as its
    working directory invites a "fix" inside pydantic, which is neither ours to
    change nor something a report could usefully describe. This tree is exactly
    the code we wrote, plus the templates and the built shell we ship with it.
    """
    return os.path.dirname(os.path.abspath(fused_render.__file__))


def state_dir() -> str:
    return os.path.join(install_root(), STATE_DIR_NAME)


def marker_path() -> str:
    return os.path.join(state_dir(), MARKER_NAME)


def baseline_path() -> str:
    return os.path.join(state_dir(), BASELINE_NAME)


def writable() -> bool:
    """Whether a fix session could actually change anything here.

    Asked BEFORE the session starts, because the alternative is an agent that
    spends several minutes reading code it can never edit and then reports a
    fix it did not apply. A DMG dragged to /Applications by the user is owned by
    that user and writable; a copy an admin installed for everyone is not.
    """
    return os.access(install_root(), os.W_OK)


# ---------------------------------------------------------------------- digest


def _file_digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 18), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_digest(root: str | None = None) -> str:
    """sha256 over the (relative path, content) pairs of the install tree.

    Same construction as `core_templates._tree_digest`, and deliberately the
    same choice: **content, never mtimes or sizes**. A reinstall rewrites every
    mtime without changing a byte, and a hand edit that swaps two characters
    changes neither size nor — on a coarse filesystem clock — necessarily the
    mtime either. Each entry contributes `<relpath>\\0<file sha256>\\0`, so a
    rename is as visible as an edit.

    An unreadable file folds `<unreadable>` in rather than raising: a tree we
    cannot fully read must not hash equal to one we can.
    """
    root = install_root() if root is None else root
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            if name in _SKIP_NAMES or name.endswith(_SKIP_SUFFIXES):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            try:
                h.update(_file_digest(full).encode("ascii"))
            except OSError:
                h.update(b"<unreadable>")
            h.update(b"\0")
    return h.hexdigest()


# ------------------------------------------------------------------- json i/o


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        # Absent, unreadable, or garbage. All three mean the same thing to every
        # caller — there is no usable record — and none of them may raise out of
        # a status read that /api/config makes on every poll.
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: str, data: dict) -> None:
    """Atomic-ish write: a reader must never see half a marker.

    `os.replace` on the same directory, like every other store in this codebase.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# -------------------------------------------------------------------- baseline


def ensure_baseline(*, now: float | None = None) -> dict:
    """The pristine digest this installation started from, computed once.

    Taken when the FIRST fix session on this version starts — the last moment
    the tree is still what we shipped, and the only moment we get: nothing runs
    at install time, so there is no earlier hook to take it in. A baseline
    stamped with a different version is re-taken rather than trusted, because an
    upgrade legitimately replaced the tree it described.

    Returned rather than merely written so the caller can put the digest in the
    marker without a second walk.
    """
    now = time.time() if now is None else now
    with _lock:
        existing = _read_json(baseline_path())
        if existing and existing.get("version") == __version__ and existing.get("digest"):
            return existing
        record = {
            "schema": SCHEMA,
            "version": __version__,
            "digest": tree_digest(),
            "at": now,
        }
        try:
            _write_json(baseline_path(), record)
        except OSError:
            # A read-only installation cannot be fixed in place either, so this
            # is not the failure that matters — `writable()` is what the caller
            # checks. Carry the digest back in memory regardless: it still makes
            # the in-session comparison work for the life of this process.
            logger.info("could not write the self-fix baseline", exc_info=True)
        return record


# ---------------------------------------------------------------------- marker


def _public(marker: dict) -> dict:
    """The wire shape: the marker, plus the absolute paths only we can resolve.

    Report paths are stored RELATIVE to the state dir and absolutised here. The
    install root moves — a bundle is dragged from the DMG to /Applications, a
    venv is relocated — and a marker holding absolute paths would then point at
    a directory that is not this one.
    """
    root = state_dir()
    fixes = []
    for fix in marker.get("fixes") or []:
        if not isinstance(fix, dict):
            continue
        entry = dict(fix)
        for key in ("report", "incident"):
            rel = entry.get(key)
            entry[key] = (os.path.normpath(os.path.join(root, rel))
                          if isinstance(rel, str) and rel else None)
        fixes.append(entry)
    latest = fixes[-1] if fixes else None
    return {
        "modified": True,
        "version": marker.get("version"),
        "install_root": install_root(),
        "state_dir": root,
        "first_modified_at": marker.get("first_modified_at"),
        "modified_at": marker.get("modified_at"),
        "fixes": fixes,
        # Named separately rather than left to the client to index: "the report
        # to open" is a decision (the newest one), and the version chip is not
        # the place to make it twice.
        "latest_report": latest.get("report") if latest else None,
    }


def status() -> dict | None:
    """What the shell shows on the version chip, or None for an unmodified install.

    Cheap by construction — one small JSON read, no walk — because /api/config
    carries this and the shell polls /api/config every few seconds.

    The one thing it does beyond reading is **delete a marker left by a version
    that is no longer installed**. That check has to live on the read path and
    not only in `reconcile`: an upgrade may well be the very thing that fixes
    the machine, and the badge has to be gone the moment the new version serves
    its first request — not after the next restart, and not after a walk of the
    tree that a config poll must never pay for.
    """
    path = marker_path()
    marker = _read_json(path)
    if not marker:
        return None
    if marker.get("version") != __version__:
        logger.info("clearing a self-fix marker left by version %s",
                    marker.get("version"))
        _discard(path)
        return None
    return _public(marker)


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def clear() -> bool:
    """Forget the modification. Returns whether there was a marker to forget.

    The baseline stays: it describes the release, not the modification, and
    keeping it means a LATER fix session on the same version still knows what
    pristine looked like without re-walking a tree that is no longer pristine.
    """
    with _lock:
        path = marker_path()
        if not os.path.exists(path):
            return False
        _discard(path)
        return True


def mark_modified(*, run_id: str = "", session_id: str = "", report: str = "",
                  incident: str = "", title: str = "", digest: str = "",
                  baseline_digest: str = "", now: float | None = None) -> dict:
    """Record that a fix session changed this installation. Idempotent per run.

    Upsert keyed on `run_id`, because the caller stamps REPEATEDLY: the watcher
    checks the tree every few ticks so the badge appears while the user is still
    watching the session work, rather than only once the turn ends. Appending
    per call would leave one conversation showing as a column of fixes.

    `report`/`incident` are absolute paths on the way in and stored relative to
    the state dir — see `_public` for why.
    """
    now = time.time() if now is None else now
    root = state_dir()

    def rel(path: str) -> str:
        if not path:
            return ""
        try:
            return os.path.relpath(path, root).replace(os.sep, "/")
        except ValueError:  # different drive on Windows — keep it absolute
            return path

    with _lock:
        marker = _read_json(marker_path()) or {}
        if marker.get("version") != __version__:
            # A marker from a version that is gone describes an installation
            # that is gone. Start a fresh one rather than appending this fix to
            # a history that was about different bytes.
            marker = {}
        fixes = [f for f in (marker.get("fixes") or []) if isinstance(f, dict)]
        entry = next((f for f in fixes if f.get("run_id") == run_id and run_id), None)
        if entry is None:
            entry = {"at": now, "run_id": run_id}
            fixes.append(entry)
        entry["session_id"] = session_id or entry.get("session_id") or ""
        entry["title"] = title or entry.get("title") or ""
        if report:
            entry["report"] = rel(report)
        if incident:
            entry["incident"] = rel(incident)
        entry["updated_at"] = now

        record = {
            "schema": SCHEMA,
            "version": __version__,
            "first_modified_at": marker.get("first_modified_at") or now,
            "modified_at": now,
            "baseline_digest": baseline_digest or marker.get("baseline_digest") or "",
            "digest": digest or marker.get("digest") or "",
            "fixes": fixes[-MAX_FIXES:],
        }
        _write_json(marker_path(), record)
        return _public(record)


def settle(*, run_id: str = "", session_id: str = "", report: str = "",
           incident: str = "", title: str = "", now: float | None = None) -> bool:
    """Compare the tree against its baseline and mark it modified if it moved.

    THE decision point of the whole feature, and it is made by this process
    rather than by the model. A session that is asked to stamp its own work is
    a session that can forget to — and the one thing this feature must not do is
    leave an installation quietly carrying somebody's patch. So the model is
    asked only for the REPORT (which nobody else can write) and the app decides
    for itself whether anything changed.

    Returns whether the installation is modified.
    """
    baseline = ensure_baseline(now=now)
    current = tree_digest()
    if current == baseline.get("digest"):
        return False
    mark_modified(run_id=run_id, session_id=session_id, report=report,
                  incident=incident, title=title, digest=current,
                  baseline_digest=str(baseline.get("digest") or ""), now=now)
    return True


def reconcile() -> None:
    """Once per process start: has this installation been put back?

    Only ever does work when a marker exists, which is the rare case — so the
    ordinary start pays one `stat`. When it does run, it answers the question
    `status()` deliberately cannot: a **same-version reinstall**. That is not an
    exotic case, it is the obvious thing a user does when the app is misbehaving
    ("just install it again"), and the version stamp cannot see it because the
    version did not change.

    Never raises: this runs on a startup thread, and a badge that is a few bytes
    out of date is not a reason to fail a boot.
    """
    try:
        marker = _read_json(marker_path())
        if not marker:
            return
        if marker.get("version") != __version__:
            _discard(marker_path())
            return
        baseline = (_read_json(baseline_path()) or {})
        pristine = baseline.get("digest") if baseline.get("version") == __version__ else None
        if not pristine:
            return
        current = tree_digest()
        if current == pristine:
            logger.info("self-fix: the installation matches the released tree again "
                        "— clearing the modified marker")
            clear()
        elif current != marker.get("digest"):
            # Still modified, but not the way the marker last described. Keep
            # the record honest so the "restored" test above stays meaningful.
            with _lock:
                marker["digest"] = current
                marker["modified_at"] = time.time()
                try:
                    _write_json(marker_path(), marker)
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — startup housekeeping, never fatal
        logger.debug("self-fix reconcile failed", exc_info=True)


def start_reconcile() -> None:
    """`reconcile` on a daemon thread — it walks the tree, so never inline."""
    threading.Thread(target=reconcile, daemon=True,
                     name="fused-render-selffix-reconcile").start()


# ------------------------------------------------------------------- incidents


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime(now))


def _iso(now: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))


def machine_facts() -> dict:
    """What a developer reading the report needs and cannot ask the user for."""
    return {
        "app_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "packaged": bool(getattr(sys, "frozen", None)),
        "install_root": install_root(),
    }


def record_incident(context: dict, *, now: float | None = None) -> tuple[str, str]:
    """Write the failure down, and pre-create the report the session must fill in.

    Returns (incident path, report path).

    **The report file is created HERE, already holding the incident**, rather
    than left for the model to create. Two reasons, and the second is the real
    one: a version chip that promises a report must always have a file to open,
    and the thing a developer most needs — what actually failed, on what machine
    — is known now and is not something a summarising model should be trusted to
    copy back accurately. The session rewrites this file; if it never gets that
    far, what survives is still the most useful half.

    Both files live in the state dir, which the digest ignores — so writing them
    is not itself a modification of the installation.
    """
    now = time.time() if now is None else now
    incidents = os.path.join(state_dir(), INCIDENTS_DIR)
    reports = os.path.join(state_dir(), REPORTS_DIR)
    os.makedirs(incidents, exist_ok=True)
    os.makedirs(reports, exist_ok=True)
    # Second resolution, plus a suffix when that is not enough. A user with two
    # failed rows clicks Fix on both in the same second more often than the
    # timestamp suggests — and the collision would not be a duplicate file, it
    # would be the SECOND session overwriting the first session's report while
    # that one was still being written to.
    stamp = _stamp(now)
    suffix = 0
    while os.path.exists(os.path.join(reports, f"{stamp}.md")) or os.path.exists(
            os.path.join(incidents, f"{stamp}.md")):
        suffix += 1
        stamp = f"{_stamp(now)}-{suffix}"
    incident = os.path.join(incidents, f"{stamp}.md")
    report = os.path.join(reports, f"{stamp}.md")

    facts = machine_facts()
    title = str(context.get("title") or "").strip() or "an unnamed failure"
    lines = [
        f"# Incident — {title}",
        "",
        f"- **When**: {_iso(now)}",
        f"- **fused-render**: v{facts['app_version']}"
        f"{' (packaged app)' if facts['packaged'] else ''}",
        f"- **Python**: {facts['python']}",
        f"- **Platform**: {facts['platform']}",
        f"- **Installation**: `{facts['install_root']}`",
        "",
        "## What the user saw",
        "",
    ]
    for label, key in (("Operation", "title"), ("Detail", "detail"),
                       ("State", "state"), ("Kind", "kind"),
                       ("Reported by", "page"), ("Job id", "job_id"),
                       ("Where", "source")):
        value = str(context.get(key) or "").strip()
        if value:
            lines.append(f"- **{label}**: {value}")
    message = str(context.get("message") or "").strip()
    if message:
        lines += ["", "## Error", "", "```", message, "```"]
    lines += ["", "This file was written by fused-render when the user asked for a "
              "local fix. It is the input to the session; the session's own "
              f"account of what it did goes in `{REPORTS_DIR}/{stamp}.md`.", ""]
    incident_text = "\n".join(lines)

    with open(incident, "w", encoding="utf-8") as f:
        f.write(incident_text)
    with open(report, "w", encoding="utf-8") as f:
        f.write(_report_stub(stamp, incident_text))
    return incident, report


def _report_stub(stamp: str, incident_text: str) -> str:
    """The report as it exists before the session has written a word of it."""
    return (
        f"# Self-fix report — {stamp}\n"
        "\n"
        "> **Not written yet.** fused-render created this file when the user "
        "asked for a local fix and handed it to a Claude Code session running "
        "on this installation. If it still reads like this, that session did "
        "not get as far as reporting — the incident it was given is below, and "
        "is worth sending to the Fused Render developers either way.\n"
        "\n"
        "---\n"
        "\n" + incident_text
    )


def list_reports() -> list[dict]:
    """Every report on disk, newest first — the panel's list.

    The DIRECTORY, not the marker's `fixes`: the marker is capped and a report
    file is the artefact, so a listing that could go missing under a cap would
    be the one thing this feature is not allowed to lose.
    """
    reports = os.path.join(state_dir(), REPORTS_DIR)
    out = []
    try:
        names = os.listdir(reports)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".md"):
            continue
        full = os.path.join(reports, name)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        out.append({"path": full, "name": name, "at": stat.st_mtime,
                    "size": stat.st_size})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


# ---------------------------------------------------------------- the session


# The permission mode the fix session runs in. NOT "auto" (the mode the app
# scaffolder uses, routers/apps.py), and the difference is the whole posture of
# this feature: that session writes a new folder in the user's workspace, this
# one edits the application itself. A user who asked for a local fix has not
# thereby agreed to let a model rewrite their installation unwatched — so every
# tool call that the CLI's classifier does not consider safe parks a permission
# card, and the user answers it in the sidebar they are already looking at.
#
# That is affordable here in a way it is not for the scaffolder precisely
# because THIS session is opened in front of the user by the same click that
# starts it. Nothing is unattended.
FIX_PERMISSION_MODE = "prompt"


def fix_prompt(incident: str, report: str) -> str:
    """What the fix session is told. Written as instructions to a colleague who
    has never seen this machine and cannot ask the user anything.

    Three things it has to establish, and each has a failure it is there to
    prevent: WHERE it is (an installed copy, not a checkout — so "run the test
    suite" and "open a PR" are not available moves), WHAT IT MAY TOUCH (an
    agent that wanders up into `site-packages` or down into the app bundle's
    signed binaries breaks the app far worse than the bug it came to fix), and
    that THE REPORT IS THE DELIVERABLE (the fix helps one machine; the report is
    the only thing that can help everyone else).
    """
    root = install_root()
    return f"""\
You are fixing a problem in the fused-render installation on this machine.

The user hit a failure in the app and chose "Fix this locally". What happened is
written down here — read it first:

  {incident}

## Where you are

Your working directory is this machine's INSTALLED copy of fused-render:

  {root}

That is the app's own Python package inside the installation. It is not a source
checkout: there is no test suite here, no git history, no way to open a pull
request, and no frontend build. What you change here changes the app running on
this one machine, right now, and nowhere else.

## What to do

1. Read the incident file, then trace the failure through the code in this
   folder until you can name the cause. Say "I could not find it" rather than
   guessing — a wrong patch to an installation is worse than no patch.
2. If there is a small, safe fix, apply it here. Smallest change that fixes the
   reported failure; no refactors, no drive-by cleanups, no dependency changes.
3. Rewrite the report at

     {report}

   It already contains the incident. Replace it with your own account, keeping
   these sections:

     ## What went wrong        the failure, in one short paragraph
     ## Cause                  the actual mechanism, with file:line references
     ## What I changed         every file you edited and why; "nothing" if so
     ## How to verify          what the user should do to confirm it worked
     ## For the developers     what the real fix looks like upstream

   Write it for a Fused Render developer who has never seen this machine. It is
   the only thing that travels back to them.

## Rules

- Only edit files under {root}. Never anything outside it: not the user's own
  files, not other Python packages beside this one, not the app bundle's
  binaries, frameworks or Info.plist.
- Only edit source and text — .py, .html, .css, .json, .md, and template
  assets. Never a compiled artefact (.so, .dylib, .pyc) and never the built
  frontend under static/shell-dist/: that is minified build output, patching it
  by hand is not a fix anyone can carry upstream.
- Do not install, upgrade or remove packages, do not run the app's updater, and
  do not touch ~/.claude or ~/.fused-render.
- Python changes need the app to be restarted before they take effect. Say so in
  the report if you changed any.
- If the right fix cannot be made here — it needs a new release, a rebuilt
  frontend, or a change outside this folder — change nothing and say that in the
  report. That is a good outcome, not a failure.

fused-render is watching this folder while you work. If you change anything in
it, the app marks the installation as modified and shows the user a badge on the
version number that leads to your report. So write the report even if you fixed
nothing: an unexplained modified install is the one outcome this must not leave
behind.
"""


# ------------------------------------------------------------------- reinstall


def install_method() -> str:
    """How this copy got here: "brew" | "dmg" | "windows" | "linux" | "source" | "pip".

    Decides the reinstall instructions, which are the panel's other half — a
    badge that says "this app has been modified" and cannot tell you how to get
    an unmodified one is only half an answer.
    """
    if getattr(sys, "frozen", None) == "macosx_app":
        from fused_render.update import mac as mac_update

        # Reuses the updater's own probe so "how do I reinstall" and "how do I
        # update" can never disagree about what manages this bundle. It shells
        # out to brew, which is why nothing on the /api/config path calls this.
        return "brew" if mac_update.detect_method(mac_update.bundle_path()) == "brew" else "dmg"
    if getattr(sys, "frozen", None):
        return "windows" if sys.platform == "win32" else "linux"
    # A checkout is recognised by the repo ABOVE the package — an editable
    # install and a plain `python -m fused_render` from a clone look identical
    # from in here, and both want git's answer rather than pip's.
    if os.path.isdir(os.path.join(os.path.dirname(install_root()), ".git")):
        return "source"
    return "pip"


def reinstall_advice() -> dict:
    """How to get a clean copy of the latest version, for THIS kind of install.

    Every branch ends in the same promise, which the caller renders once: the
    replacement removes the state dir along with the tree, so reinstalling
    clears the badge. That is a property of where the marker lives (module
    docstring), not of anything the installer does on our behalf.

    **`command` empty means the link IS the instruction**, and the panel styles
    it as the section's primary action rather than as a footnote. That is the
    DMG case — the most common end-user install, where "reinstall" means "go to
    the download page and drag it over" and there is nothing to type. Wording
    the link is this function's job too (`url_label`): the branches already say
    everything else per method, and a raw URL printed as the only call to
    action reads as a citation, not as a button.
    """
    method = install_method()
    if method == "brew":
        from fused_render.update import mac as mac_update

        return {
            "method": method,
            "headline": "Reinstall with Homebrew",
            "command": f"brew reinstall --cask {mac_update.CASK_NAME}",
            "note": "Run it in a terminal, then quit and reopen fused-render. "
                    "Homebrew manages this copy, so the app never swaps it out "
                    "on its own.",
            "url": DOWNLOAD_URL,
            "url_label": "Download page",
        }
    if method == "dmg":
        return {
            "method": method,
            "headline": "Reinstall from the latest DMG",
            "command": "",
            "note": "Download the DMG and drag FusedRender.app into "
                    "Applications, replacing the copy that is there. Then quit "
                    "and reopen it.",
            "url": DOWNLOAD_URL,
            "url_label": "Download the latest version",
        }
    if method in ("windows", "linux"):
        return {
            "method": method,
            "headline": "Reinstall the latest build",
            "command": "",
            "note": "Download the latest installer and run it over this "
                    "install, then restart fused-render.",
            "url": DOWNLOAD_URL,
            "url_label": "Download the latest version",
        }
    if method == "source":
        return {
            "method": method,
            "headline": "Restore this checkout with git",
            "command": f"git -C {os.path.dirname(install_root())} status",
            "note": "This is a source checkout, so the fix is in your working "
                    "tree — review it, keep it or revert it. The badge clears "
                    "once the tree matches what the release ships.",
            "url": RELEASES_URL,
            "url_label": "Latest release",
        }
    return {
        "method": "pip",
        "headline": "Reinstall the wheel",
        "command": "pip install --force-reinstall --no-cache-dir <wheel-url>",
        "note": "Take the wheel URL from the latest release's notes. "
                "--force-reinstall is what replaces the files that were "
                "changed here.",
        "url": RELEASES_URL,
        "url_label": "Latest release notes",
    }


def snapshot() -> dict:
    """Everything the badge's panel needs, in one read.

    Deliberately NOT what /api/config carries: `install_method` shells out to
    brew and `list_reports` walks a directory, and the config poll runs every
    few seconds on every route. The chip's presence is a config field; its
    contents are this endpoint, fetched when the user opens the panel.
    """
    state = status()
    return {
        "modified": state is not None,
        "version": __version__,
        "install_root": install_root(),
        "writable": writable(),
        "marker": state,
        "reports": list_reports(),
        "reinstall": reinstall_advice(),
        "issues_url": ISSUES_URL,
        "machine": machine_facts(),
    }
