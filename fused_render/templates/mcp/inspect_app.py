"""The app surface behind `mcp/template.html` — the READ half (SPEC §44 / MC-2, MC-4).

`main(path)` answers everything the curation panel needs to know about one app
folder, in ONE call:

* **what could become a tool** — every top-level `.py`'s top-level entrypoints,
  with their parameter names, annotations and defaults, plus the module
  docstring's first line as a description seed;
* **what the page already pins** — each `fused.runPython()` call site in the
  entry page with its literal arguments, which is the hint that turns
  `runPython("./mail.py", {op: "send"})` into a proposed `op = "send"` pin;
* **whether registration is possible** — the absolute `fused` executable, or
  `""` when none resolves (the panel disables Register and says why, rather than
  writing an MCP entry whose `command` does not exist);
* **whether the curation still matches the code** — the drift verdict (MC-4),
  per manifest tool, comparing the recorded `signature` snapshot against the
  entrypoint's current one.

Everything here is a READ. The write half is `manifest.py`; this module never
touches the folder.

**Signatures come from the AST, never from an import.** These folders are exactly
the ones whose modules open the author's token files, hit a keychain, or talk to
a localhost service at import time — inspecting by importing would run all of
that just to draw a list of parameter names. `ast.parse` answers the question
without executing a line, and it is the same derivation `fused app serve` does on
the other side of the contract, so the panel and the server agree about what an
entrypoint is.

**Top-level only, both for files and for defs.** A `.py` a directory down is not
this app's entrypoint, and a `main` that is a method or a closure is not an
entrypoint at all: the server invokes one by looking the name up in the executed
module's namespace (openfused spec/serve/app-mcp.md §5), where neither appears.
Reporting them would offer the user a tool that cannot be called.

Refusals are PAYLOADS, not exceptions (`{ok: false, reason, message}` — the shape
`git/ops.py` established): the panel renders the reason, and a template backend
that raises gets a red traceback overlay instead of a UI state.

stdlib only, and nothing here imports fused_render (SPEC PY-15). Returns are
JSON-native throughout.
"""

import ast
import json
import os
import re
import sys

# Which `.html` is "the app's page" is NOT decided here: it is
# `app_entry.entry_html`'s answer, the marker being the only signal (D301).

# The manifest filename — the contract with `fused app serve` (openfused
# spec/serve/app-mcp.md §2). Deliberately NOT `openfused.toml`: fused's project
# resolution walks up looking for that name, and an app must never resolve as a
# project.
_MANIFEST = "mcp.toml"

# Ceilings. The panel is interactive, so every read here is bounded: a folder
# with a thousand `.py` files must still answer, and a multi-megabyte page must
# not be scanned in full.
_MAX_FILES = 200
_READ_LIMIT = 1024 * 1024
_PAGE_READ_LIMIT = 512 * 1024

# `fused.runPython("./mail.py", { ... })` call sites in the page. A regex, not a
# JS parser: this is a HINT for the proposal step, not a contract — the user
# edits every pin before it is written, and a missed call site costs a
# suggestion, never correctness. Captures the quoted first argument and, when
# present, the opening of the object literal that follows; the argument list
# itself is then read by a brace scan (`_object_literal`), because a regex
# cannot balance braces.
_RUN_PYTHON = re.compile(
    r"""runPython\s*\(\s*(?P<q>['"`])(?P<file>[^'"`]+)(?P=q)\s*(?P<rest>,)?""")

# One `key: <literal>` pair inside a call site's object literal. A non-literal
# value (an identifier, an expression) matches the bare-key alternative and is
# reported with a null value — the page decides it at runtime, so it must not be
# proposed as a fixed pin.
_ARG_PAIR = re.compile(
    r"""(?P<key>[A-Za-z_$][\w$]*)\s*:\s*(?:(?P<q>['"`])(?P<val>[^'"`]*)(?P=q)"""
    r"""|(?P<num>-?\d+(?:\.\d+)?)|(?P<bool>true|false)|(?P<other>[^,}]+))""")


def _shared_import(name: str, attr: str):
    """An attribute of a `../shared/*.py` module, or None if it cannot be had.

    The guarded `sys.path.insert` every template uses (SPEC PY-15: a template
    never imports fused_render). "Cannot tell" is a None rather than an
    exception, because each caller has a defined answer for the absence.
    """
    shared = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    try:
        module = __import__(name)
    except Exception:  # noqa: BLE001 — absent shared helper: answer "cannot"
        return None
    return getattr(module, attr, None)


def _fused_cli() -> str:
    """The absolute `fused` the registration entry should name, or `""`.

    From `appenv.fused_cli_dir()` — the directory the SERVER exported after
    vetting its own interpreter's CLI and baking `FUSED_ENV` (D334,
    `fusedcli.py`) — never from a PATH lookup. `shutil.which("fused")` was what
    this did, and it is the exact mechanism D334 replaced: on a machine whose app
    venv has no `[fused]` extra but whose PATH carries some other `fused` (a pipx
    shim, another project's venv), the panel would bake that unvetted binary into
    a GLOBAL `~/.claude.json` entry, where it runs with no FUSED_ENV and fails
    somewhere the user cannot see. Absent means registration is not offered.
    """
    cli_dir = _shared_import("appenv", "fused_cli_dir")
    if cli_dir is None:
        return ""
    directory = cli_dir()
    if not directory:
        return ""
    candidate = os.path.join(directory, "fused.exe" if os.name == "nt" else "fused")
    return candidate if os.path.isfile(candidate) else ""


def _toml():
    """The TOML parser, or None: stdlib `tomllib` on 3.11+, else `tomli`.

    `requires-python` is >=3.10 and `tomllib` only became stdlib in 3.11, so on
    3.10 the `tomli` dependency supplies it — the same two-name lookup
    `fused_render/projectenv.py::_load_manifest` does, and for the same reason a
    template cannot just import the package's copy (SPEC PY-15).

    None rather than a raise, because a template backend may also be running in a
    PROJECT venv (SPEC PY-16), which declares its own dependencies and need not
    carry `tomli`. Every caller has a payload for "no parser" — the module's
    contract is that no exception escapes `main`.
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


def _refuse(reason: str, message: str) -> dict:
    """A refusal payload — the panel renders it (never an exception)."""
    return {"ok": False, "reason": reason, "message": message}


def _first_line(doc) -> str:
    """A docstring's first non-empty line, or `""`.

    One line, because it is a description SEED shown in one row of the panel —
    and because a whole docstring pasted into a tool description is noise the
    model pays for on every request. Loops rather than indexing: `None`, `""`
    and a docstring of nothing but whitespace all have to answer `""` instead of
    raising, and this runs over whatever an app folder happens to contain.
    """
    for line in (doc or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _doc_summary(tree) -> str:
    """The module docstring's first non-empty line, or `""`."""
    return _first_line(ast.get_docstring(tree))


def _signature(name: str, fn) -> str:
    """The snapshot string for one entrypoint: `name(<unparsed args>)`.

    `ast.unparse` of the arguments node, deliberately — a hand-built string
    would be a second formatter that drifts from this one, and this is the value
    the manifest RECORDS and the drift check COMPARES. Both sides call this
    function, so a change to the format changes both at once (a manifest written
    before such a change reads as drifted once, which is honest: the panel then
    re-records it).
    """
    return "%s(%s)" % (name, ast.unparse(fn.args))


def _params(fn) -> list:
    """One entry per named parameter: name, annotation, default, required.

    `*args`/`**kwargs` are skipped: an MCP tool exposes named parameters, so a
    var-arg is not something the panel can pin or the server can bind.
    Annotation and default are TEXT (`ast.unparse`) rather than values — the
    panel displays them, and evaluating a default would be executing app code.
    """
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


def _entrypoints(tree) -> list:
    """Every top-level function in `tree`, in source order.

    Every one, not just `main`: an app that grew a second dispatcher
    (`send()` beside `main()`) is exactly the folder worth curating, and the
    manifest's `entrypoint` field exists to name it. Private names (`_helper`)
    are excluded — by convention they are not the module's surface.
    """
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue
        out.append({
            "name": node.name,
            "signature": _signature(node.name, node),
            # `_first_line`, not `.splitlines()[0]`: a docstring that is truthy
            # but strips to nothing (a lone form feed) indexes an empty list, and
            # this call is outside any try — an IndexError here blanked the whole
            # panel over one odd docstring in one file.
            "doc": _first_line(ast.get_docstring(node)),
            "params": _params(node),
        })
    return out


def _read(path: str, limit: int) -> str:
    """Bounded UTF-8 read; decode errors are replaced rather than fatal."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read(limit)


def _python_files(folder: str) -> list:
    """Top-level, non-hidden `.py` names, sorted, capped at `_MAX_FILES`.

    One `scandir`, one level: the same rule the gate documents. Sorted so a
    capped folder reports a deterministic subset instead of one that depends on
    directory order.
    """
    try:
        with os.scandir(folder) as entries:
            names = sorted(
                e.name for e in entries
                if e.name.endswith(".py") and not e.name.startswith(".") and e.is_file()
            )
    except OSError:
        return []
    return names[:_MAX_FILES]


def _file_report(folder: str, name: str) -> dict:
    """One `.py`'s entrypoints, or the parse error that stopped them.

    An unparseable file is REPORTED, not fatal: a half-written sibling must not
    blank out the whole panel, and the error is what tells the user why their
    file offers nothing.
    """
    entry = {"file": name, "doc": "", "entrypoints": [], "error": ""}
    try:
        source = _read(os.path.join(folder, name), _READ_LIMIT)
    except OSError as exc:
        entry["error"] = str(exc)
        return entry
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, exc)
        return entry
    entry["doc"] = _doc_summary(tree)
    entry["entrypoints"] = _entrypoints(tree)
    return entry


def _object_literal(text: str, start: int) -> str:
    """The `{...}` beginning at or after `start`, by brace count, or `""`.

    A brace scan rather than a regex because a nested object or a `}` inside a
    string would defeat one. Quote-aware for the same reason.
    """
    i = start
    while i < len(text) and text[i] not in "{)":
        i += 1
    if i >= len(text) or text[i] != "{":
        return ""
    depth = 0
    quote = ""
    for j in range(i, len(text)):
        ch = text[j]
        if quote:
            if ch == "\\":
                continue
            if ch == quote:
                quote = ""
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return ""


def _call_args(literal: str) -> dict:
    """`{op: "send", to: to}` → `{"op": "send", "to": None}`.

    A literal value is reported as itself (as text — the panel writes pins as
    strings unless the user changes them); a non-literal is reported with `None`,
    which is how the panel knows to offer the parameter WITHOUT proposing a
    value for it.
    """
    args = {}
    for m in _ARG_PAIR.finditer(literal):
        key = m.group("key")
        if m.group("val") is not None:
            args[key] = m.group("val")
        elif m.group("num") is not None:
            args[key] = m.group("num")
        elif m.group("bool") is not None:
            args[key] = m.group("bool")
        else:
            args[key] = None
    return args


def _page_report(folder: str) -> dict:
    """The entry page's `runPython` call sites, in source order.

    WHICH page is `app_entry.entry_html`'s answer — the first non-hidden
    top-level `.html` (name order) carrying `<meta name="fused-app">`, the marker
    being the only signal (D301). Deliberately not `index.html`: a filename
    declares nothing, so a folder whose tagged page is `mail.html` reports ITS
    call sites, and one with an untagged `index.html` beside a tagged `mail.html`
    no longer draws its pin hints from the wrong file. Uncapped here, unlike the
    gate: this runs once, on demand, for a folder the user opened.

    Best-effort by construction (see `_RUN_PYTHON`): the page is JavaScript and
    this is a regex plus a brace scan, so a computed path or an exotic call shape
    is simply not reported. It costs a suggestion, never correctness — the
    manifest is written from the user's edits, not from this.
    """
    entry_html = _shared_import("app_entry", "entry_html")
    page = entry_html(folder) if entry_html is not None else None
    report = {
        "file": os.path.basename(page) if page else "",
        "exists": bool(page),
        "calls": [],
    }
    if not page:
        return report
    try:
        text = _read(page, _PAGE_READ_LIMIT)
    except OSError as exc:
        report["error"] = str(exc)
        return report
    for m in _RUN_PYTHON.finditer(text):
        target = m.group("file")
        # `./mail.py` and `mail.py` are the same file; the manifest records the
        # path relative to the folder.
        rel = target[2:] if target.startswith("./") else target
        args = {}
        if m.group("rest"):
            args = _call_args(_object_literal(text, m.end()))
        report["calls"].append({"file": rel, "args": args})
    return report


def _manifest_report(folder: str) -> dict:
    """The current `mcp.toml`'s `[[tool]]` tables, or the reason there are none.

    Read with the stdlib TOML parser (or `tomli` on 3.10) — the same parser the
    server uses — so the panel's idea
    of what is curated cannot diverge from what `fused app serve` will load. An
    unparseable manifest is reported as an error on this sub-object rather than
    failing the whole report: the surface above it is still readable and still
    worth drawing.
    """
    path = os.path.join(folder, _MANIFEST)
    out = {"exists": os.path.isfile(path), "tools": [], "error": ""}
    if not out["exists"]:
        return out
    toml = _toml()
    if toml is None:
        out["error"] = ("no TOML parser is available: tomllib needs Python 3.11+, "
                        "and tomli is not installed in this folder's environment")
        return out
    try:
        with open(path, "rb") as fh:
            raw = toml.load(fh)
    except (OSError, ValueError) as exc:
        # ValueError covers BOTH tomllib.TOMLDecodeError (a subclass) and the
        # UnicodeDecodeError tomllib raises on a manifest that is not UTF-8 — a
        # hand-edited Latin-1 file. Neither may reach the page as a traceback
        # overlay when this module's contract is a refusal payload.
        out["error"] = str(exc)
        return out
    for entry in raw.get("tool", []) or []:
        if not isinstance(entry, dict):
            continue
        out["tools"].append({
            "name": entry.get("name", ""),
            "description": entry.get("description", ""),
            "file": entry.get("file", ""),
            "entrypoint": entry.get("entrypoint", "main"),
            "pinned": entry.get("pinned", {}) if isinstance(entry.get("pinned"), dict) else {},
            "signature": entry.get("signature", ""),
        })
    return out


def _current_signature(folder: str, rel: str, entrypoint: str) -> str:
    """The named entrypoint's signature TODAY, or `""` if it is gone."""
    if not rel or not entrypoint:
        return ""
    try:
        tree = ast.parse(_read(os.path.join(folder, rel), _READ_LIMIT))
    except (OSError, SyntaxError, ValueError):
        return ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint:
            return _signature(entrypoint, node)
    return ""


def _drift(folder: str, tools: list) -> list:
    """Per curated tool: does the code still look like the manifest says (MC-4)?

    Four verdicts, and the distinctions are the point:

    * `ok` — the recorded snapshot equals the current signature;
    * `changed` — it does not: the served tool still WORKS (the server derives
      its schema from the current source), but the curation was made against a
      different shape, so the pins may name parameters that no longer exist;
    * `missing` — the file or the entrypoint is gone: the served tool is BROKEN,
      which is a different sentence for the panel to say;
    * `unknown` — no snapshot was recorded (a hand-written manifest). There is
      nothing to compare, and claiming either "fine" or "drifted" would be a
      guess.
    """
    out = []
    for tool in tools:
        current = _current_signature(folder, tool["file"], tool["entrypoint"])
        recorded = tool["signature"]
        if not current:
            status = "missing"
        elif not recorded:
            status = "unknown"
        elif recorded == current:
            status = "ok"
        else:
            status = "changed"
        out.append({
            "name": tool["name"],
            "file": tool["file"],
            "entrypoint": tool["entrypoint"],
            "status": status,
            "recorded": recorded,
            "current": current,
        })
    return out


def main(path: str = "") -> dict:
    """The whole app surface for the panel, in one JSON-native payload.

    `path` is the app FOLDER (the mode is folder-only, `condition.py`). A file or
    a missing path is a refusal payload, not an exception.
    """
    if not path or not os.path.isdir(path):
        return _refuse(
            "not_a_folder",
            "The MCP panel curates an app FOLDER's Python entrypoints, and "
            "%r is not a folder." % (path,))

    folder = os.path.abspath(path)
    manifest = _manifest_report(folder)
    return {
        "ok": True,
        "path": folder,
        "name": os.path.basename(folder.rstrip(os.sep)) or folder,
        # The absolute executable the registration entry will name, from the
        # server's own vetted export (D334) and NOT from PATH — see `_fused_cli`.
        # `""` means no registration is possible and the panel says so.
        "fused": _fused_cli(),
        "files": [_file_report(folder, name) for name in _python_files(folder)],
        "page": _page_report(folder),
        "manifest": manifest,
        "drift": _drift(folder, manifest["tools"]) if not manifest["error"] else [],
    }


if __name__ == "__main__":   # pragma: no cover — a hand probe, not a code path
    import sys
    print(json.dumps(main(path=sys.argv[1] if len(sys.argv) > 1 else "."), indent=2))
