"""Manifest IO behind `mcp/template.html` — the WRITE half (SPEC §46 / MC-3).

`main(action=read|write, path, tools=...)` reads and writes the `[[tool]]` tables
in an app folder's `mcp.toml`. That file is the contract with the server side:
`fused app serve <app_dir>` loads exactly these tables and publishes one MCP
tool per entry (openfused `spec/serve/app-mcp.md` §2). `inspect_app.py` beside
this module answers questions; this one changes the folder — the same split, and
the same refusal-as-payload shape, as `git/log.py` vs `git/ops.py`.

Three properties make this module worth its own file rather than a branch of the
reader:

1. **It validates exactly what the server's loader validates** (§3): identifier
   names and entrypoints, a `.py` target inside the folder, an existing
   entrypoint, unique names, identifier pin keys. The server is strict on
   purpose — a bad manifest fails at startup rather than serving a partial tool
   list — and a panel that could write one the server rejects would turn a typo
   into a registration the user only finds out about from inside Claude. So the
   refusal happens here, on the keystroke, naming the offending value.

2. **It captures the `signature` snapshot itself, from the file's current AST.**
   The snapshot is what the drift verdict later compares against
   (`inspect_app._drift`), so it must be recorded at write time by the same
   formatter that reads it — a value supplied by the page, or a second
   formatter, is how a folder ends up permanently reporting drift it does not
   have. Both sides call `_signature` (the same `ast.unparse` of the arguments
   node the reader uses).

3. **It preserves everything in the file it does not own.** `mcp.toml` is a
   plain TOML file the user may hand-edit and other tooling may share, so a
   write replaces the `[[tool]]` array and NOTHING else — comments and unrelated
   tables survive — and the result is re-parsed before it is committed to disk
   (below).

The write is atomic (temp file + `os.replace`) and *verified*: the rendered text
is parsed back with `tomllib` and checked to contain the intended tools and every
unrelated key that was there before. Only then does it replace the original. That
verification is the reason a hand-rolled renderer is acceptable at all — the
failure mode of TOML generation is silent corruption of a file the user owns, and
a round-trip check turns that into a refusal payload.

Strings are encoded with `json.dumps`, never a `.replace()` chain: a TOML basic
string and a JSON string share their escape grammar (`\\"`, `\\\\`, `\\n`, `\\t`,
`\\uXXXX`), and `json.dumps` is the rigorous encoder for it. Hand-rolled quoting
is how a description containing a quote or a backslash corrupts the file.

stdlib only, and nothing here imports fused_render (SPEC PY-15). Returns are
JSON-native throughout; no exception ever escapes `main`.
"""

import ast
import json
import os
import tempfile
import tomllib

_MANIFEST = "mcp.toml"
_DEFAULT_ENTRYPOINT = "main"

# The keys this module owns in `mcp.toml`. Everything else in the file is the
# user's (or another tool's) and is copied through untouched.
_OWNED_TABLE = "tool"

# Bounded read of an entrypoint file — only to capture the signature snapshot.
_READ_LIMIT = 1024 * 1024

# What a pin may hold: the JSON values that survive the server's `_params.json`
# encoding. A TOML date/time has no JSON equivalent, and the server rejects one
# at load time, so it is refused here too.
_PIN_SCALARS = (str, int, float, bool)

_HEADER = (
    "# MCP tools for this app, curated in fused-render's MCP panel.\n"
    "# `fused app serve <this folder>` publishes one tool per [[tool]] table.\n"
)


def _ok(**extra) -> dict:
    out = {"ok": True}
    out.update(extra)
    return out


def _refuse(reason: str, message: str) -> dict:
    """A refusal payload — the panel renders it (never an exception)."""
    return {"ok": False, "reason": reason, "message": message}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def _load(path: str):
    """`(tables, error)` for `<path>` — `({}, "")` when the file is absent."""
    if not os.path.isfile(path):
        return {}, ""
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh), ""
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {}, str(exc)


def _tool_from_table(entry: dict) -> dict:
    """One `[[tool]]` table as the panel's editable shape."""
    pinned = entry.get("pinned")
    return {
        "name": entry.get("name", ""),
        "description": entry.get("description", ""),
        "file": entry.get("file", ""),
        "entrypoint": entry.get("entrypoint", _DEFAULT_ENTRYPOINT),
        "pinned": pinned if isinstance(pinned, dict) else {},
        "signature": entry.get("signature", ""),
    }


def _read(folder: str) -> dict:
    manifest = os.path.join(folder, _MANIFEST)
    raw, error = _load(manifest)
    if error:
        return _refuse(
            "bad_manifest",
            "%s does not parse as TOML (%s). Fix it by hand — rewriting it here "
            "would discard whatever is in there." % (manifest, error))
    tools = [_tool_from_table(e) for e in (raw.get(_OWNED_TABLE) or []) if isinstance(e, dict)]
    return _ok(path=manifest, exists=os.path.isfile(manifest), tools=tools)


# ---------------------------------------------------------------------------
# validation — the server's loader rules, enforced at the keystroke
# ---------------------------------------------------------------------------


def _signature(name: str, fn) -> str:
    """`name(<unparsed args>)` — the same formatter `inspect_app` compares with."""
    return "%s(%s)" % (name, ast.unparse(fn.args))


def _entrypoint_signature(folder: str, rel: str, entrypoint: str):
    """`(signature, error)` for a top-level `def entrypoint` in `folder/rel`.

    By AST, never by importing: an app module's import side effects are exactly
    what must not run because someone opened a curation panel.
    """
    target = os.path.join(folder, rel)
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read(_READ_LIMIT)
    except OSError as exc:
        return "", str(exc)
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as exc:
        return "", "%s does not parse as Python (%s)" % (rel, exc)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint:
            return _signature(entrypoint, node), ""
    return "", ("%s defines no top-level %r function — the served tool would fail "
                "at startup." % (rel, entrypoint))


def _pin_ok(value) -> bool:
    """Whether a pinned value survives the server's JSON encoding of params."""
    if isinstance(value, bool) or isinstance(value, _PIN_SCALARS) or value is None:
        return True
    if isinstance(value, list):
        return all(_pin_ok(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _pin_ok(v) for k, v in value.items())
    return False


def _validated(folder: str, tools) -> tuple:
    """`(clean_tools, refusal)` — one refusal, naming the first bad value.

    Fresh `signature` snapshots are captured here, as part of validation: the
    check that the entrypoint EXISTS and the read that records its shape are the
    same read, so the two can never disagree.
    """
    if isinstance(tools, str):
        # runPython params cross as strings, so the panel may hand the array
        # over JSON-encoded.
        try:
            tools = json.loads(tools)
        except ValueError as exc:
            return [], _refuse("invalid_tools", "tools is not valid JSON: %s" % exc)
    if tools is None:
        tools = []
    if not isinstance(tools, list):
        return [], _refuse("invalid_tools", "tools must be a list of tool objects.")

    clean = []
    seen = set()
    for index, tool in enumerate(tools):
        where = "tool #%d" % (index + 1)
        if not isinstance(tool, dict):
            return [], _refuse("invalid_tool", "%s is not an object." % where)

        name = str(tool.get("name") or "").strip()
        if not name.isidentifier():
            return [], _refuse(
                "invalid_tool",
                "%s: name %r must be a Python identifier — it names the tool the "
                "model calls." % (where, name))
        if name in seen:
            return [], _refuse("invalid_tool", "%s: duplicate tool name %r." % (where, name))
        seen.add(name)

        description = str(tool.get("description") or "").strip()
        if not description:
            return [], _refuse(
                "invalid_tool",
                "%s (%s): a description is required — it is all the model has to "
                "decide whether to call the tool." % (where, name))

        rel = str(tool.get("file") or "").strip()
        refusal = _target_refusal(folder, rel, where, name)
        if refusal is not None:
            return [], refusal

        entrypoint = str(tool.get("entrypoint") or _DEFAULT_ENTRYPOINT).strip()
        if not entrypoint.isidentifier():
            return [], _refuse(
                "invalid_tool",
                "%s (%s): entrypoint %r must be a Python identifier."
                % (where, name, entrypoint))

        pinned = tool.get("pinned") or {}
        if not isinstance(pinned, dict):
            return [], _refuse(
                "invalid_tool", "%s (%s): pinned must be an object." % (where, name))
        for key, value in pinned.items():
            if not str(key).isidentifier():
                return [], _refuse(
                    "invalid_tool",
                    "%s (%s): pinned key %r must be a parameter name."
                    % (where, name, key))
            if not _pin_ok(value):
                return [], _refuse(
                    "invalid_tool",
                    "%s (%s): pinned value for %r cannot be written as JSON."
                    % (where, name, key))

        signature, error = _entrypoint_signature(folder, rel, entrypoint)
        if error:
            return [], _refuse("invalid_tool", "%s (%s): %s" % (where, name, error))

        clean.append({
            "name": name,
            "description": description,
            "file": rel,
            "entrypoint": entrypoint,
            "pinned": dict(pinned),
            "signature": signature,
        })
    return clean, None


def _target_refusal(folder: str, rel: str, where: str, name: str):
    """The `file` field's refusal, or `None`.

    Containment is checked on the RESOLVED path: neither an absolute path nor a
    `..` segment may point a served tool at code outside the folder the user is
    about to register.
    """
    if not rel:
        return _refuse("invalid_tool", "%s (%s): file is required." % (where, name))
    if not rel.endswith(".py"):
        return _refuse(
            "invalid_tool",
            "%s (%s): file %r is not a .py file." % (where, name, rel))
    try:
        resolved = os.path.realpath(os.path.join(folder, rel))
        root = os.path.realpath(folder)
    except OSError as exc:
        return _refuse("invalid_tool", "%s (%s): file %r: %s" % (where, name, rel, exc))
    if os.path.commonpath([resolved, root]) != root:
        return _refuse(
            "invalid_tool",
            "%s (%s): file %r is outside the app folder — a served tool may only "
            "run the app's own code." % (where, name, rel))
    if not os.path.isfile(resolved):
        return _refuse(
            "invalid_tool", "%s (%s): file %r does not exist." % (where, name, rel))
    return None


# ---------------------------------------------------------------------------
# rendering + writing
# ---------------------------------------------------------------------------


def _toml_value(value) -> str:
    """One TOML value.

    Strings go through `json.dumps` — a TOML basic string and a JSON string
    share their escape grammar, so the rigorous encoder for the one is the
    rigorous encoder for the other. Nothing here builds a quoted string with
    `.replace()`.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[%s]" % ", ".join(_toml_value(v) for v in value)
    if isinstance(value, dict):
        return "{%s}" % ", ".join(
            "%s = %s" % (_toml_key(k), _toml_value(v)) for k, v in value.items())
    # Unreachable: `_pin_ok` refused everything else before this point.
    return json.dumps(str(value))


def _toml_key(key: str) -> str:
    """A bare key when it is one, else a quoted key (same encoder as values)."""
    if key and all(c.isalnum() or c in "-_" for c in key):
        return key
    return json.dumps(key)


def _render_tools(tools: list) -> str:
    """The `[[tool]]` block for every curated tool, in the panel's order."""
    out = []
    for tool in tools:
        lines = ["[[tool]]"]
        for key in ("name", "description", "file", "entrypoint", "signature"):
            lines.append("%s = %s" % (key, _toml_value(tool[key])))
        if tool["pinned"]:
            lines.append("")
            lines.append("[tool.pinned]")
            for pin, value in tool["pinned"].items():
                lines.append("%s = %s" % (_toml_key(pin), _toml_value(value)))
        out.append("\n".join(lines))
    return "\n\n".join(out)


def _strip_tool_tables(text: str) -> str:
    """`text` with every `[[tool]]` / `[tool.*]` block removed, rest verbatim.

    A line scan over table headers, which is what keeps comments and unrelated
    tables byte-identical — a re-render from the parsed data would drop them. A
    header line inside a multi-line string would fool it, which is precisely why
    the result is re-parsed and compared before anything is written
    (`_write`): the check, not the scan, is what makes this safe.
    """
    kept = []
    dropping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[["):
            dropping = stripped[2:].split("]")[0].strip() == _OWNED_TABLE
        elif stripped.startswith("["):
            head = stripped[1:].split("]")[0].strip()
            dropping = head == _OWNED_TABLE or head.startswith(_OWNED_TABLE + ".")
        if not dropping:
            kept.append(line)
    return "\n".join(kept).strip("\n")


def _write(folder: str, tools: list) -> dict:
    """Render, verify by re-parsing, then replace the manifest atomically."""
    manifest = os.path.join(folder, _MANIFEST)
    existing_text = ""
    existing, error = _load(manifest)
    if error:
        return _refuse(
            "bad_manifest",
            "%s does not parse as TOML (%s). Fix it by hand — rewriting it here "
            "would discard whatever is in there." % (manifest, error))
    if os.path.isfile(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as fh:
                existing_text = fh.read()
        except OSError as exc:
            return _refuse("unwritable", "cannot read %s: %s" % (manifest, exc))

    preserved = _strip_tool_tables(existing_text) if existing_text else ""
    rendered = _render_tools(tools)
    parts = [p for p in (preserved or _HEADER.rstrip("\n"), rendered) if p]
    text = "\n\n".join(parts) + "\n"

    # Verify BEFORE replacing: the rendered file must parse, carry exactly the
    # intended tools, and still carry every key this module does not own.
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return _refuse(
            "render_failed",
            "the manifest this would write does not parse (%s) — nothing was "
            "written." % exc)
    if [t.get("name") for t in parsed.get(_OWNED_TABLE, [])] != [t["name"] for t in tools]:
        return _refuse(
            "render_failed",
            "the manifest this would write does not read back as the curated "
            "tools — nothing was written.")
    lost = [k for k in existing if k != _OWNED_TABLE and k not in parsed]
    if lost:
        return _refuse(
            "render_failed",
            "writing would drop unrelated content from %s (%s) — nothing was "
            "written; edit the file by hand." % (manifest, ", ".join(sorted(lost))))

    try:
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".mcp-toml-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, manifest)
        except OSError:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
    except OSError as exc:
        return _refuse("unwritable", "cannot write %s: %s" % (manifest, exc))

    return _ok(path=manifest, exists=True, tools=tools)


def main(action: str = "read", path: str = "", tools=None) -> dict:
    """Read or write the app folder's `mcp.toml`.

    `read` → `{ok, path, exists, tools}`; `write` (with `tools`, a list of
    `{name, description, file, entrypoint?, pinned?}` or its JSON string) →
    the same shape with fresh `signature` snapshots. Anything wrong is a refusal
    payload (`{ok: false, reason, message}`), never an exception: the panel
    renders the reason, and a raise would be a red traceback overlay instead.
    """
    if not path or not os.path.isdir(path):
        return _refuse(
            "not_a_folder",
            "The MCP manifest belongs to an app FOLDER, and %r is not one." % (path,))
    folder = os.path.abspath(path)

    if action == "read":
        return _read(folder)
    if action == "write":
        clean, refusal = _validated(folder, tools)
        if refusal is not None:
            return refusal
        return _write(folder, clean)
    return _refuse(
        "unknown_action", "unknown action %r — expected 'read' or 'write'." % (action,))
