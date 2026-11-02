"""Export a bot as one `.fusedbot` file (docs/bots.md §17, D1455).

A `.fusedbot` is the `.fused` container codec (appfile_container) with the
`bot` kind key: the same hardened reader, a different index key, so neither
kind ever half-opens as the other. Members:

    bot.json             the public half, BYTES as on disk (store.meta_path)
    skills/<name>.md     every playbook, sorted
    README.md            when the user wrote one (never generated)
    icon.svg             when the user put one at the folder root (the install page's picture, else the face)
    .fused/memory.md     only when the exporter ticked it (memory is private; default off)
    apps/<folder>.fused  one nested app file per BUNDLED app the exporter chose

Apps travel three ways (`resolve_source`): a starter or a marketplace app is a
REFERENCE in the index (the importer installs it from its own source), only a
hand-made app is bundled. Everything here reads the bot's files from disk,
never a live Bot: the export must work whether or not the registry has that
bot loaded, and a running task's in-memory meta is not what the user sees in
the folder.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone

from fused_render import appfile
from fused_render import appfile_container as container
from fused_render.bots import paths as bpaths
from fused_render.bots import store

log = logging.getLogger(__name__)

PACKAGE_MAX_BYTES = 1 << 30            # 1 GiB total, declared sizes, before anything is written
TEXT_MAX_BYTES = 1 << 20               # memory.md + skills/ together: authored notes, not a data store
EXT = ".fusedbot"

_SNIPPET_MAX = 80

# The fallback secret shapes, used only when app_doctor's floor engine cannot
# load (a wheel without the app-doctor skill). Copied from
# skills/fused-render-app-doctor/ci/app_check.py `_PREFIXED_SECRET_PATTERNS`
# (~line 378) and `_PRIVATE_KEY_RE` (~line 394); the engine stays the one
# source whenever it is present.
_FALLBACK_SECRET_RES = (
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"sk_live_[0-9a-zA-Z]{24,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ sources ---
def resolve_source(app_dir: str) -> dict:
    """How an app reaches another machine: `{"kind": "starter", "key"}` when it was installed from a built-in
    starter (its `.fused/starter.json` names one), `{"kind": "marketplace", "slug", "repo"?, "commit"?}` when the
    contrib store records it as a marketplace copy, else `{"kind": "bundled"}`. A reference is always preferred
    over bundling: the importer gets the app from where it is maintained, so it keeps receiving updates, and the
    package stays small."""
    from fused_render.bots import starters

    rec0 = starters._installed_record(app_dir)
    key = rec0.get("starter") if isinstance(rec0, dict) else None
    if isinstance(key, str) and key:
        return {"kind": "starter", "key": key}
    from fused_render.contrib import store as cstore

    # get_raw, not get(): get() re-binds the record (runs `git config`, may rewrite the store), and plan() runs on a
    # GET that must have no side effects.
    try:
        rec = cstore.get_raw(cstore.key(app_dir))
    except Exception:  # noqa: BLE001 - an unreadable contrib store means "not a marketplace copy", not a failure
        rec = None
    if isinstance(rec, dict) and rec.get("kind") == "marketplace" and isinstance(rec.get("slug"), str) and rec["slug"]:
        out = {"kind": "marketplace", "slug": rec["slug"]}
        if isinstance(rec.get("repo"), str) and rec["repo"]:
            out["repo"] = rec["repo"]
        commit = (rec.get("approved") or {}).get("commit") if isinstance(rec.get("approved"), dict) else None
        if isinstance(commit, str) and commit:
            out["commit"] = commit
        return out
    return {"kind": "bundled"}


# -------------------------------------------------------------- reading the bot ---
def _public(bid: str) -> tuple[str, bytes, dict]:
    """(bot.json path, the bytes that ship, parsed). The bytes are `store.public_json` of the PUBLIC half of the
    merged meta: byte-identical to the file on disk once the split is complete (write_meta writes exactly that
    text), and filtered down to PUBLIC_KEYS when it is not (a bot.json from before the split, or hand-edited with a
    private key in it) — a stranger's package must never carry browser ids, allow rules or folders."""
    try:
        path = store.meta_path(bid)
        meta = store.read_meta(bid)
    except (LookupError, FileNotFoundError):
        raise ValueError(f"no such bot {bid}")
    except ValueError as e:
        raise ValueError(f"this bot's bot.json is not valid JSON: {e}")
    public, _private = store.split_meta(meta)
    public["id"] = bid
    return path, store.public_json(public).encode("utf-8"), public


def _skill_files(bid: str, skipped: list | None = None) -> list[tuple[str, str]]:
    """[(name, abs path)] of `skills/*.md`, sorted. A file that cannot travel (a symlink, a name the container
    refuses, a name that is not valid UTF-8) is skipped and, when `skipped` is given, reported as
    `{"name", "problem"}` so the dialog can say so: one odd file must not fail the whole export, nor vanish silently."""
    d = os.path.join(bpaths.bot_folder(bid), "skills")
    if os.path.islink(d):
        if skipped is not None:
            skipped.append({"name": "skills", "problem": "the skills folder is a symlink; not exported"})
        return []
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".md") and not n.startswith("."))
    except OSError:
        return []
    out = []
    for n in names:
        p = os.path.join(d, n)
        problem = None
        try:
            n.encode("utf-8")
        except UnicodeEncodeError:
            problem = "file name is not valid UTF-8"
        if problem is None and (not os.path.isfile(p) or os.path.islink(p)):
            problem = "not a regular file"
        if problem is None:
            problem = container.path_problem(f"skills/{n}")
        if problem:
            log.warning("bot %s: skill file %r cannot travel in a package; skipped (%s)", bid, n, problem)
            if skipped is not None:
                skipped.append({"name": n, "problem": problem})
            continue
        out.append((n[:-3], p))
    return out


def _read_text(path: str, cap: int = TEXT_MAX_BYTES + 1) -> str:
    """At most `cap` bytes of a text file (plan() runs on every dialog open; an unbounded read of a runaway
    memory.md would be a memory spike for nothing — write() refuses it by size anyway)."""
    try:
        with open(path, "rb") as f:
            return f.read(cap).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _memory_path(bid: str) -> str:
    return os.path.join(bpaths.bot_private(bid), "memory.md")


def _regular(path: str) -> bool:
    """A regular file, not a symlink: nothing exported is ever read through a link."""
    return os.path.isfile(path) and not os.path.islink(path)


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ------------------------------------------------------------------ secrets ---
def _scan(where: str, text: str, hits: list) -> None:
    """Append `{"where", "snippet"}` per likely credential in `text`, using app_doctor's floor engine (the same
    patterns the doctor runs on an app before sharing it). The snippet is the engine's MASKED excerpt with its line
    number, never the raw secret: the export dialog shows it, and a caution line must not itself leak the value."""
    if not text:
        return
    check = None
    try:
        from fused_render import app_doctor

        eng = app_doctor.engine()
        check = getattr(eng, "_check_secrets", None) if eng is not None else None
    except Exception:  # noqa: BLE001 - no doctor engine: the fallback patterns below scan instead
        log.debug("secret scan: app_doctor engine unavailable, using fallback patterns", exc_info=True)
    if check is not None:
        findings: list = []
        try:
            check(where, text, findings)
        except Exception as e:  # noqa: BLE001 - a scan that breaks must SAY so, never read as "no secrets"
            log.exception("secret scan failed for %s", where)
            hits.append({"where": "scan", "snippet": f"scan incomplete for {where}: {e or type(e).__name__}"[:_SNIPPET_MAX]})
            return
        for f in findings:
            hits.append({"where": where, "snippet": f"line {f.get('line', 0)}: {f.get('excerpt', '')}"[:_SNIPPET_MAX]})
        return
    for rx in _FALLBACK_SECRET_RES:
        for m in rx.finditer(text):
            v = m.group(0)
            masked = v[:2] + "*" * max(0, len(v) - 4) + v[-2:] if len(v) > 8 else "*" * len(v)
            hits.append({"where": where, "snippet": f"line {text.count(chr(10), 0, m.start()) + 1}: {masked}"[:_SNIPPET_MAX]})


# --------------------------------------------------------------------- apps ---
def _app_dir(folder) -> str:
    """The app folder under apps_root() for a manifest/request `folder`; ValueError for anything but a plain child
    name, because `folder` arrives in a request body and is joined onto a real path."""
    if not isinstance(folder, str) or not folder or folder.startswith(".") or folder != os.path.basename(folder) \
            or "/" in folder or "\\" in folder or "\x00" in folder:
        raise ValueError(f"not an app folder name: {folder!r}")
    root = bpaths.apps_root()
    p = os.path.join(root, folder)
    # A symlinked app folder would bundle whatever it points at; only a real folder under the apps root travels.
    if os.path.islink(p) or (os.path.exists(p) and not os.path.realpath(p).startswith(os.path.realpath(root) + os.sep)):
        raise ValueError(f"{folder}: not a real folder under the apps root")
    return p


def _bundle_size(app_dir: str) -> tuple[int, str | None]:
    """(bytes, problem) for bundling `app_dir`: the declared size of exactly the members export_app_file would
    write (appfile._iter_app_files), and why it cannot be bundled when it cannot (gone, not a fused app, over the
    app file caps) so the dialog can show the row muted before the user presses Export."""
    from fused_render import app_listing

    if not os.path.isdir(app_dir):
        return 0, "the app folder is gone"
    try:
        if app_listing.app_entry(app_dir) is None:
            return 0, 'not a fused app: no page in it carries <meta name="fused-app">'
    except OSError as e:
        return 0, f"cannot read the app folder: {e}"
    n = total = 0
    try:
        for full, _rel in appfile._iter_app_files(app_dir):
            n += 1
            total += _size(full)
    except (appfile.AppFileError, OSError) as e:
        return 0, str(e)
    if n > appfile.MAX_EXPORT_ENTRIES:
        return total, f"too many files to export ({n} > {appfile.MAX_EXPORT_ENTRIES})"
    if total > appfile.MAX_EXPORT_TOTAL_BYTES:
        return total, f"too large to export ({total} bytes > {appfile.MAX_EXPORT_TOTAL_BYTES})"
    return total, None


def _app_row(folder: str, manifest_row: dict | None) -> dict:
    """One plan row, re-resolved now: the manifest records what the app WAS when it was seeded, but a copy may have
    been switched to a marketplace one or replaced since. A folder that is gone keeps its manifest source when that
    was a reference (a reference needs no folder to travel); a gone bundled app is a problem."""
    app_dir = _app_dir(folder)
    row = manifest_row if isinstance(manifest_row, dict) else {}
    if os.path.isdir(app_dir):
        source = resolve_source(app_dir)
    else:
        prev = row.get("source")
        source = prev if isinstance(prev, dict) and prev.get("kind") in ("starter", "marketplace") else {"kind": "bundled"}
    app_id = row.get("app_id") if isinstance(row.get("app_id"), str) else None
    if app_id is None and os.path.isdir(app_dir):
        from fused_render import app_id as app_identity
        from fused_render import app_listing
        try:
            entry = app_listing.app_entry(app_dir)
            app_id = app_identity.app_id(entry) if entry else None
        except OSError:
            app_id = None
    name = row.get("name") if isinstance(row.get("name"), str) and row.get("name") else folder
    out = {"folder": folder, "name": name, "app_id": app_id, "source": source, "bytes": 0}
    if source.get("kind") == "bundled":
        out["bytes"], problem = _bundle_size(app_dir)
        if problem:
            out["problem"] = problem
        elif app_id is None:
            # export_app_file mints an id into the entry page of an app that has none and no git remote (its first
            # export): a write into the app folder that exporting a BOT triggers, so the dialog discloses it.
            out["mints_app_id"] = not appfile._has_remote(app_dir)
    return out


def _manifest(public: dict) -> dict[str, dict]:
    """folder -> manifest row, in manifest order, dropping rows without a usable folder (a hand-edited bot.json)."""
    out: dict[str, dict] = {}
    rows = public.get("apps")
    for r in rows if isinstance(rows, list) else []:
        if isinstance(r, dict) and isinstance(r.get("folder"), str) and r["folder"] not in out:
            try:
                _app_dir(r["folder"])
            except ValueError:
                continue
            out[r["folder"]] = r
    return out


# --------------------------------------------------------------------- plan ---
def plan(bid: str) -> dict:
    """Everything the export dialog shows before the user presses Export: what always travels, how big memory is,
    each app's travel mode and size (or why it cannot be bundled), and likely secrets in the text that would travel.
    Read-only: nothing is exported or written here."""
    from fused_render.bots.bot import Bot, is_super

    _path, _raw, public = _public(bid)
    name = public.get("name") or bid
    hits: list = []
    _scan("instructions", public.get("instructions") or "", hits)
    for r in public.get("routines") or []:
        if isinstance(r, dict) and isinstance(r.get("task"), str):
            _scan("routines", r["task"], hits)
    skipped: list = []
    skills = []
    skills_bytes = 0  # file sizes, the same count write() holds to TEXT_MAX_BYTES with memory
    for sname, p in _skill_files(bid, skipped):
        skills_bytes += _size(p)
        text = _read_text(p)
        title, trigger, _body = Bot._parse_skill(text)
        skills.append({"name": sname, "title": title or sname, "trigger": trigger})
        _scan(f"skills/{sname}", text, hits)
    folder = bpaths.bot_folder(bid)
    readme = os.path.join(folder, "README.md")
    if _regular(readme):
        _scan("readme", _read_text(readme), hits)
    mpath = _memory_path(bid)
    memory_bytes = _size(mpath) if _regular(mpath) else 0
    if memory_bytes:
        _scan("memory", _read_text(mpath), hits)
    text_problem = None
    if skills_bytes + memory_bytes > TEXT_MAX_BYTES:
        text_problem = f"skills and memory together are {skills_bytes + memory_bytes} bytes; the cap is {TEXT_MAX_BYTES}"
    return {
        "ok": True,
        "name": name,
        "file_name": f"{bpaths.slug(name)}{EXT}",
        "public": public,
        "skills": skills,
        "skills_bytes": skills_bytes,
        "skills_skipped": skipped,
        "memory_bytes": memory_bytes,
        "apps": [_app_row(f, row) for f, row in _manifest(public).items()],
        "secrets": hits,
        # What the caution line covers; bundled app SOURCES are not scanned here (the app doctor is for that).
        "secrets_scope": ["instructions", "memory", "skills", "readme", "routines"],
        "text_problem": text_problem,
        "super": is_super(public),
    }


# -------------------------------------------------------------------- write ---
def _file_name(file_name: str | None, name: str) -> str:
    """A bare `<x>.fusedbot` name inside dest_dir: basename strips any directory a request body could smuggle in,
    leading dots are dropped (no hidden file, no `..`), and a blank name falls back to the plan's default."""
    base = os.path.basename((file_name or "").strip()).lstrip(".").strip()
    if not base:
        base = f"{bpaths.slug(name)}{EXT}"
    return base if base.lower().endswith(EXT) else base + EXT


def write(bid: str, *, memory: bool, apps: list[str], file_name: str | None, dest_dir: str) -> str:
    """Write the bot as a `.fusedbot` under `dest_dir` and return its path. `apps` are the folders the user ticked:
    manifest rows and folders added in the dialog alike, each re-resolved; a bundled one is exported as a nested
    `.fused`, a reference travels as its index row only. Caps are checked on declared sizes BEFORE anything is
    exported, so an over-budget package fails in a second instead of after minutes of compression. Built in a
    scratch dir beside the destination and renamed in once complete: the result path is only ever absent or whole."""
    from fused_render.bots.bot import is_super
    from fused_render.server.routers.appfile import _unique_export_path

    _path, raw, public = _public(bid)
    if is_super(public):
        raise ValueError("Super Bot cannot be exported: it is this machine's own assistant")
    name = public.get("name") or bid
    if not isinstance(apps, list) or not all(isinstance(a, str) for a in apps):
        raise ValueError("apps must be a list of app folder names")

    members: list[tuple[str, str | bytes]] = [("bot.json", raw)]
    text_bytes = 0
    for sname, p in _skill_files(bid):
        members.append((f"skills/{sname}.md", p))
        text_bytes += _size(p)
    readme = os.path.join(bpaths.bot_folder(bid), "README.md")
    if os.path.isfile(readme) and not os.path.islink(readme):
        members.append(("README.md", readme))
    icon = os.path.join(bpaths.bot_folder(bid), "icon.svg")
    if os.path.isfile(icon) and not os.path.islink(icon):
        # The same cap an app file's icon member has: the install page streams it into a card, unbounded it is not.
        if _size(icon) <= appfile.MAX_ICON_BYTES:
            members.append(("icon.svg", icon))
        else:
            log.warning("bot %s: icon.svg is over %d bytes; exported without it", bid, appfile.MAX_ICON_BYTES)
    mpath = _memory_path(bid)
    if memory and _regular(mpath):
        members.append((".fused/memory.md", mpath))
        text_bytes += _size(mpath)
    if text_bytes > TEXT_MAX_BYTES:
        raise ValueError(f"skills and memory are too large to export ({text_bytes} bytes > {TEXT_MAX_BYTES})")

    manifest = _manifest(public)
    rows: list[dict] = []
    bundled: list[str] = []
    added: list[dict] = []
    for folder in dict.fromkeys(apps):  # de-duplicated, order kept
        if folder not in manifest:
            # Added in the dialog ("Add app…"): it has no manifest row whose reference could stand in for a missing
            # folder, so it must be a fused app that is here now, whatever its source.
            _require_app(folder)
        row = _app_row(folder, manifest.get(folder))
        if folder not in manifest:
            added.append(row)
        if row.get("problem"):
            raise ValueError(f"{folder}: {row['problem']}")
        if row["source"].get("kind") == "bundled":
            bundled.append(folder)
        rows.append(row)

    declared = len(raw) + sum(_size(src) for rel, src in members[1:] if isinstance(src, str)) \
        + sum(r["bytes"] for r in rows)
    if declared > PACKAGE_MAX_BYTES:
        raise ValueError(f"this bot is too large to export ({declared} bytes > {PACKAGE_MAX_BYTES})")

    try:
        os.makedirs(dest_dir, exist_ok=True)
        scratch = tempfile.mkdtemp(prefix=".fusedbot-", dir=dest_dir)
    except OSError as e:
        raise ValueError(f"cannot write under {dest_dir}: {e}")
    try:
        by_folder = {r["folder"]: r for r in rows}
        for folder in bundled:
            out = os.path.join(scratch, f"{folder}.fused")
            try:
                written = appfile.export_app_file(os.path.join(bpaths.apps_root(), folder), out)
            except appfile.AppFileError as e:
                raise ValueError(f"could not bundle the app {folder}: {e}")
            members.append((f"apps/{folder}.fused", out))
            # An app's first export mints its identity into the entry page; the index row must carry that id, or the
            # importer could not tell this app from another of the same name.
            if by_folder[folder]["app_id"] is None and isinstance(written.get("app_id"), str):
                by_folder[folder]["app_id"] = written["app_id"]
        now = _utc_now()
        index = {"name": name, "exported_at": now, "origin": {"bot_id": bid, "exported_at": now},
                 "apps": [{k: r[k] for k in ("folder", "name", "app_id", "source")} for r in rows]}
        tmp = os.path.join(scratch, "package" + EXT)
        try:
            container.write(tmp, index, members, kind="bot")
        except container.ContainerError as e:
            raise ValueError(str(e))
        except OSError as e:
            raise ValueError(f"could not write the package: {e}")
        actual = _size(tmp)
        if actual > PACKAGE_MAX_BYTES:  # the declared check above was on sizes BEFORE compression and growth
            raise ValueError(f"this bot is too large to export ({actual} bytes > {PACKAGE_MAX_BYTES})")
        final = _place(tmp, dest_dir, _file_name(file_name, name), _unique_export_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    log.info("bot %s exported to %s (%d app(s) bundled)", bid, final, len(bundled))
    if added:
        _remember_apps(bid, [{k: r[k] for k in ("folder", "name", "app_id", "source")} for r in added])
    return final


def _place(tmp: str, dest_dir: str, file_name: str, unique) -> str:
    """Move the finished package to its final name without ever overwriting: `os.link` fails with EEXIST when a
    racing writer took the name since `unique()` looked (os.replace would silently win that race), so the next free
    name is tried; a filesystem without hard links falls back to a re-checked os.replace."""
    final = unique(dest_dir, file_name)
    for _ in range(20):
        try:
            os.link(tmp, final)
        except FileExistsError:
            final = unique(dest_dir, file_name)
            continue
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise ValueError(f"could not save the package: {e}")
            if os.path.lexists(final):
                raise ValueError(f"refusing to overwrite existing file: {final}")
            try:
                os.replace(tmp, final)
            except OSError as e2:
                raise ValueError(f"could not save the package: {e2}")
            return final
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return final
    raise ValueError(f"could not find a free file name for {file_name} under {dest_dir}")


def _require_app(folder: str) -> None:
    """ValueError naming `folder` unless it is a fused app under apps_root() right now."""
    from fused_render import app_listing

    app_dir = _app_dir(folder)
    try:
        ok = os.path.isdir(app_dir) and app_listing.app_entry(app_dir) is not None
    except OSError:
        ok = False
    if not ok:
        raise ValueError(f"{folder}: there is no fused app with that folder name in your apps")


def _remember_apps(bid: str, rows: list[dict]) -> None:
    """Append apps added in the export dialog to the bot's `apps` manifest, so the next plan() shows them: the dialog
    edits a real field, not a one-off list. Through the live Bot (the POST runs in the owner process): writing bot.json
    behind it would be overwritten by its next save(). The package is already written, so a failure here costs only
    the memory of the addition, never the export."""
    from fused_render.bots import registry

    try:
        b = registry.get(bid)
        with b.lock:  # the task thread mutates and saves meta under this lock too
            cur = b.meta.get("apps") if isinstance(b.meta.get("apps"), list) else []
            have = {r.get("folder") for r in cur if isinstance(r, dict)}
            new = [r for r in rows if r["folder"] not in have]
            if new:
                b.meta["apps"] = [*cur, *new]
                b.save()
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("bot %s: could not add exported apps to its manifest", bid)
