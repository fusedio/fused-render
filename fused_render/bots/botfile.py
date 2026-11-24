"""Opening a `.fusedbot` package: what installing it would do, and the install (docs/bots.md §17, D1459).

A package is the app container (`appfile_container`, kind "bot") holding the bot's public half: `bot.json`,
`skills/<name>.md`, an optional `README.md`, `icon.svg` and `.fused/memory.md`, plus one nested
`apps/<folder>.fused` per bundled app. The index's `apps` rows name every app the bot comes with; a row's
`source` says how it travels: `bundled` (the nested app file), `starter` (a key of bots/starters/, copied
from this install), `marketplace` (a slug fetched at install time; never a bundled fallback) or `github` (a
public repo url cloned at install time through the same path the `open?git=` deep link takes).

Every byte here is as untrusted as a mailed `.fused`: the container reader validates the index, members
are read bounded, every field taken from `bot.json` passes the same check the Settings dialog applies, and
an app install is never forced (a state that would lose the user's work is reported as skipped).

A package is keyed by its bot's id (bot.json `id`, its lineage, like an app's `app_id`): the probe's `bot` block says
whether that bot is already here (`new` / `same` / `update` / `diverged`, by the public digest `public_digest`) and
the install either makes a NEW bot (keeping the package id when it is free) or updates the matching one in place.
Either way the bot is stamped with the package's private `origin`; a new bot's routines arrive paused.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import copy
import time
import uuid
import zlib

from fused_render import appfile, appfile_container
from fused_render.bots import paths as bpaths

logger = logging.getLogger(__name__)

# Index walk bound: the same ceiling an app file gets when opened (appfile.MAX_OPEN_ENTRIES); the byte caps
# come from package.py so export and import agree on what a package may weigh.
MAX_ENTRIES = appfile.MAX_OPEN_ENTRIES
BOT_JSON_MAX_BYTES = 256 * 1024
STAGING_PREFIX = ".install-bot-"  # under appfile.install_dir(): ".install-" so _sweep_install_staging ages it out
SOURCE_KINDS = ("bundled", "starter", "marketplace", "github")
# What the file's bot.json may set on the new bot. `id`/`created` are minted fresh, `apps`/`routines` are
# rebuilt below, and name/model/effort/instructions go through create(); `kind`/`super_access` are Super
# Bot's (refused) and everything else in PUBLIC_KEYS has a validator in _public_fields.
_NOT_COPIED = frozenset({"id", "created", "apps", "routines"})
_SKILL_RE = re.compile(r"^skills/([^/]+)\.md$")
_BUNDLED_RE = re.compile(r"^apps/([^/]+)\.fused$")


class BotFileError(Exception):
    """A package that cannot be opened or installed; the message is the sentence the page shows."""


# ------------------------------------------------------------------ reading ---
def _caps():
    from fused_render.bots import package
    return package.PACKAGE_MAX_BYTES, package.TEXT_MAX_BYTES


def _index(path: str) -> dict:
    """The validated index of the package at `path` (kind "bot": an app file says so in its own words)."""
    if not path or not os.path.isabs(path):
        raise BotFileError("path must be an absolute .fusedbot file path")
    if not os.path.isfile(path):
        raise BotFileError(f"no such file: {path}")
    total, _text = _caps()
    try:
        return appfile_container.read_index(path, max_entries=MAX_ENTRIES, max_entry_bytes=total,
                                            max_total_bytes=total, kind="bot")
    except appfile_container.ContainerError as e:
        raise BotFileError(str(e)) from None


def _member_bytes(path: str, idx: dict, rel: str, cap: int) -> bytes | None:
    """A member's bytes, bounded; None when absent. Over the cap is refused rather than truncated: a cut
    playbook or memory would install as something the author never wrote."""
    entry = appfile_container.find(idx, rel)
    if entry is None:
        return None
    if not isinstance(entry.get("size"), int) or entry["size"] > cap:
        raise BotFileError(f"{rel} is larger than {cap} bytes")
    try:
        raw = appfile_container.read_member(path, idx, rel, cap)
    except appfile_container.ContainerError as e:
        raise BotFileError(str(e)) from None
    if raw is None:
        return None
    # read_member bounds, it does not verify: a short or tampered stream must not install as a cut playbook.
    if len(raw) != entry["size"] or (isinstance(entry.get("sha256"), str)
                                      and hashlib.sha256(raw).hexdigest() != entry["sha256"]):
        raise BotFileError(f"{rel} does not match its declared size or hash")
    return raw


def _member_text(path: str, idx: dict, rel: str, cap: int) -> str | None:
    raw = _member_bytes(path, idx, rel, cap)
    return None if raw is None else raw.decode("utf-8", errors="replace")


def _bot_json(path: str, idx: dict) -> dict:
    """The package's bot.json restricted to the public keys (store.PUBLIC_KEYS): nothing private rides in."""
    from fused_render.bots import store
    text = _member_text(path, idx, "bot.json", BOT_JSON_MAX_BYTES)
    if text is None:
        raise BotFileError("this bot package has no bot.json")
    try:
        pub = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise BotFileError(f"bot.json is not valid JSON: {e}") from None
    if not isinstance(pub, dict):
        raise BotFileError("bot.json is not an object")
    return {k: v for k, v in pub.items() if k in store.PUBLIC_KEYS}


def _str(v) -> str:
    return v if isinstance(v, str) else ""


def _origin(idx: dict) -> dict:
    o = idx.get("origin") if isinstance(idx.get("origin"), dict) else {}
    return {"bot_id": _str(o.get("bot_id"))[:64], "exported_at": _str(o.get("exported_at"))[:64]}


def _package_sha(idx: dict) -> str:
    """The digest that binds a bot to the exact package it was installed from: sha256 over the index's member
    hashes (path + sha256, sorted) and its raw `apps` rows. Not bot.json's hash alone: two exports of one bot
    in the same second carry the same bot.json and `origin` and differ only in which apps they list. Index-only
    (read_index already validated every entry's sha256), so no member is read to compute it."""
    files = sorted((_str(f.get("path")), _str(f.get("sha256"))) for f in idx.get("files") or [] if isinstance(f, dict))
    try:
        apps = json.dumps(idx.get("apps"), sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError, RecursionError):
        apps = repr(idx.get("apps"))
    return hashlib.sha256(json.dumps([files, apps], separators=(",", ":")).encode()).hexdigest()


def _folder_ok(folder) -> bool:
    """An app row's folder is one plain path segment: it names a member (`apps/<folder>.fused`) and a
    workspace folder, so a slash, a dot-name or anything path_problem refuses never reaches the disk."""
    return (isinstance(folder, str) and bool(folder) and "/" not in folder and "\\" not in folder
            and not folder.startswith(".") and appfile_container.path_problem(folder) is None)


def _app_rows(idx: dict) -> list[dict]:
    """The index's `apps` rows, cleaned to {folder, name, app_id, source}; a malformed row is dropped. A
    non-list `apps` is a broken package (refused like a non-object bot.json), not a server error."""
    apps = idx.get("apps")
    if apps is None:
        apps = []
    if not isinstance(apps, list):
        raise BotFileError("this bot package's app list is not a list")
    out, seen = [], set()
    for r in apps:
        if not isinstance(r, dict) or not _folder_ok(r.get("folder")) or r["folder"] in seen:
            logger.debug("bot package: dropped a malformed apps row %r", r)  # every probe re-reads the index
            continue
        seen.add(r["folder"])
        raw = r.get("source") if isinstance(r.get("source"), dict) else {}
        # Only the keys the installer reads, each a bounded string: the row lands in the new bot's manifest.
        src = {k: v[:256] for k, v in raw.items()
               if k in ("kind", "key", "slug", "repo", "commit", "url") and isinstance(v, str)}
        if src.get("kind") == "github":
            # The url is what gets cloned, so it alone names the repo: `repo` is rewritten from it (a row saying
            # "trusted/app" over an attacker's url must not display the trusted name) and the url is
            # canonicalized (query/fragment/.git dropped). An over-long url is refused on the RAW value, never a
            # truncated one that might parse as some other repo.
            gh = _github_repo_of(raw.get("url"))
            if gh is None:
                src["kind"] = "unknown"
            else:
                src["repo"] = f"{gh[0]}/{gh[1]}"
                src["url"] = f"https://github.com/{gh[0]}/{gh[1]}"
        elif src.get("kind") not in SOURCE_KINDS:
            src["kind"] = "unknown"
        app_id = r.get("app_id") if isinstance(r.get("app_id"), str) and len(r["app_id"]) <= 128 else None
        out.append({"folder": r["folder"], "name": (_str(r.get("name")) or r["folder"])[:120], "app_id": app_id,
                    "source": src})
    return out


GITHUB_URL_MAX = 256


def _github_repo_of(url) -> tuple[str, str] | None:
    """(owner, name) of a github row's url, else None. It must be a `https://github.com/<owner>/<repo>` link
    (appfetch.parse_github_repo) of at most GITHUB_URL_MAX chars: the only shape the install clones, and
    anything else out of the file must not reach git."""
    from fused_render import appfetch
    if not isinstance(url, str) or not url or len(url) > GITHUB_URL_MAX:
        return None
    try:
        return appfetch.parse_github_repo(url)
    except appfetch.FetchError:
        return None


def _github_url_ok(url) -> bool:
    return _github_repo_of(url) is not None


def _check_text_budget(idx: dict) -> None:
    """Export caps skills + memory at TEXT_MAX_BYTES together; import mirrors it from the index's declared
    sizes BEFORE any member is inflated, so a package of 5000 fat playbooks costs one JSON read, not 5 GB."""
    _total, text_cap = _caps()
    declared = 0
    for f in idx.get("files") or []:
        rel = f.get("path") or ""
        if _SKILL_RE.match(rel) or rel == ".fused/memory.md":
            declared += f.get("size") if isinstance(f.get("size"), int) and f["size"] > 0 else 0
    if declared > text_cap:
        raise BotFileError(f"skills and memory in this package are {declared} bytes; the cap is {text_cap}")


def _check_skill_limits(idx: dict, bodies: list[tuple[str, str]] | None = None) -> None:
    """The bot's own playbook limits (Bot.SKILL_MAX playbooks, Bot.SKILL_BODY_CAP characters of steps), which
    skill_save enforces: playbooks install verbatim (_write_skills), so a package past either is REFUSED, never
    truncated (a cut playbook would neither run as written nor digest like its package). The count is index-only;
    `bodies` ([(name, parsed body)]) is checked when the members were read."""
    from fused_render.bots.bot import Bot
    n = len(_skill_names(idx))
    if n > Bot.SKILL_MAX:
        raise BotFileError(f"this package has {n} playbooks; a bot holds at most {Bot.SKILL_MAX}")
    for name, body in bodies or []:
        if len(body) > Bot.SKILL_BODY_CAP:
            raise BotFileError(f"playbook {name} has more than {Bot.SKILL_BODY_CAP} characters of steps")


def _skill_names(idx: dict) -> list[str]:
    """The playbook members' names, sorted; a dot-name is skipped (export never ships one, package._skill_files)."""
    return sorted(m.group(1) for f in idx.get("files") or []
                  if (m := _SKILL_RE.match(f.get("path") or "")) and not m.group(1).startswith("."))


def _skills(path: str, idx: dict) -> list[dict]:
    """Every playbook member parsed the way the bot parses its own (Bot._parse_skill), body included."""
    from fused_render.bots.bot import Bot
    _total, text_cap = _caps()
    out = []
    for n in _skill_names(idx):
        text = _member_text(path, idx, f"skills/{n}.md", text_cap) or ""
        title, trigger, body = Bot._parse_skill(text)
        out.append({"name": n, "title": title or n, "trigger": trigger, "body": body})
    return out


def _member_check(path: str, idx: dict, rel: str) -> None:
    """Stream-verify one member against its declared size + sha256 without holding its bytes (a bundled app
    can be hundreds of MB): `appfile_container.extract`'s bounded-inflate loop, hashing into nothing."""
    entry = appfile_container.find(idx, rel)
    if entry is None:
        return
    bad = BotFileError(f"{rel} does not match its declared size or hash")
    declared, chunk_max = entry["size"], 1 << 20
    d, h, got, remaining = zlib.decompressobj(), hashlib.sha256(), 0, entry["csize"]
    try:
        with open(path, "rb") as f:
            f.seek(idx["_data_start"] + entry["offset"])
            while remaining > 0:
                chunk = f.read(min(chunk_max, remaining))
                if not chunk:
                    raise bad
                remaining -= len(chunk)
                while chunk:
                    piece = d.decompress(chunk, max_length=min(chunk_max, declared - got + 1))
                    got += len(piece)
                    if got > declared:
                        raise bad
                    h.update(piece)
                    chunk = d.unconsumed_tail
            tail = d.flush(min(chunk_max, declared - got + 1))
            got += len(tail)
            h.update(tail)
    except zlib.error:
        raise bad from None
    except OSError as e:
        raise BotFileError(f"not a readable bot package: {e}") from None
    if got != declared or h.hexdigest() != entry.get("sha256"):
        raise bad


def _validate(path: str, idx: dict, *, members: bool = True,
              bundled: bool = False) -> tuple[dict, list[dict], list[dict] | None]:
    """Every read-only check a package must pass before anything opens it -> (public bot.json, app rows, skills).
    The probe and the upload route both call this, so a file the upload saves is one the install page can open:
    the text budget, bot.json, the `apps` list, and (with `members`) every playbook member and `.fused/memory.md`
    read in full through _member_bytes' size + sha256 check. `members=False` is the light probe's peek (no member
    reads; skills None). `bundled` (the upload only: a probe must not pay for it on every open) also
    stream-verifies every `apps/*.fused` member's size + sha256."""
    _check_text_budget(idx)
    _check_skill_limits(idx)
    pub = _bot_json(path, idx)
    rows = _app_rows(idx)
    skills = None
    if members:
        skills = _skills(path, idx)
        _check_skill_limits(idx, [(s["name"], s["body"]) for s in skills])
        _total, text_cap = _caps()
        _member_bytes(path, idx, ".fused/memory.md", text_cap)  # the read install(memory=True) makes
    if bundled:
        for f in idx.get("files") or []:
            if _BUNDLED_RE.match(f.get("path") or ""):
                _member_check(path, idx, f["path"])
    return pub, rows, skills


# ------------------------------------------------------------------ staging ---
def _staging_dir(path: str) -> str:
    """One staging dir per package version (path + mtime): a probe stages, the install reuses it."""
    st = os.stat(path)
    key = hashlib.sha256(f"{os.path.abspath(path)}\0{st.st_mtime_ns}\0{st.st_size}".encode()).hexdigest()[:16]
    return os.path.join(appfile.install_dir(), STAGING_PREFIX + key)


def _stage(path: str, idx: dict, folders: list[str]) -> dict[str, str]:
    """Extract the nested `apps/<folder>.fused` members for `folders` to `<staging>/<folder>.fused` and return
    folder -> staged path (folders without a member are absent). Each member goes through its own temp dir
    because `extract` deletes its whole dest on a bad stream, and lands by rename, so a staged file that
    exists is complete. Streaming (never `read_member`): an app file can be hundreds of MB."""
    want = {}
    for f in idx.get("files") or []:
        m = _BUNDLED_RE.match(f.get("path") or "")
        if m and m.group(1) in folders:
            want[m.group(1)] = f
    if not want:
        return {}
    root = appfile.install_dir()
    os.makedirs(root, exist_ok=True)
    appfile._sweep_install_staging(root)  # otherwise it runs only inside install_app_file
    staging = _staging_dir(path)
    os.makedirs(staging, exist_ok=True)
    os.utime(staging)  # in use again: the TTL sweep counts from now
    out = {}
    for folder, entry in want.items():
        dst = os.path.join(staging, folder + ".fused")
        if os.path.isfile(dst):
            # Reused only when it is this very member: same declared size and hash (a staging-key collision or
            # a rewritten package must not hand the installer another package's app).
            try:
                same = os.path.getsize(dst) == entry.get("size") and (not isinstance(entry.get("sha256"), str)
                                                                       or _sha256_file(dst) == entry["sha256"])
            except OSError:
                same = False
            if not same:
                os.unlink(dst)
        if not os.path.isfile(dst):
            tmp = tempfile.mkdtemp(dir=staging, prefix=".x-")
            try:
                appfile_container.extract(path, {"files": [entry], "_data_start": idx["_data_start"]}, tmp)
                os.replace(os.path.join(tmp, "apps", folder + ".fused"), dst)
            except (appfile_container.ContainerError, OSError) as e:
                raise BotFileError(f"could not unpack the app {folder}: {e}") from None
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        out[folder] = dst
    return out


def _sha256_file(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ app state ---
_BUNDLED_REASON = {
    appfile.STATE_SAME: "Already installed",
    appfile.STATE_DIVERGED: "Your copy has changes of its own; update it from the app's page",
    appfile.STATE_DOWNGRADE: "A newer version is already installed",
    appfile.STATE_UNKNOWN: "A copy with no install record is already there",
    appfile.STATE_LOCAL: "Your copy is a git checkout with its own commits; update it from the app's page",
    appfile.STATE_REPLACE: "Your copy came from a repository; update it from the app's page",
    appfile.STATE_SWITCH_LINK: "Your copy follows a Marketplace listing; switch it from the app's page",
}


def _starter_installed(key: str) -> bool:
    from fused_render.bots import starters
    try:
        rows = starters.list_state(bpaths.apps_root())["starters"]
    except (ValueError, OSError):
        return False
    return any(s["key"] == key and s["installed"] for s in rows)


def _market_copy(slug: str) -> dict | None:
    from fused_render import marketplace
    try:
        return marketplace.installed_copies().get(slug)
    except Exception:  # noqa: BLE001 - a store read failure reads as "not installed"; the install says why
        logger.warning("bot package: marketplace copies unreadable", exc_info=True)
        return None


def _github_clone(url: str) -> str | None:
    """The workspace clone of a github row's (canonical, `_app_rows`) url, else None:
    `appfile_git.installed_clone_of`, the walk `appfile.install_target` runs for a git-source file. Local git and
    the workspace walk only — no network."""
    from fused_render import appfile_git
    if not url:
        return None
    try:
        return appfile_git.installed_clone_of(appfile_git.normalize_url(url))
    except Exception:  # noqa: BLE001 - a git that cannot answer reads as "no clone"; the install says why
        logger.warning("bot package: clone lookup failed for %s", url, exc_info=True)
        return None


def _row_state(row: dict, staged: str | None) -> dict:
    """state / ticked / tickable / reason for one app row (§8)."""
    src = row["source"]
    kind = src.get("kind")
    if kind == "bundled":
        if not staged:
            return {"state": "missing", "ticked": False, "tickable": False, "reason": "The app is not in the package"}
        try:
            t = appfile.install_target(staged, light=True)
        except appfile.AppFileError as e:
            return {"state": "error", "ticked": False, "tickable": False, "reason": str(e)}
        state = t["state"]
        if state == appfile.STATE_SAME:
            # Installed at this exact version: nothing to write, but the row stays ticked (and tickable) so the
            # bot gets to use it without asking, as the exporter's bot did — the member's own app id was checked
            # against the copy by install_target, so this is the real app, not an index claim. Untick to refuse.
            return {"state": state, "ticked": True, "tickable": True,
                    "reason": "Already installed; the bot may use it without asking"}
        out = {"state": state, "ticked": state in (appfile.STATE_NEW, appfile.STATE_UPDATE),
               "tickable": state not in appfile.WARN_STATES and state != appfile.STATE_CLASH and not t.get("needs_force")}
        if state == appfile.STATE_CLASH:
            out["reason"] = f"{t['path']} already holds a different app ({t.get('other_name') or 'unknown'})"
        elif state in _BUNDLED_REASON:
            out["reason"] = _BUNDLED_REASON[state]
        return out
    if kind == "starter":
        installed = _starter_installed(_str(src.get("key")))
    elif kind == "marketplace":
        installed = _market_copy(_str(src.get("slug"))) is not None
    elif kind == "github":
        # A clone of the repo only. A hand-made app carrying the row's app id is NOT "installed": ids are public
        # and the install will clone regardless, so saying so here would be a lie the page cannot qualify.
        installed = _github_clone(_str(src.get("url"))) is not None
    else:
        return {"state": "unknown", "ticked": False, "tickable": False, "reason": "Unknown app source"}
    if installed:
        # Same rule as a bundled app at the same version: ticked = trusted, untick to refuse; nothing is written.
        return {"state": "installed", "ticked": True, "tickable": True,
                "reason": "Already installed; the bot may use it without asking"}
    return {"state": "missing", "ticked": True, "tickable": True}


# ------------------------------------------------------------------ identity ---
# The public settings an install applies beyond name/model/effort/instructions, and the ones an update replaces
# (a key the package lacks is removed from the bot). `kind` is left out: a non-Super bot is "bot" or unset, and
# the Super Bot never matches. Permissions (approval/build_access/terminal) never travel (_public_fields).
_APPLIED_KEYS = ("face", "preset", "engine", "max_steps", "step_cap")
_ROUTINE_DIGEST_KEYS = ("id", "task", "kind", "minutes", "time", "weekdays", "at")
_PERMISSION_KEYS = frozenset({"approval", "build_access", "terminal", "super_access"})


def _package_id(pub: dict, idx: dict) -> str | None:
    """The package's lineage id: bot.json's `id` (the exported bot's id), else the index's `origin.bot_id`; None
    when neither is a bot id (a hand-made package matches nothing and installs under a minted id)."""
    for v in (pub.get("id"), _origin(idx)["bot_id"]):
        if isinstance(v, str) and bpaths._ID_RE.fullmatch(v):
            return v
    return None


def _applied(pub: dict, name: str) -> dict:
    """What an install writes into the bot's public half from bot.json: name/model/effort/instructions the way
    create() stores them, plus the settings that pass _public_fields' checks."""
    from fused_render.bots import bot as bm
    model, effort = _str(pub.get("model")), _str(pub.get("effort"))
    return {"name": name, "model": model if model in bm.MODELS else bm.DEFAULT_MODEL,
            "effort": effort if effort in bm.EFFORTS else bm.DEFAULT_EFFORT,
            "instructions": _str(pub.get("instructions")).strip(),
            **_public_fields({}, {k: v for k, v in pub.items() if k not in _PERMISSION_KEYS})}


def public_digest(pub: dict, name: str, skills: list[tuple[str, str]], readme_sha: str, icon_sha: str) -> str:
    """The "same / update / diverged" oracle (docs/bots.md §17): sha256 over (a) store.public_json of the public
    half PROJECTED onto what an install carries (`_applied` keys, routines cut to their definition: no `id` of the
    bot, `created`, `anchor`, `apps` or permissions, none of which an install copies verbatim), (b) each playbook
    `name \\0 sha256(bytes)` sorted by name, (c) README.md's and (d) icon.svg's sha256 ('' when absent). One
    function for both sides: `_package_public_sha` (a package, from its index) and `package.public_sha` (a bot on
    disk) feed it, so a bot right after its install or update digests exactly like its package."""
    from fused_render.bots import store
    a = _applied(pub, name)
    view = {k: a[k] for k in ("name", "model", "effort", "instructions")}
    view.update({k: a[k] for k in _APPLIED_KEYS if k in a})
    view["routines"] = [{k: r[k] for k in _ROUTINE_DIGEST_KEYS if k in r} for r in _routines(pub.get("routines"))]
    h = hashlib.sha256(store.public_json(view).encode("utf-8"))
    keep, _drop = _case_unique(n for n, _sha in skills)
    for n, sha in sorted(s for s in skills if s[0] in keep):
        h.update(b"\0skill\0" + n.encode("utf-8", errors="replace") + b"\0" + sha.encode())
    h.update(b"\0readme\0" + (readme_sha or "").encode() + b"\0icon\0" + (icon_sha or "").encode())
    return h.hexdigest()


def _entry_sha(path: str, idx: dict, rel: str, cap: int) -> str:
    """A member's sha256 from the index ('' when absent or over `cap`: the install would not copy it); the member
    is read only when the index carries no usable hash."""
    entry = appfile_container.find(idx, rel)
    if entry is None or not isinstance(entry.get("size"), int) or entry["size"] > cap:
        return ""
    sha = entry.get("sha256")
    if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha):
        return sha
    raw = _member_bytes(path, idx, rel, cap)
    return "" if raw is None else hashlib.sha256(raw).hexdigest()


def _package_public_sha(path: str, idx: dict, pub: dict | None = None) -> str:
    """public_digest of the package at `path`: bot.json parsed, every other part from the index's member hashes."""
    pub = _bot_json(path, idx) if pub is None else pub
    _total, text_cap = _caps()
    skills = [(n, _entry_sha(path, idx, f"skills/{n}.md", text_cap)) for n in _skill_names(idx)]
    return public_digest(pub, _install_name(pub, idx), skills, _entry_sha(path, idx, "README.md", text_cap),
                         _entry_sha(path, idx, "icon.svg", appfile.MAX_ICON_BYTES))


def _install_name(pub: dict, idx: dict) -> str:
    return _str(pub.get("name")) or _str(idx.get("name")) or "Bot"  # never empty: create() would invent one


def _find_match(pkg_id: str | None, id_only: bool = False) -> dict | None:
    """The bot already here for a package id, read off disk (the probe is served by any process, never through a
    live Bot): the bot whose id IS the package id, else (bots installed by earlier builds, which minted a fresh
    id) the first bot whose private `origin.bot_id` is. The Super Bot never matches. `id_only`: no scan."""
    from fused_render.bots import store
    if not pkg_id:
        return None
    ids = store.list_ids()
    if pkg_id in ids and (m := _disk_meta(pkg_id)) is not None:
        return m
    if id_only:
        return None
    for bid in ids:
        if bid == pkg_id:
            continue
        m = _disk_meta(bid)
        o = (m or {}).get("origin")
        if isinstance(o, dict) and o.get("bot_id") == pkg_id:
            return m
    return None


def _disk_meta(bid: str) -> dict | None:
    """A bot's merged meta read WITHOUT side effects (None for the Super Bot or an unreadable bot.json): unlike
    store.read_meta, a corrupt state.json is not moved aside (the probe is a read) but read as empty private
    state. Not store.read_all_metas: one bad folder there answers None for every bot."""
    from fused_render.bots import store
    from fused_render.bots.bot import is_super
    try:
        folder = bpaths.bot_folder(bid)
    except LookupError:
        return None
    public = store._read_object(os.path.join(folder, store.BOT_FILE))
    if public is None:
        return None
    spath = os.path.join(folder, bpaths._PRIVATE, store.STATE_FILE)
    private = (store._read_object(spath) if os.path.lexists(spath) else None) or {}
    m = store._merge(public, private, spath, warn=False)
    return None if is_super(m) else {**m, "id": bid}


def _bot_block(path: str, idx: dict, pub: dict, light: bool = False) -> dict:
    """The probe's `bot` block: {state, existing, edited_here} (docs/bots.md §17). `light` (the card peek, which
    never pays for the open): the id match only, through the folder index (no origin scan across bots, no
    digest), so a match answers `update` and `edited_here` is False; the full probe has the exact states."""
    from fused_render.bots import package
    m = _find_match(_package_id(pub, idx), id_only=light)
    if m is None:
        return {"state": "new", "existing": None, "edited_here": False}
    existing = {"id": m["id"], "name": _str(m.get("name")) or m["id"],
                "face": m["face"] if isinstance(m.get("face"), dict) else None}
    if light:
        return {"state": "update", "existing": existing, "edited_here": False}
    try:
        have = package.public_sha(m["id"])
    except (OSError, ValueError, LookupError):  # unreadable now: never "same", and the page still offers both
        logger.warning("bot package: could not digest bot %s", m["id"], exc_info=True)
        have = ""
    origin = m.get("origin") if isinstance(m.get("origin"), dict) else {}
    stamped = origin.get("public_sha") if isinstance(origin.get("public_sha"), str) else None
    edited = stamped is not None and stamped != have
    if have == _package_public_sha(path, idx, pub):
        state = "same"
    else:
        state = "diverged" if edited else "update"
    return {"state": state, "edited_here": edited, "existing": existing}


# ------------------------------------------------------------------ probe ---
def probe(path: str, light: bool = False) -> dict:
    """What installing the package at `path` would do: the install page's data. Read-only towards the
    workspace; the full probe stages the bundled app files (so each can be asked `install_target` in its own
    words) under appfile.install_dir(). `light` is a card peek: index + bot.json only, no staging, no member
    reads, no state per app (D396's rule: a peek never pays for the open); its `bot` block is the id match alone
    (`update` when a bot here holds the package id, else `new`; no origin scan, no digest)."""
    idx = _index(path)
    pub, rows, skills = _validate(path, idx, members=not light)
    out = {"ok": True, "name": _install_name(pub, idx), "public": pub,
           "exported_at": _str(idx.get("exported_at")) or None, "origin": _origin(idx),
           "memory": appfile_container.find(idx, ".fused/memory.md") is not None,
           "icon": appfile_container.find(idx, "icon.svg") is not None,
           "readme": appfile_container.find(idx, "README.md") is not None,
           "bot": _bot_block(path, idx, pub, light=light)}
    if light:
        out["skills"] = [{"name": n} for n in _skill_names(idx)]
        out["apps"] = rows
        return out
    out["skills"] = [{k: s[k] for k in ("name", "title", "trigger")} for s in skills or []]
    staged = _stage(path, idx, [r["folder"] for r in rows if r["source"].get("kind") == "bundled"])
    out["apps"] = [{**r, **_row_state(r, staged.get(r["folder"]))} for r in rows]
    out["staging"] = _staging_dir(path) if staged else None
    return out


def icon(path: str) -> bytes | None:
    """The package's `icon.svg` member (bounded like an app file's icon), None without one: the page then
    draws the bot's face."""
    idx = _index(path)
    try:
        return _member_bytes(path, idx, "icon.svg", appfile.MAX_ICON_BYTES)
    except BotFileError:
        return None


# ------------------------------------------------------------------ install ---
def _install_bundled(staged: str | None) -> dict:
    """One bundled app, never forced: a state that needs the user's confirmation is skipped, not overwritten."""
    if not staged:
        return {"ok": False, "state": "missing", "error": "the app is not in the package"}
    try:
        t = appfile.install_target(staged)
    except appfile.AppFileError as e:
        return {"ok": False, "state": "error", "error": str(e)}
    if t["state"] == appfile.STATE_SAME:
        return {"ok": True, "state": "same", "path": t["path"]}  # install_app_file would refuse it unforced
    try:
        done = appfile.install_app_file(staged)
    except appfile.InstallNeedsConfirm as e:
        st = e.target["state"]
        return {"ok": False, "state": st, "error": "skipped: " + _BUNDLED_REASON.get(st, f"state {st!r} needs confirmation")}
    except (appfile.AppFileError, OSError, ValueError) as e:  # a row's failure, never the whole install's
        return {"ok": False, "state": t["state"], "error": str(e)}
    return {"ok": True, "state": done.get("action") or "installed", "path": done["path"]}


def _install_starter(key: str) -> dict:
    from fused_render.bots import starters
    if not key or starters.get(key) is None:
        return {"ok": False, "state": "error", "error": f"this install has no built-in app {key!r}"}
    root = bpaths.apps_root()
    if _starter_installed(key):
        return {"ok": True, "state": "installed", "path": os.path.join(root, key)}
    starters.ensure([key], root)  # never raises; the re-check below is the verdict
    if not _starter_installed(key):
        return {"ok": False, "state": "error", "error": f"could not copy the built-in app {key!r}"}
    return {"ok": True, "state": "installed", "path": os.path.join(root, key)}


def _install_marketplace(slug: str) -> dict:
    """Start the Community install and wait for its job. Mirrors tools._market_install's loop (that one
    takes a Bot for its Stop flag and answers model-facing text, so it is not reusable here)."""
    from fused_render import marketplace as mkt
    from fused_render.bots import tools
    copy = _market_copy(slug)
    if copy is not None:
        return {"ok": True, "state": "installed", "path": copy.get("path")}
    try:
        job = mkt.start_install(slug)
    except mkt.InstallError as e:
        return {"ok": False, "state": "error", "error": str(e)}
    if job.get("status") == "confirm":
        # The user's own copy (a GitHub link, a showcase clone) holds the place: their choice, made on the
        # app's Community page, never here.
        return {"ok": False, "state": "error", "error": job.get("message") or "needs your choice on the app's Community page"}
    deadline = time.monotonic() + tools.MARKET_INSTALL_WAIT_S
    while job.get("state") not in ("done", "error"):
        if time.monotonic() > deadline:
            return {"ok": False, "state": "error",
                    "error": f"still installing after {tools.MARKET_INSTALL_WAIT_S} s; it finishes on its own (see the Community page)"}
        time.sleep(1.0)
        job = (mkt.list_jobs().get("jobs") or {}).get(slug) or job
    if job.get("state") == "error":
        return {"ok": False, "state": "error", "error": job.get("error") or job.get("message") or "install failed"}
    copy = _market_copy(slug)
    return {"ok": True, "state": "installed", "path": job.get("path") or (copy or {}).get("path")}


def _install_github(row: dict) -> dict:
    """One github row: clone its repo into a staged `.fused` through `appfetch.download_github_app` — EXACTLY
    what the `fused-render://open?git=` deep link does — then install that file the way a bundled member is
    installed (`install_target`, then `install_app_file`, never forced; `same` writes nothing). A clone of the
    repo already here (`_github_clone`) → installed, nothing written, so a Retry is safe. A copy matched by app
    id only is NOT enough (ids are public): the repo is cloned and `install_target` decides, so a hand-made app
    holding the id is a replace/diverged row that is skipped, never overwritten or trusted unseen.

    The install follows the AUTHOR'S DEFAULT-BRANCH HEAD, what the platform's GitHub link installs, not the
    exporter's `commit` (which stays in the row for display). Pinning to the exporter's commit was considered and
    rejected: the platform's GitHub share semantics are "follow the author" (share_app.github_link_state links the
    default branch, appfile_git updates a clone to it), so a pinned install would silently diverge from every other
    GitHub-installed copy of the same app. A clone failure (offline, private or deleted repo) is this row's error;
    the bot is still made and the page's per-row Retry (re-install mode) runs this again."""
    from fused_render import appfetch, appfile_git
    url = _str(row["source"].get("url"))
    repo = _str(row["source"].get("repo")) or url
    if not _github_url_ok(url):
        return {"ok": False, "state": "error", "error": f"not a GitHub repository link: {url or '(empty)'}"}
    have = _github_clone(url)
    if have:
        return {"ok": True, "state": "installed", "path": have}
    try:
        staged = appfetch.download_github_app(url)
    except (appfetch.FetchError, appfile_git.GitInstallError, appfile.AppFileError, OSError) as e:
        return {"ok": False, "state": "error",
                "error": f"could not clone {repo} from GitHub: {e or e.__class__.__name__}"}
    # OSError/ValueError too: appfile_git.install's post-place steps (git config, record writes) must fail this
    # row, never escape _install_apps and fail the whole POST before the bot exists.
    try:
        t = appfile.install_target(staged)
    except (appfile.AppFileError, OSError, ValueError) as e:
        return {"ok": False, "state": "error", "error": f"could not install {repo} from GitHub: {e}"}
    if t["state"] == appfile.STATE_SAME:
        return {"ok": True, "state": "same", "path": t["path"]}
    try:
        done = appfile.install_app_file(staged)
    except appfile.InstallNeedsConfirm as e:
        st = e.target["state"]
        return {"ok": False, "state": st, "error": "skipped: " + _BUNDLED_REASON.get(st, f"state {st!r} needs confirmation")}
    except (appfile.AppFileError, OSError, ValueError) as e:
        return {"ok": False, "state": t["state"], "error": f"could not install {repo} from GitHub: {e}"}
    return {"ok": True, "state": done.get("action") or "installed", "path": done["path"]}


def _already_there(row: dict) -> str | None:
    """Where an app the user did NOT tick already lives, else None: it is trusted like one installed now.
    Bundled rows are found by app id (no extraction); a copy found that way is the same app whatever its
    edits, so it counts."""
    src = row["source"]
    kind = src.get("kind")
    if kind == "bundled":
        return appfile.workspace_app_with_id(row["app_id"]) if row["app_id"] else None
    if kind == "starter":
        key = _str(src.get("key"))
        return os.path.join(bpaths.apps_root(), key) if key and _starter_installed(key) else None
    if kind == "marketplace":
        return (_market_copy(_str(src.get("slug"))) or {}).get("path")
    if kind == "github":
        return _github_clone(_str(src.get("url")))  # a clone only: an app id match counts for nothing
    return None


def _routines(rows) -> list[dict]:
    """The file's routines as definitions only (store.ROUTINE_PUBLIC_KEYS), paused: every imported routine
    waits for the user to switch it on (routine_update recomputes `next` then)."""
    from fused_render.bots.bot import Bot
    out, ids = [], set()
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not _str(r.get("task")).strip() or r.get("kind") not in ("interval", "daily", "once"):
            continue
        kind = r["kind"]
        # The schedule fields go through the same validator Settings uses (ValueError on a bad time, weekday,
        # minutes, or a once-time in the past); `anchor` is NEVER copied: _next_run loops from it, and a
        # file-written -1e300 would spin the scheduler thread forever on the user's first Enable.
        try:
            fields = Bot._routine_fields(kind, r.get("minutes"), r.get("time") or "", r.get("weekdays"), r.get("at"))
        except (ValueError, TypeError, OverflowError) as e:
            logger.info("bot package: routine %r dropped: %s", _str(r.get("task"))[:60], e)
            continue
        rid = _str(r.get("id"))
        if not re.fullmatch(r"[0-9a-f]{4,16}", rid) or rid in ids:
            rid = hashlib.sha256(f"{len(out)}{r['task']}".encode()).hexdigest()[:6]
        ids.add(rid)
        row = {"id": rid, "task": _str(r["task"]).strip()[:2000], "kind": kind, "created": time.time(),
               **fields, "enabled": False, "next": None, "last": None, "last_result": ""}
        out.append(row)
    return out


def _public_fields(meta: dict, pub: dict) -> dict:
    """The file's public settings that pass the Settings dialog's own checks (routes._settings); a value
    that fails is dropped with a log line, never half-applied."""
    from fused_render.bots import bot as bm
    from fused_render.bots import presets, store
    out = {}
    for k in sorted(store.PUBLIC_KEYS - _NOT_COPIED - {"name", "model", "effort", "instructions"}):
        if k not in pub:
            continue
        v = pub[k]
        try:
            if k == "face" and isinstance(v, dict):
                out[k] = bm.check_face(meta, v, strict=True)
            elif k == "preset" and isinstance(v, str) and presets.get(v) is not None:
                out[k] = v  # the key only: the playbooks arrive as skills, apply_preset would duplicate them
            elif k in ("approval", "build_access", "terminal"):
                # Permissions never travel: a file's author does not get to pre-approve actions, a full build
                # scope or a terminal on the importer's Mac. The bot arrives with the defaults (ask / scoped /
                # off) and the importer flips them in Settings, where each has its own explanation.
                if v not in (None, "", False, "ask", "scoped"):
                    logger.info("bot package: %s=%r not applied (permissions are set by the importer)", k, v)
            elif k == "engine" and v in bm.ENGINES:
                out[k] = v
            elif k == "max_steps" and v not in (None, "", 0):
                bm.check_settings(meta, max_steps=v)
                out[k] = int(v)
            elif k == "step_cap" and isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                out[k] = v
            elif k == "kind" and v == "bot":
                out[k] = v
            elif v not in (None, "", 0):
                logger.info("bot package: %s=%r not applied", k, v)
        except ValueError as e:
            logger.info("bot package: %s refused: %s", k, e)
    return out


def _install_apps(path: str, idx: dict, rows: list[dict], apps) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Install the chosen rows (in the given order, never forced) -> (per-row results, rows installed OK, installed
    path by the INDEX folder key: a result row's `folder`, never the disk spelling a done row may carry).
    An installed row's `folder` is rewritten to where the app really sits (a bundled app found by id can live
    elsewhere): the bot's manifest row and its trust follow the disk, not the exporter's spelling.
    Idempotent: an app already there reports ok (same / installed) without a write, so a retry is safe."""
    from fused_render.bots import apptools
    by_folder = {r["folder"]: r for r in rows}
    chosen = list(dict.fromkeys(a for a in apps or [] if isinstance(a, str)))
    staged = _stage(path, idx, [f for f in chosen if f in by_folder and by_folder[f]["source"].get("kind") == "bundled"])
    results, done, paths = [], [], {}
    for folder in chosen:
        row = by_folder.get(folder)
        if row is None:
            results.append({"folder": folder, "ok": False, "state": "missing", "error": "not in this package"})
            continue
        kind = row["source"].get("kind")
        if kind == "bundled":
            res = _install_bundled(staged.get(folder))
        elif kind == "starter":
            res = _install_starter(_str(row["source"].get("key")))
        elif kind == "marketplace":
            res = _install_marketplace(_str(row["source"].get("slug")))
        elif kind == "github":
            res = _install_github(row)
        else:
            res = {"ok": False, "state": "unknown", "error": "unknown app source"}
        if res["ok"]:
            where = res.get("path")
            if isinstance(where, str) and where:
                row["folder"] = os.path.basename(where.rstrip("/\\")) or folder
                paths[folder] = where
            done.append(row)
        results.append({"folder": folder, **{k: v for k, v in res.items() if k != "path"}})
    if done:
        try:
            apptools.apps(force=True)
            apptools.registry(force=True)
        except Exception:  # noqa: BLE001 - a stale APPS cache heals on its TTL
            logger.debug("bot package: apps cache refresh failed", exc_info=True)
    return results, done, paths


def _unlinked(results: list[dict], paths: dict[str, str], err: Exception) -> None:
    """The bot update after an install (manifest row + trust) failed: every row whose app DID install becomes
    `ok: false, state: "settings"` with its installed path, so the page shows that row's Retry. The Retry is
    re-install mode and idempotent: the app is already there (ok, nothing written), its manifest row is matched
    by folder, and trust skips a folder already listed."""
    why = f"installed, but the bot could not be updated: {err or err.__class__.__name__}"
    for r in results:
        if r.get("ok"):
            r.update({"ok": False, "state": "settings", "error": why})
            if paths.get(r["folder"]):
                r["path"] = paths[r["folder"]]


# Idempotent POSTs (PER OWNER PROCESS: a forwarder never dedups, and a Retry that lands on a freshly elected
# owner finds no memo — accepted, the window is a quit between POST and Retry): request_id -> (finished at,
# result) for installs that finished in this process,
# and request_id -> Event for one still running. A page Retry after a transport failure (the reply was lost,
# the bot was made) carries the same id and gets the first answer instead of a second bot. Only successes
# are kept: a failure happened before anything irreversible (install()'s invariant), so re-running is safe.
REQUEST_TTL_S = 3600
REQUEST_MAX = 200
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_req_done: dict[str, tuple[float, dict]] = {}
_req_running: dict[str, threading.Event] = {}
_req_lock = threading.Lock()


def _req_prune_locked(now: float) -> None:
    for rid in [r for r, (t, _) in _req_done.items() if now - t > REQUEST_TTL_S]:
        _req_done.pop(rid, None)
    while len(_req_done) > REQUEST_MAX:
        _req_done.pop(min(_req_done, key=lambda r: _req_done[r][0]), None)


class NoSuchBot(BotFileError):
    """Re-install mode named a bot that does not exist (the route answers 404)."""


INSTALL_MODES = ("new", "update")


def install(path: str, *, apps: list[str], memory: bool, mode: str = "new", target: str | None = None,
            bot_id: str | None = None, request_id: str | None = None, wait_s: float = REQUEST_TTL_S) -> dict:
    """Install the package at `path`: `mode="new"` makes a bot (_install_new), `mode="update"` updates `target`,
    the bot the probe's `bot.existing` named (_install_update); `bot_id` is re-install mode, the per-row Retry
    (_install_into), whatever the mode. With `request_id` the call is idempotent: a repeat answers the first
    call's result (whatever mode it carries: the page pins its selection on first submit); a repeat while the
    first still runs waits for it (up to `wait_s`)."""
    if mode not in INSTALL_MODES:
        raise BotFileError("mode must be 'new' or 'update'")
    kw = {"apps": apps, "memory": memory, "mode": mode, "target": target, "bot_id": bot_id}
    if not request_id:
        return _install(path, **kw)
    if not REQUEST_ID_RE.match(request_id):
        raise BotFileError("request_id must be 1-64 letters, digits, '-' or '_'")
    with _req_lock:
        _req_prune_locked(time.time())
        if request_id in _req_done:
            return _req_done[request_id][1]
        ev = _req_running.get(request_id)
        mine = ev is None
        if mine:
            ev = _req_running[request_id] = threading.Event()
    if not mine:
        ev.wait(wait_s)
        with _req_lock:
            hit = _req_done.get(request_id)
        if hit is None:
            raise BotFileError("that install did not finish; try again")
        return hit[1]
    try:
        out = _install(path, **kw)
        with _req_lock:
            _req_done[request_id] = (time.time(), out)
            _req_prune_locked(time.time())
        return out
    finally:
        with _req_lock:
            _req_running.pop(request_id, None)
        ev.set()


def _install(path: str, *, apps: list[str], memory: bool, mode: str, target: str | None,
             bot_id: str | None) -> dict:
    from fused_render.bots import registry
    idx = _index(path)
    pub = _bot_json(path, idx)
    if pub.get("kind") == "super":
        raise BotFileError("this package is a Super Bot; there is one per Mac and it cannot be imported")
    registry.require_owner()
    if bot_id:
        return _install_into(path, idx, bot_id, apps)
    if mode == "update":
        return _install_update(path, idx, pub, target, apps, memory)
    return _install_new(path, idx, pub, apps, memory)


def _merge_manifest(manifest, rows: list[dict], orig: dict[int, str]) -> list[dict]:
    """The bot's `apps` manifest with `rows` merged in by folder: a row already listed (under its disk folder or
    the index spelling `orig[id(row)]`, which a first install recorded) is replaced by the package's row, which
    carries the folder the app really sits in; any other row is appended."""
    out = [dict(r) for r in manifest or [] if isinstance(r, dict)]
    for row in rows:
        hit = next((m for m in out if m.get("folder") in (row["folder"], orig.get(id(row)))), None)
        if hit is not None:
            hit.update(row)
        else:
            out.append(dict(row))
    return out


def _install_into(path: str, idx: dict, bot_id: str, apps) -> dict:
    """Re-install mode, the page's per-row Retry: install only the listed rows for a bot this file already
    made (its private `origin` matches the file's), then add what installed to that bot's `apps` manifest and
    `trusted_apps`. Never creates a bot."""
    from fused_render.bots import apptools, registry
    try:
        b = registry.get(bot_id)
    except (ValueError, LookupError):
        raise NoSuchBot(f"no such bot {os.path.basename(bot_id)}") from None
    want, have = _origin(idx), b.meta.get("origin") if isinstance(b.meta.get("origin"), dict) else {}
    if not (want["bot_id"] and want["exported_at"] and have.get("bot_id") == want["bot_id"]
            and have.get("exported_at") == want["exported_at"]):
        raise BotFileError("that bot did not come from this file")
    # bot_id + exported_at (1 s resolution) can match a DIFFERENT export of the same bot made in the same
    # second; the digest stamped at first install names the very package, so its apps never land in this bot.
    if have.get("package_sha") != _package_sha(idx):
        raise BotFileError("this package is not the one this bot was installed from")
    rows = _app_rows(idx)
    orig = {id(r): r["folder"] for r in rows}  # the index spelling, before _install_apps follows the disk
    results, done, paths = _install_apps(path, idx, rows, apps)
    warnings: list[str] = []
    if done:
        try:
            with b.lock:
                b.meta["apps"] = _merge_manifest(b.meta.get("apps"), done, orig)
                apptools.set_trusted_apps(b.meta, add=[r["folder"] for r in done])
                b.save()
        except Exception as e:  # noqa: BLE001 - the apps are installed; say the bot was not updated
            logger.warning("bot package: re-install could not update %s", b.id, exc_info=True)
            warnings.append(f"settings: {e or e.__class__.__name__}")
            _unlinked(results, paths, e)
    return {"ok": True, "bot_id": b.id, "updated": False, "apps": results, "warnings": warnings}


def _copy_folder_members(path: str, idx: dict, bid: str, warnings: list[str], replace: bool = False) -> None:
    """The package's README.md and icon.svg into the bot's public folder, where export picked them up: without
    this a re-export of an imported bot silently drops both. Each is bounded (icon at the cap export and the
    install page apply, README at the text cap). A new bot's copy never replaces a file already there; an update
    (`replace`) makes the bot's match the package (override semantics, so the bot digests like its package,
    public_digest): swapped in when the package carries it, REMOVED when it does not or when it is not copied
    (over the cap, a failed write; said in a warning). Any failure is a warning, never a failed install (the bot
    exists by now)."""
    _total, text_cap = _caps()
    for rel, cap in (("README.md", text_cap), ("icon.svg", appfile.MAX_ICON_BYTES)):
        entry = appfile_container.find(idx, rel)
        copied = False
        try:
            if entry is None:
                pass
            elif not isinstance(entry.get("size"), int) or entry["size"] > cap:
                warnings.append(f"{rel}: larger than {cap} bytes; not copied")
            elif (raw := _member_bytes(path, idx, rel, cap)) is None:
                pass
            elif replace:
                dst = os.path.join(bpaths.bot_folder(bid), rel)
                tmp = f"{dst}.{uuid.uuid4().hex[:8]}.tmp"
                try:
                    with open(tmp, "xb") as f:
                        f.write(raw)
                    os.replace(tmp, dst)
                    copied = True
                except OSError:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            else:
                _copy_exclusive(os.path.join(bpaths.bot_folder(bid), rel), rel, raw, warnings)
        except (BotFileError, OSError, LookupError) as e:
            logger.warning("bot package: %s not copied to %s", rel, bid, exc_info=True)
            warnings.append(f"{rel}: {e or e.__class__.__name__}")
        if replace and not copied:
            _remove_folder_member(bid, rel, entry is not None, warnings)


def _remove_folder_member(bid: str, rel: str, had: bool, warnings: list[str]) -> None:
    """An update whose package does not (or could not) bring `rel`: the bot's own goes, so it matches the package."""
    try:
        dst = os.path.join(bpaths.bot_folder(bid), rel)
        if not os.path.lexists(dst):
            return
        os.unlink(dst)
        if had:
            warnings.append(f"{rel}: the bot's own was removed to match the package")
    except (OSError, LookupError) as e:
        logger.warning("bot package: %s not removed from %s", rel, bid, exc_info=True)
        warnings.append(f"{rel}: could not remove the bot's own: {e or e.__class__.__name__}")


def _copy_exclusive(dst: str, rel: str, raw: bytes, warnings: list[str]) -> None:
    """A new bot's copy of a folder member: never over a file the bot already has."""
    try:
        f = open(dst, "xb")  # exclusive: never over a file the bot already has
    except FileExistsError:
        warnings.append(f"{rel}: the bot already has one; kept it")
        return
    try:
        with f:
            f.write(raw)
    except OSError:
        try:
            os.unlink(dst)  # ours, half-written: it would export as something the author never wrote
        except OSError:
            pass
        raise


def _has_folder(bid: str) -> bool:
    """A folder place() registered for `bid` (its bot.json may not be written yet)."""
    try:
        bpaths.bot_folder(bid)
        return True
    except LookupError:
        return False


def _mint_id() -> str:
    """A fresh bot id (create()'s shape: 8 hex) that no folder claims yet. Not a reservation: create() refuses
    an id taken in between (BotIdTaken), and _install_new answers that as a retry, never an adoption.
    raw_ids, not list_ids: the index drops an id two folders claim, which would read as free."""
    taken = bpaths.raw_ids()
    while True:
        bid = uuid.uuid4().hex[:8]
        if bid in taken:
            continue
        try:
            bpaths.bot_folder(bid)  # a folder place() registered whose bot.json and marker are not written yet
        except LookupError:
            return bid


def _skill_members(path: str, idx: dict) -> list[tuple[str, bytes]]:
    """[(name, bytes)] of every playbook member, read (and size + hash checked) in full."""
    from fused_render.bots.bot import Bot
    _total, text_cap = _caps()
    _check_skill_limits(idx)
    out = [(n, _member_bytes(path, idx, f"skills/{n}.md", text_cap) or b"") for n in _skill_names(idx)]
    _check_skill_limits(idx, [(n, Bot._parse_skill(raw.decode("utf-8", errors="replace"))[2]) for n, raw in out])
    return out


def _case_unique(names) -> tuple[set[str], list[str]]:
    """(names kept, names dropped): of names equal but for case (one file on a case-insensitive disk) only the
    first in sorted order is kept, by the install and by both sides of the digest alike."""
    seen, keep, drop = set(), set(), []
    for n in sorted(names):
        if n.lower() in seen:
            drop.append(n)
        else:
            seen.add(n.lower())
            keep.add(n)
    return keep, drop


def _write_skills(b, members: list[tuple[str, bytes]], warnings: list[str]) -> None:
    """REPLACE the bot's `skills/` with the package's playbooks, each written VERBATIM under its member name (so
    the bot digests like its package, public_digest): built in a scratch dir beside it and swapped in by rename,
    so a failure while writing leaves the old folder whole. A `*.md` file the package lacks is gone afterwards;
    anything else in the folder (not a playbook: export never ships it) is carried over. A playbook with no
    trigger or no steps is still written (it never runs until edited) and said in a warning."""
    from fused_render.bots.bot import Bot
    d = b.skills_dir
    parent = os.path.dirname(d)
    if not members and not os.path.lexists(d):
        return
    os.makedirs(parent, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".skills-new-", dir=parent)  # beside skills/: one filesystem, so rename swaps
    try:
        keep, drop = _case_unique(n for n, _raw in members)
        for n in drop:
            warnings.append(f"playbook {n}: its name differs from another only by case; not installed")
        for n, raw in members:
            if n not in keep:
                continue
            _title, trigger, body = Bot._parse_skill(raw.decode("utf-8", errors="replace"))
            if not trigger or not body:
                warnings.append(f"playbook {n}: it has no {'trigger' if not trigger else 'steps'}; it never runs until you add one")
            with open(os.path.join(tmp, n + ".md"), "xb") as f:
                f.write(raw)
        old = None
        if os.path.lexists(d):
            old = os.path.join(parent, f".skills-old-{uuid.uuid4().hex[:8]}")
            os.rename(d, old)
        try:
            os.rename(tmp, d)
        except OSError:
            if old is not None:
                os.rename(old, d)
            raise
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    if old is None:
        return
    if os.path.islink(old):
        os.unlink(old)
        return
    try:
        for n in os.listdir(old):
            if not (n.endswith(".md") and not n.startswith(".")) and not os.path.lexists(os.path.join(d, n)):
                os.rename(os.path.join(old, n), os.path.join(d, n))
    except OSError:
        logger.warning("bot package: could not carry %s's non-playbook files over", b.id, exc_info=True)
    shutil.rmtree(old, ignore_errors=True)


def _merge_routines(existing, new: list[dict]) -> list[dict]:
    """An update's routines: the package's (`_routines`, in its order) keyed by id. A routine already on the bot
    keeps its private run state (enabled, last, next, fails, runs; every key outside ROUTINE_PUBLIC_KEYS) and its
    `created`; when its schedule is unchanged its `anchor` too, else `next` is recomputed (a once-routine moved to
    a new time is live again, as routine_update does). A new one arrives paused; one the package lacks is gone."""
    from fused_render.bots import store
    from fused_render.bots.bot import Bot
    old = {r["id"]: r for r in existing or [] if isinstance(r, dict) and isinstance(r.get("id"), str)}
    now, out = time.time(), []
    for r in new:
        prev = old.get(r["id"])
        if prev is None:
            out.append(r)
            continue
        row = {k: v for k, v in prev.items() if k not in store.ROUTINE_PUBLIC_KEYS}
        same = prev.get("kind") == r["kind"] and all(prev.get(k) == r.get(k) for k in ("minutes", "time", "weekdays", "at"))
        pub = {k: v for k, v in r.items() if k in store.ROUTINE_PUBLIC_KEYS}
        pub["created"] = prev.get("created") or pub["created"]
        if same and "anchor" in prev and "anchor" in pub:
            pub["anchor"] = prev["anchor"]
        row.update(pub)
        if not same:
            if r["kind"] == "once":
                row["last"] = None
            row["next"] = Bot._next_run(row, now) if row.get("enabled") else None
        out.append(row)
    return out


def _refuse_busy(b) -> None:
    if b.meta.get("status") in ("running", "waiting") or b.running():
        raise BotFileError(f"stop {_str(b.meta.get('name')) or b.id} before updating it")


def _install_update(path: str, idx: dict, pub: dict, target, apps, memory: bool) -> dict:
    """Update `target` in place from the package (docs/bots.md §17): the bot the probe matched (`_find_match`,
    re-derived here; never the Super Bot), refused while it runs or waits. The public half is REPLACED (name,
    model, effort, instructions, the `_APPLIED_KEYS` settings, skills/, routines merged by id), README.md and
    icon.svg swapped when the package has them; the ticked apps install and merge into its manifest and trust;
    memory only when asked. Everything else private (events, files, inbox, browser, other trust) is untouched.
    `origin` is restamped, so re-install mode (the per-row Retry) and the next probe know this package."""
    from fused_render.bots import apptools, registry
    from fused_render.bots import bot as bm
    m = _find_match(_package_id(pub, idx))
    if not isinstance(target, str) or m is None or m["id"] != target:
        raise BotFileError("that bot did not come from this package; install it as a new bot")
    try:
        b = registry.get(target)
    except (ValueError, LookupError):
        raise NoSuchBot(f"no such bot {os.path.basename(target)}") from None
    if bm.is_super(b.meta):
        raise BotFileError("Super Bot is never updated from a package")
    _refuse_busy(b)
    rows = _app_rows(idx)
    # Every read from the file happens before any app installs or the bot is touched (as _install_new).
    _check_text_budget(idx)
    name = _install_name(pub, idx)
    applied = _applied(pub, name)
    routines = _routines(pub.get("routines"))
    skills = _skill_members(path, idx)
    mem_text = None
    if memory:
        _total, text_cap = _caps()
        mem_text = _member_text(path, idx, ".fused/memory.md", text_cap)
    origin = {**_origin(idx), "package_sha": _package_sha(idx), "public_sha": _package_public_sha(path, idx, pub)}

    orig = {id(r): r["folder"] for r in rows}  # the index spelling, before _install_apps follows the disk
    results, done, _paths = _install_apps(path, idx, rows, apps)
    warnings: list[str] = []
    with b.lock:
        _refuse_busy(b)  # again: a task may have started while the apps installed
        snapshot = copy.deepcopy(b.meta)
        try:
            _write_skills(b, skills, warnings)  # first: a failure here leaves the bot as it was
            for k in _APPLIED_KEYS:
                b.meta.pop(k, None)
            b.meta.update(applied)
            b.meta["routines"] = _merge_routines(b.meta.get("routines"), routines)
            b.meta["apps"] = _merge_manifest(b.meta.get("apps"), rows, orig)
            apptools.set_trusted_apps(b.meta, add=[r["folder"] for r in done])
            b.meta["origin"] = origin
            b.save()
        except Exception as e:  # noqa: BLE001 - no half-applied meta in memory; the page re-POSTs (idempotent)
            logger.warning("bot package: update of %s failed", b.id, exc_info=True)
            b.meta.clear()
            b.meta.update(snapshot)
            raise BotFileError(f"could not update {_str(b.meta.get('name')) or b.id}: {e or e.__class__.__name__}") from e
        _copy_folder_members(path, idx, b.id, warnings, replace=True)
        if mem_text is not None:
            try:
                b.set_memory(mem_text)
            except OSError as e:
                logger.warning("bot package: memory not replaced for %s", b.id, exc_info=True)
                warnings.append(f"memory: {e or e.__class__.__name__}")
    try:
        b._sync_wake()
    except Exception:  # noqa: BLE001 - the scheduler's next pass picks the routines up anyway
        logger.debug("bot package: wake sync failed for %s", b.id, exc_info=True)
    return {"ok": True, "bot_id": b.id, "updated": True, "apps": results, "warnings": warnings}


def _install_new(path: str, idx: dict, pub: dict, apps, memory: bool) -> dict:
    """The ticked apps first (in the given order), then a NEW bot carrying the file's settings, playbooks
    and (when asked) memory, under the package's id when that is free here (else a minted one: the user chose
    "install as a new bot" over the bot already holding it). An app that fails is reported on its row and the
    bot is still made: the page's Retry for that row is re-install mode (`bot_id`)."""
    from fused_render.bots import apptools
    from fused_render.bots import bot as bm
    from fused_render.bots import store
    rows = _app_rows(idx)
    # Everything read from the file is read BEFORE any app is installed and before the bot exists: a bad member
    # fails the request while nothing irreversible has happened, so a retry is still harmless.
    _check_text_budget(idx)
    settings = _public_fields({}, pub)
    routines = _routines(pub.get("routines"))
    skills = _skill_members(path, idx)
    public_sha = _package_public_sha(path, idx, pub)
    mem_text = None
    if memory:
        _total, text_cap = _caps()
        mem_text = _member_text(path, idx, ".fused/memory.md", text_cap)

    results, done, paths = _install_apps(path, idx, rows, apps)
    # Trust ("use without asking") only what THIS call installed or found installed by its own install_target
    # probe: a row the user left unticked, or that only CLAIMS an app id / slug / key in the index, grants
    # nothing — the index is the file author's word, and trust is the importer's to give.
    trusted = [r["folder"] for r in done]

    # Once the bot's `origin` is on disk the request always answers 200 with the bot id: the page retries a
    # non-200 by re-POSTing, which would mint a second bot. Later failures become `warnings`. Before that (create
    # refused, or the origin save failed and the bot was deleted again) no bot remains, so a non-200 and a
    # re-POST are safe. create() itself can raise AFTER its bot.json exists (the registry build, the first emit),
    # so the id is minted HERE and handed in: after a raise, a bot.json under THAT id carrying the name we
    # passed is this request's bot. (Diffing the ids on disk would adopt a bot some other request created in the
    # same window, and overwrite its settings below.) The mint is not a reservation: an id another creator took
    # in between is BotIdTaken, refused before create writes anything, and answered as a retry, never adopted.
    # The package id travels (docs/bots.md §17: an id is a bot's lineage, like an app's app_id) unless a bot here
    # holds it, i.e. the user chose "install as a new bot" over it: then a minted one. A free id taken in between
    # is BotIdTaken below, a retry (which then mints), never an adoption.
    name = _install_name(pub, idx)
    pkg_id = _package_id(pub, idx)
    bid = pkg_id if pkg_id and pkg_id not in bpaths.raw_ids() and not _has_folder(pkg_id) else _mint_id()
    warnings: list[str] = []
    try:
        b = bm.create(name, _str(pub.get("model")), _str(pub.get("effort")), _str(pub.get("instructions")),
                      greet=False, bot_id=bid)
    except bm.BotIdTaken:
        raise BotFileError("could not create the bot: id taken, retry") from None
    except Exception as e:  # noqa: BLE001 - see above
        from fused_render.bots import registry
        try:
            ours = bm.Bot._exists(bid) and store.read_meta(bid).get("name") == name
        except (OSError, ValueError):
            ours = False
        if not ours:
            raise BotFileError(f"could not create the bot: {e or e.__class__.__name__}") from e
        logger.warning("bot package: create() raised after writing bot %s; continuing", bid, exc_info=True)
        warnings.append(f"create: {e or e.__class__.__name__}")
        b = registry.get(bid)

    # `origin` first, in a save of its own: re-install mode (the per-row Retry) checks it, so it must be durable
    # before any later save can fail. Failing here is a failed request: the bot is removed again (nothing else of
    # the file was written to it yet) so the page's re-POST does not leave a second bot behind.
    try:
        with b.lock:
            # package_sha (private, like all of origin): re-install mode accepts only this very package.
            # public_sha: the package's public digest, the probe's "edited here since" mark (docs/bots.md §17).
            b.meta["origin"] = {**_origin(idx), "package_sha": _package_sha(idx), "public_sha": public_sha}
            b.save()
    except Exception as e:  # noqa: BLE001 - any failure: undo the bot, then refuse
        logger.warning("bot package: could not record the origin of %s; removing it", b.id, exc_info=True)
        try:
            bm.delete(b.id)
        except Exception:  # noqa: BLE001 - a cleanup failure must not mask the origin error
            logger.warning("bot package: could not remove %s after a failed origin save", b.id, exc_info=True)
        raise BotFileError(f"could not create the bot: {e or e.__class__.__name__}") from e

    def step(what, fn) -> bool:
        try:
            fn()
            return True
        except Exception as e:  # noqa: BLE001 - the bot exists: report, never fail the request (comment above)
            logger.warning("bot package: %s failed for %s", what, b.id, exc_info=True)
            warnings.append(f"{what}: {e or e.__class__.__name__}")
            return False

    def apply_meta():
        with b.lock:
            b.meta.update(settings)
            b.meta["routines"] = routines
            b.save()

    def link_apps():
        # Its own save; origin is already durable (saved alone above), so when THIS one fails (or the settings
        # save did too) each installed row says so (state "settings") and its Retry, re-install mode, finds the
        # origin on disk even after the bot is reloaded.
        with b.lock:
            b.meta["apps"] = rows
            b.meta["trusted_apps"] = apptools.clean_trusted_apps(trusted)
            b.save()

    if step("settings", apply_meta):
        _copy_folder_members(path, idx, b.id, warnings)
    try:
        link_apps()
    except Exception as e:  # noqa: BLE001 - the bot exists: report, never fail the request (comment above)
        logger.warning("bot package: settings failed for %s", b.id, exc_info=True)
        warnings.append(f"settings: {e or e.__class__.__name__}")
        _unlinked(results, paths, e)
    step("playbooks", lambda: _write_skills(b, skills, warnings))
    if mem_text:
        step("memory", lambda: b.set_memory(mem_text))
    step("transcript", lambda: b.emit("system", f"Imported from {os.path.basename(path)}"))
    step("greeting", b.greet)
    return {"ok": True, "bot_id": b.id, "updated": False, "apps": results, "warnings": warnings}
