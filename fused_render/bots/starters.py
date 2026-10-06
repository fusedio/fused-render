"""Starter apps (port of OpenBot `installapp.py`, docs/bots.md §5): fused apps
that ship inside this package under `bots/starters/<key>/`, ready to copy into
the apps folder (`~/Fused/app`) so a bot has its tools on a fresh machine
without building anything. Add a folder to add a starter; each is a complete
fused-render app (index.html with the marker, a pyproject.toml, an mcp.toml for
its tools) plus an optional `starter.json`:

    {"setup_tool": "docs_status", "ready_key": "connected"}

`setup_tool` names one of the app's own read tools whose result carries
`ready_key`; the Apps panel shows "Needs setup" until that value is true (the
Google apps need a service-account key pasted once). Without it the app counts
as ready once installed.

* `starters()`          the catalog: [{key, src, name, desc, version, tools, icon, setup_tool, ready_key}]
* `list_state(root)`    the catalog with each starter's install state under `root` (no `src`)
* `install(key, root)`  copy to `<root>/<key>` unless that folder exists (then it is left alone);
                        records the version in `<dir>/.fused/starter.json`.
* `install(..., update=True)`  copy the starter's files over an installed copy (only when the user
                        asked: a bot's `build` may have edited it). `.fused/`, the venv and anything
                        the starter does not ship are kept.
* `ensure(keys, root)`  install the missing ones; never raises (a preset still applies).
* `status(root)`        run each installed starter's `setup_tool`: ({key: True|False|None}, {key: why}).
"""
from __future__ import annotations

import json
import os
import shutil
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STARTERS_DIR = os.path.join(HERE, "starters")
SKIP = {".venv", ".fused", "__pycache__", ".DS_Store", ".pytest_cache"}
STATUS_TIMEOUT_S = 8


def _pyproject(d):
    """[project].version and [tool.fused-render.bot] name/description, read with tomllib."""
    try:
        import tomllib
        with open(os.path.join(d, "pyproject.toml"), "rb") as f:
            t = tomllib.load(f)
    except Exception:  # noqa: BLE001 — a broken pyproject just means no metadata
        return {}
    bot = ((t.get("tool") or {}).get("fused-render") or {}).get("bot") or {}
    return {"version": str((t.get("project") or {}).get("version") or ""), "name": str(bot.get("name") or ""),
            "desc": str(bot.get("description") or "")}


def _marker(d):
    try:
        with open(os.path.join(d, "index.html"), "rb") as f:
            return 'name="fused-app"' in f.read(4096).decode("utf-8", "replace")
    except OSError:
        return False


def _installed_record(dir_):
    try:
        with open(os.path.join(dir_, ".fused", "starter.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def starters():
    """Every starter folder, sorted by key: the catalog the Apps panel and presets read."""
    from fused_render.bots import apptools
    out = []
    try:
        keys = sorted(k for k in os.listdir(STARTERS_DIR) if not k.startswith("."))
    except OSError:
        return out
    for key in keys:
        d = os.path.join(STARTERS_DIR, key)
        if not os.path.isdir(d) or not _marker(d):
            continue
        pp = _pyproject(d)
        meta = {}
        try:
            with open(os.path.join(d, "starter.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        if not isinstance(meta, dict):
            meta = {}
        icon = next((i for i in ("icon.svg", "icon.png") if os.path.isfile(os.path.join(d, i))), "")
        out.append({"key": key, "src": d, "name": pp.get("name") or key.replace("-", " ").title(), "desc": pp.get("desc", ""),
                    "version": pp.get("version", ""), "tools": apptools.count_tools(d), "icon": icon,
                    "setup_tool": str(meta.get("setup_tool") or ""), "ready_key": str(meta.get("ready_key") or "connected")})
    return out


def get(key):
    """The catalog entry for `key`, else None (keys come from os.listdir, so never `..`)."""
    return next((s for s in starters() if s["key"] == key), None)


def _with_state(s, root):
    dir_ = os.path.join(root, s["key"])
    installed = os.path.isdir(dir_) and _marker(dir_)
    rec = _installed_record(dir_) if installed else {}
    iv = str(rec.get("version") or "")
    return {**{k: v for k, v in s.items() if k != "src"}, "installed": installed, "dir": dir_ if installed else "",
            "installed_version": iv, "update": bool(installed and rec and iv != s["version"])}


def _need_root(root):
    if not root or not os.path.isabs(root):
        raise ValueError("starter apps need an absolute apps root")


def list_state(root):
    """{root, starters: [{key, name, desc, version, tools, icon, setup_tool, ready_key,
    installed, dir, installed_version, update}]}."""
    _need_root(root)
    return {"root": root, "starters": [_with_state(s, root) for s in starters()]}


def _copy(src, dst):
    def ignore(_d, names):
        return [n for n in names if n in SKIP]
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def _record(dst, s):
    os.makedirs(os.path.join(dst, ".fused"), exist_ok=True)
    tmp = os.path.join(dst, ".fused", "starter.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"starter": s["key"], "version": s["version"], "installed_at": time.time()}, f)
    os.replace(tmp, os.path.join(dst, ".fused", "starter.json"))


def install(key, root, update=False):
    """Copy starter `key` under `root`. Returns {key, dir, installed: bool (did a copy happen), existed: bool, name}."""
    s = get(key)
    if not s:
        raise ValueError(f"unknown starter {key!r}")
    _need_root(root)
    dst = os.path.join(root, key)
    existed = os.path.exists(dst)
    if existed and not update:
        return {"key": key, "dir": dst, "installed": False, "existed": True, "name": s["name"]}
    os.makedirs(root, exist_ok=True)
    _copy(s["src"], dst)
    _record(dst, s)
    return {"key": key, "dir": dst, "installed": True, "existed": existed, "name": s["name"]}


def ensure(keys, root):
    """Install every starter in `keys` that is missing under `root`; returns the names of the ones
    installed now. Never raises: a preset must still be applied when a starter cannot be copied."""
    done = []
    for k in keys or []:
        try:
            r = install(k, root)
        except (ValueError, OSError):
            continue
        if r["installed"]:
            done.append(r["name"])
    return done


def status(root):
    """({key: True|False|None}, {key: reason}) for each installed starter with a setup_tool.
    None when the tool could not be run (app tools unavailable, not in the registry, timeout,
    error); the reason says which. Each tool is capped at STATUS_TIMEOUT_S."""
    _need_root(root)
    out, why = {}, {}
    try:
        from fused_render.bots import apptools
    except Exception as e:  # noqa: BLE001
        apptools, err = None, f"apptools import failed: {e}"
    else:
        err = "" if apptools.available() else "app tools unavailable on this interpreter"
    recs = None
    for s in starters():
        st = _with_state(s, root)
        if not st["installed"] or not s["setup_tool"]:
            continue
        ready, reason = None, err
        if not err:
            try:
                if recs is None:
                    recs = apptools.registry(force=True)
                rec = apptools.find(recs, s["key"], s["setup_tool"])
                if not rec:
                    reason = f"tool {s['setup_tool']} not in the registry ({len(recs)} tools known)"
                else:
                    r = apptools.run_tool(rec, {}, timeout_s=STATUS_TIMEOUT_S)
                    if r.ok:
                        ready = bool(json.loads(r.text).get(s["ready_key"]))
                    else:
                        reason = r.text[:300]
            except Exception as e:  # noqa: BLE001
                reason = f"{type(e).__name__}: {e}"
        out[s["key"]] = ready
        if ready is None:
            why[s["key"]] = reason
    return out, why
