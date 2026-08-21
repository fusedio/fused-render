"""What tools exist on this machine — the palette behind `workflow/template.html`.

`main(action="tools")` answers with every curated MCP tool the user has, across
every app folder, in the shape the canvas draws: one entry per `[[tool]]` table
in every `mcp.toml` the machine can be shown to hold. The `mcp` panel authors one
folder's manifest; this reads ALL of them, because a workflow's whole point is
chaining a tool from one app into a tool from another.

Three sources, and the split is the design:

1. **The file index** — `SELECT path FROM files WHERE name = 'mcp.toml'`. This is
   the only source that can find an app folder the user keeps somewhere this
   template would never think to look, and it costs one indexed query instead of
   a walk of the disk. `git_repos.py:_repos` does exactly this for `.git`.
2. **The workspace** — `<~/Fused>/<tag>/<name>`, two levels, one `scandir` each.
3. **`registered_apps.json`** — the folders the user opened through "Open app",
   which live outside the workspace by definition.

(2) and (3) are AUTHORITATIVE and never stale: they are a listing and a small
JSON file, read at the moment of the call. (1) is a cache of the filesystem and
is routinely behind it — a folder curated a minute ago is genuinely not in it
yet. So the payload reports the index's state as its own field rather than
folding it into the answer, and the union is deduplicated by `realpath`.

**"No tools" and "no index" are different answers and the payload keeps them
apart.** That is the readiness rule the whole index surface is built around
(`skills/fused-render-index/SKILL.md`): an index query returns zero rows both
when nothing matches and when nothing has been indexed, and rendering the second
as the first is a silent lie. `index.available` / `index.reason` carry the
distinction, and `action="rescan"` is offered so the panel can DO something about
it — deliberately not fired automatically on load, because a full machine rescan
is minutes of I/O and nobody asked for one by opening a canvas.

**The parameter list comes from the manifest's `signature` snapshot, minus the
pins**, and never from the MCP server's own input schema. That is not a shortcut,
it is the only source that works: a dispatcher entrypoint gives every parameter a
default, so `fused app serve` reports `required: []` and — measured on
`open-mail`'s `send_mail` — 22 properties, which tells a canvas nothing about
which three of them this node is actually about. The snapshot at least carries
each parameter's annotation and default, and the node's own `inputs` list
(`condition.py`) records the author's choice on top of it.

Never a raise: every failure is a refusal payload (`{ok: false, reason, message}`)
or a per-app `error` field, the same contract as `mcp/manifest.py`. A folder whose
manifest is broken is reported as a broken folder, not as a missing one, and never
takes the other twenty down with it.

stdlib only apart from `duckdb` (optional — see `_index_apps`) and
`../shared/appenv.py`. Nothing here imports fused_render (SPEC PY-15 / D166).
"""

import ast
import json
import os
import sys

_MANIFEST = "mcp.toml"

# How many `mcp.toml` paths the index query may return. An app folder per row;
# a machine with more than this has something else going on, and the palette
# could not render them anyway.
_MAX_INDEX_ROWS = 500

# The workspace is `<root>/<tag>/<name>` — exactly two levels, and these cap each
# so a workspace someone has filled with thousands of folders cannot turn a
# palette load into a directory-listing storm.
_MAX_TAGS = 200
_MAX_APPS_PER_TAG = 500

# Bounded read of a manifest. `mcp.toml` is a handful of tables; anything at this
# size is not one.
_MANIFEST_LIMIT = 1024 * 1024

# Bounded read of an entrypoint file, only ever to re-derive a signature the
# manifest did not record (a hand-written manifest with no snapshot).
_SOURCE_LIMIT = 1024 * 1024

_DEFAULT_ENTRYPOINT = "main"

# Seconds to wait on the local index HTTP calls. These talk to the server that is
# already running this subprocess, on loopback; a stall past this means something
# is wrong with it and the disk sources answer perfectly well without it.
_HTTP_TIMEOUT = 5.0
# A rescan POST returns as soon as the run is queued, but the route does real
# setup first, so it gets a little more room than a status GET.
_SCAN_TIMEOUT = 20.0


def _shared():
    """Put `../shared` on the path and return the `appenv` module.

    Guarded insert, like every other template does it: this module is exec'd
    standalone and may be exec'd repeatedly in one process.
    """
    shared = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    import appenv

    return appenv


def _ok(**extra) -> dict:
    out = {"ok": True}
    out.update(extra)
    return out


def _refuse(reason: str, message: str) -> dict:
    """A refusal payload — the panel renders it (never an exception)."""
    return {"ok": False, "reason": reason, "message": message}


def _toml():
    """The TOML parser, or None: stdlib `tomllib` on 3.11+, else `tomli`.

    The same two-name lookup as `mcp/manifest.py::_toml`, and None rather than a
    raise for the same reason: this backend may be running in a project venv
    (SPEC PY-16) that declares its own dependencies and need not carry `tomli`.
    The caller has a payload for it.
    """
    try:
        import tomllib

        return tomllib
    except ImportError:
        try:
            import tomli

            return tomli
        except ImportError:
            return None


# ---------------------------------------------------------------------------
# sources — where an app folder can be found
# ---------------------------------------------------------------------------


def _http_json(url: str, payload=None, timeout: float = _HTTP_TIMEOUT):
    """One JSON call to the local server, or None.

    `X-Fused: 1` rides every POST (`server/common.py::_require_fused` — the
    cross-origin-preflight tripwire, not authentication). None on ANY failure:
    no server around, a refused POST, a body that is not JSON. Every caller has
    an answer for None, because the disk sources below do not need the server at
    all and the index is an optimisation, never a dependency.
    """
    import urllib.error
    import urllib.request

    data = None
    headers = {"X-Fused": "1"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return json.loads(res.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _index_location() -> str:
    """The store directory the SERVER is actually using, or `""`.

    Asked rather than derived. `home_dir()` nests under `branches/<ref>/` on a
    dev worktree (`shell/storage.py`), so a reader that resolves the path itself
    reports "no index" against a store sitting right there — the exact drift
    `skills/fused-render-index/SKILL.md` warns about. The env fallback in that
    skill's snippet exists for a template running with no server; here there IS
    a server (it is what spawned this process), so its own answer is the one to
    take, and `""` means take the reader's default.
    """
    appenv = _shared()
    origin = appenv.origin()
    if not origin:
        return ""
    out = _http_json(origin + "/api/index/config")
    if isinstance(out, dict) and isinstance(out.get("location"), str):
        return out["location"]
    return ""


def _index_connect(location: str):
    """`(con, manifest)` over the index's parquet, or `(None, None)`.

    Copied from `skills/fused-render-index/SKILL.md`'s reader, with its one
    non-negotiable rule intact: **the partition list comes from
    `partitions.json`, never from a `files/*.parquet` glob.** Compaction leaves
    the previous generation on disk beside the new one for readers still holding
    the old manifest, so a glob picks up both and silently DOUBLE-COUNTS. Nothing
    errors; every answer is just wrong, and only on a store that has been scanned
    more than once — so it passes on a fresh machine and fails on the user's.

    `(None, None)` — never a raise — for every "nothing to read yet" shape: no
    manifest, an unreadable one, a manifest naming zero partitions, or no
    `dirs.parquet`. That is a STATE to render, not an error.
    """
    try:
        import duckdb
    except ImportError:
        return None, None
    if not location:
        # The reader's own fallback, only reached when no server published an
        # origin. Deliberately un-branch-aware: a template with no server around
        # has no branch to resolve either.
        home = os.environ.get("FUSED_RENDER_HOME") or os.path.expanduser("~/.fused-render")
        location = os.path.join(home, "index")
    try:
        with open(os.path.join(location, "partitions.json"), "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None, None
    parts = [os.path.join(location, "files", p["file"])
             for p in (manifest.get("partitions") or []) if isinstance(p, dict) and p.get("file")]
    dirs = os.path.join(location, "dirs.parquet")
    if not parts or not os.path.exists(dirs):
        return None, None
    try:
        con = duckdb.connect()
        # NEVER a files/*.parquet glob — see above.
        con.read_parquet(parts).create_view("files")
        con.read_parquet(dirs).create_view("dirs")
    except Exception:  # noqa: BLE001 — a store mid-compaction is a state, not an error
        return None, None
    return con, manifest


def _index_folders() -> tuple:
    """`(folders, state)` — every folder holding an `mcp.toml`, per the index.

    `state` is what the panel renders when `folders` is empty, and it is the
    whole reason this returns a pair: `available: False` means "the index cannot
    answer", which must NOT be drawn as "you have no tools". `reason` is one of
    `no-duckdb`, `no-index`, `query-failed`, or `""` when the answer is real.

    The query is a `name` equality, which is the column the index stores exactly
    so this kind of question is an indexed lookup rather than a `path LIKE '%/…'`
    scan — the same query `server/routers/git_repos.py:246` runs for `.git`.
    """
    location = _index_location()
    con, manifest = _index_connect(location)
    if con is None:
        try:
            import duckdb  # noqa: F401
        except ImportError:
            return [], {"available": False, "reason": "no-duckdb", "location": location,
                        "message": "duckdb is not installed in this file's Python "
                                   "environment, so the file index cannot be read here. "
                                   "Apps in ~/Fused and your opened apps are still listed."}
        return [], {"available": False, "reason": "no-index", "location": location,
                    "message": "No file index has been built yet, so app folders "
                               "outside ~/Fused cannot be found. Scan to look for them."}
    try:
        rows = con.execute(
            "SELECT dir FROM files WHERE name = ? LIMIT ?",
            [_MANIFEST, _MAX_INDEX_ROWS]).fetchall()
    except Exception as exc:  # noqa: BLE001 — a duckdb error is a message, not a crash
        return [], {"available": False, "reason": "query-failed", "location": location,
                    "message": "The file index could not be queried: %s" % exc}
    finally:
        try:
            con.close()
        except Exception:  # noqa: BLE001
            pass
    return [r[0] for r in rows if r and r[0]], {
        "available": True, "reason": "", "location": location,
        "updated": (manifest or {}).get("updated"),
        "message": ""}


def _workspace_folders() -> list:
    """`<~/Fused>/<tag>/<name>` folders that hold an `mcp.toml`.

    Two `scandir` levels and an `isfile` per candidate — never a walk. This is
    the layout `app_listing.workspace_apps` and `claude/agent.py::_app_dir_for`
    both encode; the rule is duplicated here rather than imported because a
    template must not import fused_render (D166), and it is duplicated at
    exactly two levels so it cannot drift into a recursive search.

    Authoritative: this is a listing taken now, so a folder curated a second ago
    is in it. That is what makes the index's staleness survivable.
    """
    appenv = _shared()
    root = appenv.workspace_dir()
    out = []
    try:
        with os.scandir(root) as tags:
            tag_names = sorted(
                e.name for e in tags if e.is_dir() and not e.name.startswith("."))
    except OSError:
        return []
    for tag in tag_names[:_MAX_TAGS]:
        try:
            with os.scandir(os.path.join(root, tag)) as apps:
                app_names = sorted(
                    e.name for e in apps if e.is_dir() and not e.name.startswith("."))
        except OSError:
            continue
        for name in app_names[:_MAX_APPS_PER_TAG]:
            folder = os.path.join(root, tag, name)
            if os.path.isfile(os.path.join(folder, _MANIFEST)):
                out.append(folder)
    return out


def _registered_folders() -> list:
    """Folders from `registered_apps.json` that hold an `mcp.toml`.

    The store the explorer's "Open app" writes (`fused_render/registered_apps.py`)
    — the only record of an app folder living outside the workspace that this
    machine has actually opened. Read for its `path` values and nothing else: the
    filtering that file's own reader does (workspace overlap, the `.fused` extract
    cache) is about what to LIST on the apps hub, and here a duplicate is
    harmless because everything is deduplicated by `realpath` downstream.

    A corrupt or absent store reads as empty, like every other listing here.
    """
    appenv = _shared()
    path = os.path.join(appenv.home_dir(), "registered_apps.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return []
    entries = raw if isinstance(raw, list) else (raw.get("apps") if isinstance(raw, dict) else [])
    out = []
    for entry in entries if isinstance(entries, list) else []:
        folder = entry.get("path") if isinstance(entry, dict) else None
        if isinstance(folder, str) and folder and os.path.isfile(
                os.path.join(folder, _MANIFEST)):
            out.append(folder)
    return out


# ---------------------------------------------------------------------------
# reading one folder's manifest
# ---------------------------------------------------------------------------


def _params_from_signature(signature: str) -> list:
    """The named parameters of a `name(<args>)` snapshot, or `[]`.

    The snapshot `mcp/manifest.py` records is `ast.unparse` of the arguments
    node, so wrapping it back into a `def` is the exact inverse and the two
    cannot disagree about what a parameter is. A hand-edited snapshot that does
    not parse costs the node its parameter hints and nothing else — the palette
    still offers the tool, because the tool is real regardless of what this
    string says.

    `*args`/`**kwargs` are skipped and the entry shape matches
    `mcp/inspect_app.py::_params` field for field: an MCP tool exposes named
    parameters, so a var-arg is not something a node can bind.
    """
    if not signature:
        return []
    try:
        tree = ast.parse("def " + signature + ": pass")
        fn = tree.body[0]
    except (SyntaxError, ValueError, IndexError):
        return []
    if not isinstance(fn, ast.FunctionDef):
        return []
    out = []
    positional = list(fn.args.posonlyargs) + list(fn.args.args)
    defaults = list(fn.args.defaults)
    offset = len(positional) - len(defaults)
    for i, arg in enumerate(positional):
        default = defaults[i - offset] if i >= offset else None
        out.append({
            "name": arg.arg,
            "annotation": ast.unparse(arg.annotation) if arg.annotation else "",
            "default": ast.unparse(default) if default is not None else "",
            "required": default is None,
        })
    for arg, default in zip(fn.args.kwonlyargs, fn.args.kw_defaults):
        out.append({
            "name": arg.arg,
            "annotation": ast.unparse(arg.annotation) if arg.annotation else "",
            "default": ast.unparse(default) if default is not None else "",
            "required": default is None,
        })
    return out


def _derived_signature(folder: str, rel: str, entrypoint: str) -> str:
    """A signature read from the SOURCE, for a manifest that recorded none.

    A hand-written `mcp.toml` has no `signature` key — `mcp/inspect_app.py` calls
    that drift verdict `unknown` — and without one a node would offer no
    parameters at all, which reads to the user as "this tool takes nothing".
    So the fallback is one bounded AST read of the file the manifest names.

    By AST and never by importing, for the reason MC-2 records: these are the
    folders whose modules open token files and hit keychains at import time, and
    listing a parameter name must not run any of it. Failure is `""` — the tool
    is still offered, with no hints.
    """
    if not rel or not rel.endswith(".py"):
        return ""
    try:
        target = os.path.realpath(os.path.join(folder, rel))
        if os.path.commonpath([target, os.path.realpath(folder)]) != os.path.realpath(folder):
            return ""
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read(_SOURCE_LIMIT)
        tree = ast.parse(source)
    except (OSError, ValueError, SyntaxError):
        return ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint:
            return "%s(%s)" % (entrypoint, ast.unparse(node.args))
    return ""


def _app_report(folder: str) -> dict:
    """One app folder: its identity, its curated tools, or the reason there are none.

    Never raises and never omits the folder. A manifest that does not parse is a
    folder with an `error` and an empty `tools` list — the user needs to see that
    their curation is broken far more than they need it silently missing from a
    palette.
    """
    manifest = os.path.join(folder, _MANIFEST)
    report = {
        "folder": folder,
        "name": os.path.basename(folder.rstrip(os.sep)) or folder,
        "manifest": manifest,
        "tools": [],
        "error": "",
    }
    toml = _toml()
    if toml is None:
        report["error"] = ("no TOML parser is available: tomllib needs Python 3.11+, "
                           "and tomli is not installed in this environment")
        return report
    try:
        with open(manifest, "rb") as fh:
            raw = toml.load(fh)
    except (OSError, ValueError) as exc:
        # ValueError covers both TOMLDecodeError and the UnicodeDecodeError
        # tomllib raises on a manifest that is not UTF-8 — a hand-edited
        # Latin-1 file. Either escaping would be a traceback where this module
        # promises a payload.
        report["error"] = "%s does not parse as TOML (%s)" % (_MANIFEST, exc)
        return report

    for entry in (raw.get("tool") or []):
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        rel = str(entry.get("file") or "").strip()
        entrypoint = str(entry.get("entrypoint") or _DEFAULT_ENTRYPOINT).strip()
        pinned = entry.get("pinned")
        pinned = pinned if isinstance(pinned, dict) else {}
        signature = str(entry.get("signature") or "")
        if not signature:
            signature = _derived_signature(folder, rel, entrypoint)
        # The pins are the parameters the CURATOR already fixed: the server
        # removes them from the tool's schema outright (D401 — "a pinned
        # parameter is absent from the schema"), so a node that offered one
        # would be offering a value nothing can send.
        params = [p for p in _params_from_signature(signature) if p["name"] not in pinned]
        report["tools"].append({
            "app": folder,
            "appName": report["name"],
            "name": name,
            "description": str(entry.get("description") or ""),
            "file": rel,
            "entrypoint": entrypoint,
            "pinned": {str(k): v for k, v in pinned.items()},
            "signature": signature,
            "params": params,
        })
    if not report["tools"] and not report["error"]:
        report["error"] = "%s declares no [[tool]] tables." % _MANIFEST
    return report


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def _tools() -> dict:
    """Every curated tool on the machine, grouped by app folder."""
    index_folders, index_state = _index_folders()

    # Deduplicate on `realpath`: the same folder legitimately arrives from two
    # or three sources (a workspace app that has also been indexed and opened),
    # and the sources are recorded per folder so the panel can say WHERE a folder
    # came from — which is what makes "the index is stale" legible rather than
    # mysterious.
    seen = {}
    order = []
    for source, folders in (("index", index_folders),
                            ("workspace", _workspace_folders()),
                            ("registered", _registered_folders())):
        for folder in folders:
            if not folder:
                continue
            try:
                key = os.path.normcase(os.path.realpath(folder))
            except OSError:
                continue
            if key in seen:
                if source not in seen[key]["sources"]:
                    seen[key]["sources"].append(source)
                continue
            if not os.path.isdir(folder):
                # An index row for a folder that has since been deleted. Dropped
                # silently: it is exactly the staleness the index is expected to
                # have, and there is nothing for the user to do about it.
                continue
            seen[key] = {"folder": os.path.abspath(folder), "sources": [source]}
            order.append(key)

    apps = []
    for key in order:
        entry = seen[key]
        report = _app_report(entry["folder"])
        report["sources"] = entry["sources"]
        apps.append(report)
    apps.sort(key=lambda a: (a["name"].lower(), a["folder"]))

    # THE STALENESS GAP, MEASURED RATHER THAN GUESSED. An index that answers is
    # still routinely behind the disk, and here that is not a theory: this
    # machine's index reports zero `mcp.toml` rows while two folders on disk
    # hold one. The disk sources cover those two, so nothing is missing from the
    # palette — but a folder curated somewhere the workspace walk cannot see
    # WOULD be missing, silently, and the user would read an incomplete palette
    # as a complete one.
    #
    # So the payload states the gap as a count of folders the index did not know
    # about, which is a fact rather than an inference, and the panel turns that
    # into the offer to rescan. Reported only when the index ANSWERED: when it
    # could not, `reason` already carries the stronger statement and a second one
    # would just be noise.
    if index_state.get("available"):
        missed = sum(1 for a in apps if "index" not in a["sources"])
        index_state["missedFolders"] = missed
        if missed:
            index_state["message"] = (
                "The file index has not caught up: %d app folder%s here %s found on "
                "disk rather than in the index, so folders outside ~/Fused and your "
                "opened apps may be missing entirely. Scan to catch up."
                % (missed, "" if missed == 1 else "s", "was" if missed == 1 else "were"))
    else:
        index_state["missedFolders"] = None

    appenv = _shared()
    cli_dir = appenv.fused_cli_dir()
    return _ok(
        apps=apps,
        toolCount=sum(len(a["tools"]) for a in apps),
        index=index_state,
        # D334 / MC-5a: the CLI comes from what the server EXPORTED after vetting
        # its own interpreter, never from a PATH lookup. The panel disables Run
        # when this is empty, with the reason on screen — a workflow whose MCP
        # servers name a `fused` that is not there fails inside Claude, where the
        # user cannot see why.
        fusedCli=os.path.join(cli_dir, "fused") if cli_dir else "",
    )


def _rescan() -> dict:
    """Ask the server to rebuild the index, and report what it said.

    Explicit, never automatic. The index is the only source that finds an app
    folder outside `~/Fused`, so when it is missing there is genuinely something
    to do — but a full scan is minutes of I/O across the user's disk, and firing
    one because a canvas was opened would be spending their machine on a guess.
    So the payload above reports the state, and this is the button.
    """
    appenv = _shared()
    origin = appenv.origin()
    if not origin:
        return _refuse(
            "no_server",
            "There is no fused-render server to ask for a scan (this backend is "
            "running standalone).")
    out = _http_json(origin + "/api/index/scan", payload={}, timeout=_SCAN_TIMEOUT)
    if not isinstance(out, dict):
        return _refuse(
            "scan_failed",
            "The scan request to %s/api/index/scan got no usable answer." % origin)
    if out.get("error"):
        return _refuse("scan_failed", str(out["error"]))
    return _ok(runId=out.get("run_id") or "", root=out.get("root") or "")


def main(action: str = "tools") -> dict:
    """`tools` → the palette; `rescan` → start an index scan.

    Anything wrong is a refusal payload (`{ok: false, reason, message}`), never
    an exception: the panel renders the reason, and a raise would be a red
    traceback overlay where a sentence belongs.
    """
    try:
        if action == "tools":
            return _tools()
        if action == "rescan":
            return _rescan()
        return _refuse(
            "unknown_action",
            "unknown action %r — expected 'tools' or 'rescan'." % (action,))
    except Exception as exc:  # noqa: BLE001 — the module's contract is a payload
        return _refuse("failed", "%s: %s" % (type(exc).__name__, exc))
