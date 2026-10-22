"""The provider registry: every `providers/<key>/provider.json`, validated and
exposed as `Provider` records (SPEC-connect-oauth §2, §11).

**Adding a tool is adding a folder.** The registry finds packs by folder name,
in the built-in `providers/` next to this file and in every directory named by
`FUSED_RENDER_CONNECT_PROVIDERS` (`os.pathsep`-separated). An entry there may
be a pack itself (it holds a `provider.json`) or a folder of packs; a pack
there with the same key as a built-in one replaces it, which is how a test or
a developer tries an edited pack without touching the install.

**What a pack may say is checked here, once, by `validate`**, so the manager,
the routes and the page can index fields without guarding each one. A bad
pack is a warning in the log and a missing card, never a crash of the whole
list: one broken folder (a hand edit, a half-written file) must not take every
other provider's card down with it. Each distinct problem is logged once per
process, because the page asks for the list every second while a flow runs.

**Two load-time rewrites, both documented contract:**

- Keys starting with `_` are dropped at every level. JSON has no comments, so
  `"_comment": "..."` is how a pack explains itself (GitHub's empty client id).
- `${VAR}` inside any string value is replaced by the environment variable's
  value. An unset variable is left as written, so a URL that still holds one
  fails validation by name instead of becoming a relative path. The test fake
  vendor's pack uses `${FUSED_RENDER_FAKE_VENDOR_URL}` this way; shipped packs
  have no reason to.

**Client ids from the environment.** A kind whose `client_id` is `""` in the
file reads `FUSED_RENDER_CONNECT_<KEY>_CLIENT_ID` (key upper-cased, `-` to
`_`) at load time. Still empty, the kind is listed but `availability()` says
it cannot run, and why. That is how a pack ships before Fused has registered
its app with the vendor, without a placeholder id that would fail at the
vendor's consent page instead of on the card.

A JSON Schema file plus a validator package was considered and rejected: it
adds a dependency to say what thirty lines of Python say, and its messages
name a JSON pointer rather than the file and the field a pack author needs.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import re
import shutil
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BUILTIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers")
PROVIDERS_ENV = "FUSED_RENDER_CONNECT_PROVIDERS"

KINDS = ("mcp-oauth", "oauth-pkce", "oauth-device", "cli", "api-key")

#: kind -> (required fields, optional fields). Anything else in an auth entry
#: is a typo until proven otherwise, so it is refused by name.
_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "mcp-oauth": (("resource",), ("client_id", "client", "label")),
    "oauth-pkce": (
        ("client_id", "authorize_url", "token_url", "redirect"),
        ("scopes", "identity_url", "revoke_url", "client_secret",
         "extra_authorize", "extra_token", "byo", "label", "identity_method"),
    ),
    "oauth-device": (
        ("client_id", "device_url", "token_url", "scope", "identity_url"),
        ("revoke_url", "byo", "label", "identity_method"),
    ),
    "cli": (("command", "identity_url"), ("label", "identity_method")),
    "api-key": (("label", "identity_url"), ("docs", "header", "identity_method")),
}
_URL_FIELDS = ("resource", "authorize_url", "token_url", "revoke_url",
               "identity_url", "device_url", "docs")
_TOP = ("name", "auth", "tools", "privacy")
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_warned: set[tuple[str, str]] = set()


@dataclass
class Provider:
    key: str            # folder name
    name: str
    dir: str            # absolute pack folder
    auth: list[dict]    # ordered; first is the default
    tools: dict         # {"remote": url|None, "native": bool}  (native = pack has tools.py)
    privacy: str
    icon: str | None    # absolute path to icon.svg or None


# ------------------------------------------------------------- validation ---

def _is_url(v) -> bool:
    return isinstance(v, str) and v.startswith(("https://", "http://"))


def validate(d: dict, where: str) -> None:
    """Raise ValueError naming `where` (the file) and the offending field.

    Run on the pack AFTER `_`-key stripping and `${VAR}` expansion, which is
    the shape every consumer sees.
    """
    def bad(field: str, why: str):
        raise ValueError(f"{where}: {field}: {why}")

    if not isinstance(d, dict):
        bad("(top level)", "must be a JSON object")
    for k in d:
        if k not in _TOP:
            bad(k, f"unknown key (allowed: {', '.join(_TOP)}; prefix '_' for comments)")
    if not isinstance(d.get("name"), str) or not d["name"].strip():
        bad("name", "required, a non-empty string")
    if "privacy" in d and not isinstance(d["privacy"], str):
        bad("privacy", "must be a string")

    auth = d.get("auth")
    if not isinstance(auth, list) or not auth:
        bad("auth", "required, a non-empty list")
    for i, a in enumerate(auth):
        at = f"auth[{i}]"
        if not isinstance(a, dict):
            bad(at, "must be an object")
        kind = a.get("kind")
        if kind not in KINDS:
            bad(f"{at}.kind", f"must be one of {', '.join(KINDS)}, got {kind!r}")
        required, optional = _FIELDS[kind]
        for f in required:
            if f not in a:
                bad(f"{at}.{f}", f"required for kind {kind}")
        for f in a:
            if f != "kind" and f not in required and f not in optional:
                bad(f"{at}.{f}", f"unknown field for kind {kind}")
        for f in _URL_FIELDS:
            if f in a and not _is_url(a[f]):
                bad(f"{at}.{f}", f"must be an http(s) URL, got {a[f]!r}")
        if "identity_method" in a and a["identity_method"] not in ("GET", "POST"):
            bad(f"{at}.identity_method", "must be GET or POST")
        if "client_id" in a and not isinstance(a["client_id"], str):
            bad(f"{at}.client_id", "must be a string (\"\" = from the environment)")
        for f in ("label", "scope", "client_secret", "header"):
            if f in a and not isinstance(a[f], str):
                bad(f"{at}.{f}", "must be a string")
        for f in ("scopes", "extra_authorize", "extra_token"):
            if f in a:
                v = a[f]
                if not isinstance(v, dict) or not all(
                        isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
                    bad(f"{at}.{f}", "must be an object of string values")
        if "redirect" in a:
            r = a["redirect"]
            if not isinstance(r, dict):
                bad(f"{at}.redirect", "must be an object {host, ports, path}")
            if not isinstance(r.get("host"), str) or not r["host"]:
                bad(f"{at}.redirect.host", "required, a string")
            if not isinstance(r.get("path"), str) or not r["path"].startswith("/"):
                bad(f"{at}.redirect.path", "required, a path starting with '/'")
            ports = r.get("ports", None)
            if ports is not None and (
                    not isinstance(ports, list) or not ports
                    or not all(isinstance(p, int) and not isinstance(p, bool)
                               and 0 < p < 65536 for p in ports)):
                bad(f"{at}.redirect.ports", "must be null or a non-empty list of port numbers")
        if "byo" in a:
            b = a["byo"]
            if not isinstance(b, dict) or not all(isinstance(x, str) for x in b.values()):
                bad(f"{at}.byo", "must be an object {manifest?, docs?} of strings")
        if "client" in a and (not isinstance(a["client"], list) or not all(
                x in ("cimd", "dcr", "prompt") for x in a["client"])):
            bad(f"{at}.client", "must be a list drawn from cimd, dcr, prompt")
        if kind == "cli":
            c = a["command"]
            if not isinstance(c, list) or not c or not all(isinstance(x, str) and x for x in c):
                bad(f"{at}.command", "must be a non-empty argv list of strings")

    tools = d.get("tools", {})
    if not isinstance(tools, dict):
        bad("tools", "must be an object {remote, app?}")
    for k in tools:
        if k not in ("remote", "app"):
            bad(f"tools.{k}", "unknown key (allowed: remote, app; native comes from tools.py)")
    if tools.get("remote") is not None and not _is_url(tools["remote"]):
        bad("tools.remote", f"must be null or an http(s) URL, got {tools['remote']!r}")
    if tools.get("app") is not None and not isinstance(tools["app"], str):
        bad("tools.app", "must be null or a starter key")


# ---------------------------------------------------------------- loading ---

def _strip_private(v):
    if isinstance(v, dict):
        return {k: _strip_private(x) for k, x in v.items() if not str(k).startswith("_")}
    if isinstance(v, list):
        return [_strip_private(x) for x in v]
    return v


def _expand(v):
    if isinstance(v, str):
        return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), v)
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x) for x in v]
    return v


def client_id_env(key: str) -> str:
    """The environment variable that fills an empty `client_id` for `key`."""
    return "FUSED_RENDER_CONNECT_" + key.upper().replace("-", "_") + "_CLIENT_ID"


def _load(pack_dir: str) -> Provider:
    key = os.path.basename(os.path.normpath(pack_dir))
    path = os.path.join(pack_dir, "provider.json")
    if not _KEY_RE.match(key):
        raise ValueError(f"{path}: folder name {key!r} is not a provider key (a-z, 0-9, -, _)")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: unreadable JSON: {exc}") from None
    d = _expand(_strip_private(raw))
    validate(d, path)
    env_id = (os.environ.get(client_id_env(key)) or "").strip()
    auth = []
    for a in d["auth"]:
        a = dict(a)
        if "client_id" in a and not a["client_id"] and env_id:
            a["client_id"] = env_id
        auth.append(a)
    icon = os.path.join(pack_dir, "icon.svg")
    tools = d.get("tools", {})
    out_tools = {"remote": tools.get("remote"),
                 "native": os.path.isfile(os.path.join(pack_dir, "tools.py"))}
    if tools.get("app"):
        out_tools["app"] = tools["app"]
    return Provider(key=key, name=d["name"], dir=os.path.abspath(pack_dir), auth=auth,
                    tools=out_tools, privacy=d.get("privacy", ""),
                    icon=icon if os.path.isfile(icon) else None)


def _pack_dirs() -> list[str]:
    """Every candidate pack folder, built-ins first so env packs override."""
    roots = [BUILTIN_DIR] + [p for p in (os.environ.get(PROVIDERS_ENV) or "").split(os.pathsep) if p]
    out: list[str] = []
    for root in roots:
        if os.path.isfile(os.path.join(root, "provider.json")):
            out.append(root)
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            if root != BUILTIN_DIR:
                _warn_once(root, f"{PROVIDERS_ENV} entry {root} is not a readable folder")
            continue
        for n in names:
            d = os.path.join(root, n)
            if os.path.isfile(os.path.join(d, "provider.json")):
                out.append(d)
    return out


def _warn_once(where: str, msg: str) -> None:
    if (where, msg) not in _warned:
        _warned.add((where, msg))
        logger.warning("connect: skipping provider pack: %s", msg)


_CACHE_TTL_S = 2.0
_cache: tuple[float, str, list] | None = None     # (read_at, env value, providers)
_cache_lock = threading.Lock()


def providers() -> list[Provider]:
    """Built-in packs + every pack in $FUSED_RENDER_CONNECT_PROVIDERS, sorted
    by key. Re-read from disk after `_CACHE_TTL_S` (a handful of small files,
    so an edited pack shows on the next poll), and at once when the env var
    changes: the bots' roster calls this several times per step, which is
    what the cache is for."""
    global _cache
    env = os.environ.get("FUSED_RENDER_CONNECT_PROVIDERS", "")
    now = time.monotonic()
    with _cache_lock:
        if _cache is not None and _cache[1] == env and now - _cache[0] < _CACHE_TTL_S:
            return list(_cache[2])
    found = _read_packs()
    with _cache_lock:
        _cache = (now, env, found)
    return list(found)


def _read_packs() -> list[Provider]:
    found: dict[str, Provider] = {}
    for d in _pack_dirs():
        try:
            p = _load(d)
        except ValueError as exc:
            _warn_once(d, str(exc))
            continue
        found[p.key] = p
    return [found[k] for k in sorted(found)]


def get(key) -> Provider | None:
    for p in providers():
        if p.key == key:
            return p
    return None


# ----------------------------------------------------------- availability ---

def availability(provider: Provider, entry: dict) -> tuple[bool, str | None]:
    """(available, reason) for one auth entry of `provider` — static checks
    only, cheap enough for every poll. Added beside the contract for the
    manager's `auth: [{kind, available, reason?}]`.

    - `oauth-pkce` / `oauth-device` need a `client_id`, from the file or the
      environment (already merged into `entry` at load time).
    - `cli` needs its command on PATH. Whether it prints a token is a probe
      (it runs the vendor's CLI) and belongs to login, not to a listing.
    - `mcp-oauth` and `api-key` are always available: the first registers
      itself at sign-in, the second is a paste.
    """
    kind = entry.get("kind")
    if kind in ("oauth-pkce", "oauth-device") and not entry.get("client_id"):
        if entry.get("byo"):
            return False, (f"no client id: create your own app from the manifest, "
                           f"or set {client_id_env(provider.key)}")
        return False, f"no client id shipped yet: set {client_id_env(provider.key)}"
    if kind == "cli":
        exe = (entry.get("command") or [""])[0]
        if not shutil.which(exe):
            return False, f"`{exe}` is not installed"
    return True, None
